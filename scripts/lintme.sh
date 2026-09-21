#!/bin/bash
#
# lintme.sh -- the lint gate for auditry.
#
# Two languages, one gate. This is the same script the Lint workflow runs, so a
# clean run here is a clean run on the pull request.
#
#   Python   ruff check    lint; autofixes applied unless --check is given
#            ruff format   formatting; rewritten in place unless --check
#   Shell    shellcheck    lint; report only, it has no autofix
#            shfmt         formatting; rewritten in place unless --check
#
# Within each language, lint runs before the format check, so a syntax-level
# problem is never reported as a formatting one.
#
# Every tool comes from .venv, which uv installs from uv.lock. That includes
# the two shell binaries, which ship inside wheels. One pinned version per
# tool, shared by CI and every developer.
#
# (Careful editing these comments: a line starting "# shellcheck " is parsed
# as a shellcheck directive, not prose.)
#
# Usage:
#   ./scripts/lintme.sh                  autofix, then format in place
#   ./scripts/lintme.sh --check          report only, write nothing (CI mode)
#   ./scripts/lintme.sh --unsafe-fixes   also apply the fixes ruff marks unsafe
#   ./scripts/lintme.sh --help
#
# Exit codes:
#   0  clean
#   1  violations remain, or files are not formatted
#   2  bad usage
#   3  a linter is not available -- run ./scripts/bootstrap.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT

readonly VENV_BIN="${REPO_ROOT}/.venv/bin"
readonly PYPROJECT="${REPO_ROOT}/pyproject.toml"

# -i 4  four-space indent, matching the scripts as written
# -ci   indent switch/case bodies
# -bn   a binary operator may start a continuation line
#
# Deliberately no -kp (keep column padding): it reads manually aligned
# definitions as padded columns, and when it then splits a one-liner it
# inherits the padding column and cascades the indent off to the right.
readonly SHFMT_FLAGS=(-i 4 -ci -bn)

APPLY_FIX=1
UNSAFE_FIXES=0

RUFF=""
SHELLCHECK=""
SHFMT=""
SHELL_FILES=()

# Filled in by the steps below, printed by summary().
FAILURES=()

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
ok() { printf '    ok: %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }

die() {
    printf '\nerror: %s\n' "$*" >&2
    exit 2
}

fail() {
    printf '    FAIL: %s\n' "$*" >&2
    FAILURES+=("$1")
}

have() { command -v "$1" >/dev/null 2>&1; }

# Print the leading comment block: line 3 through the last consecutive '#' line.
usage() {
    awk 'NR < 3 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --check) APPLY_FIX=0 ;;
            --fix) APPLY_FIX=1 ;;
            --unsafe-fixes) UNSAFE_FIXES=1 ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) die "unknown option: $1 (try --help)" ;;
        esac
        shift
    done
}

# ---------------------------------------------------------------------------
# Toolchain
# ---------------------------------------------------------------------------

# The repo venv wins over whatever else is on PATH, so the version that runs
# here is the one uv.lock resolved. Missing tooling exits 3 rather than being
# quietly skipped: a linter that did not run must never read as clean.
resolve_tool() {
    local name="$1" path
    if [ -x "${VENV_BIN}/${name}" ]; then
        path="${VENV_BIN}/${name}"
    elif have "$name"; then
        path="$(command -v "$name")"
    else
        printf '\nerror: %s not found in .venv or on PATH.\n' "$name" >&2
        printf '       Run ./scripts/bootstrap.sh to provision the environment.\n' >&2
        exit 3
    fi
    printf '%s' "$path"
}

resolve_tools() {
    step "Resolving linters"
    RUFF="$(resolve_tool ruff)"
    SHELLCHECK="$(resolve_tool shellcheck)"
    SHFMT="$(resolve_tool shfmt)"

    ok "$("$RUFF" --version)"
    # The --version output is a multi-line banner; the number is its own field.
    ok "shellcheck $("$SHELLCHECK" --version | awk '/^version:/ { print $2 }')"
    ok "shfmt $("$SHFMT" --version)"
}

# One ruff version per repo (pyproject.toml's dev extra). A newer or older
# binary on PATH disagrees about what a violation is, which shows up as a
# green local run and a red pull request.
check_pin() {
    local pinned running
    pinned="$(sed -n 's/.*"ruff==\([0-9][0-9.]*\)".*/\1/p' "$PYPROJECT" | head -1)"
    [ -n "$pinned" ] || return 0

    running="$("$RUFF" --version | awk '{print $2}')"
    if [ "$running" = "$pinned" ]; then
        ok "ruff matches the pin in pyproject.toml"
    else
        warn "running ruff ${running} but pyproject.toml pins ${pinned}"
        warn "CI uses the pin; re-run ./scripts/bootstrap.sh to line them up"
    fi
}

# Every *.sh in the repo, not just scripts/, so a shell file added elsewhere
# is still covered.
collect_shell_files() {
    SHELL_FILES=()
    while IFS= read -r file; do
        SHELL_FILES+=("$file")
    done < <(find . -name '*.sh' -not -path './.venv/*' -not -path './.git/*' | sort)
}

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

lint_python() {
    step "Python lint (ruff check)"
    local args=(check)
    local annotate=0

    if [ "$APPLY_FIX" -eq 1 ]; then
        args+=(--fix)
        [ "$UNSAFE_FIXES" -eq 1 ] && args+=(--unsafe-fixes)
    else
        [ "$UNSAFE_FIXES" -eq 1 ] && warn "--unsafe-fixes has no effect with --check"
        # Inline annotations on the diff instead of a wall of log output.
        if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
            args+=(--output-format=github)
            annotate=1
        fi
    fi

    local status=0
    if [ "$annotate" -eq 1 ]; then
        # ruff puts an absolute path in the annotation's file= field, and
        # GitHub only pins an annotation to the diff when that path is
        # relative to the workspace, so the prefix is stripped back off here.
        # https://github.com/astral-sh/ruff/issues/17433
        # pipefail is set, so ruff's exit status still decides the outcome.
        "$RUFF" "${args[@]}" . | sed "s|${REPO_ROOT}/||g" || status=$?
    else
        "$RUFF" "${args[@]}" . || status=$?
    fi

    if [ "$status" -eq 0 ]; then
        ok "no violations"
    else
        fail "ruff check reported violations"
    fi
}

format_python() {
    step "Python format (ruff format)"

    if [ "$APPLY_FIX" -eq 1 ]; then
        if "$RUFF" format .; then
            ok "formatted"
        else
            fail "ruff format failed"
        fi
        return
    fi

    if "$RUFF" format --check .; then
        ok "all files formatted"
    else
        fail "Python files are not formatted (run ./scripts/lintme.sh without --check)"
    fi
}

# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

lint_shell() {
    step "Shell lint (shellcheck)"
    if [ "${#SHELL_FILES[@]}" -eq 0 ]; then
        ok "no shell files to check"
        return
    fi

    # There is no autofix worth applying unattended here, so this step reports
    # in both modes. -x follows `source`d files rather than warning on them.
    if "$SHELLCHECK" -x "${SHELL_FILES[@]}"; then
        ok "${#SHELL_FILES[@]} file(s), no findings"
    else
        fail "shellcheck reported findings"
    fi
}

format_shell() {
    step "Shell format (shfmt)"
    if [ "${#SHELL_FILES[@]}" -eq 0 ]; then
        ok "no shell files to format"
        return
    fi

    if [ "$APPLY_FIX" -eq 1 ]; then
        if "$SHFMT" -w "${SHFMT_FLAGS[@]}" "${SHELL_FILES[@]}"; then
            ok "formatted"
        else
            fail "shfmt failed"
        fi
        return
    fi

    # -d prints a diff and exits non-zero when formatting differs.
    if "$SHFMT" -d "${SHFMT_FLAGS[@]}" "${SHELL_FILES[@]}"; then
        ok "all files formatted"
    else
        fail "shell files are not formatted (run ./scripts/lintme.sh without --check)"
    fi
}

# ---------------------------------------------------------------------------

summary() {
    step "Summary"
    if [ "${#FAILURES[@]}" -gt 0 ]; then
        local item
        printf '%d step(s) failed:\n' "${#FAILURES[@]}" >&2
        for item in "${FAILURES[@]}"; do
            printf '  - %s\n' "$item" >&2
        done
        return 1
    fi
    ok "lint clean"
}

main() {
    parse_args "$@"
    cd "$REPO_ROOT"

    resolve_tools
    check_pin
    collect_shell_files

    if [ "$APPLY_FIX" -eq 1 ]; then
        info "mode: fix (files are rewritten)"
    else
        info "mode: check (nothing is written)"
    fi

    lint_python
    format_python
    lint_shell
    format_shell
    summary
}

main "$@"
