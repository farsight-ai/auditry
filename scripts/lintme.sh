#!/bin/bash
#
# lintme.sh -- the lint gate for auditry.
#
# Ruff is the only linter and the only formatter. This is the same script the
# Lint workflow runs, so a clean run here is a clean run on the pull request.
#
#   ruff check    lint; autofixes are applied unless --check is given
#   ruff format   formatting; rewritten in place unless --check is given
#
# Lint runs before the format check, so a syntax-level problem is never
# reported as a formatting one.
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
#   3  ruff is not available -- run ./scripts/bootstrap.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT

readonly VENV_RUFF="${REPO_ROOT}/.venv/bin/ruff"
readonly PYPROJECT="${REPO_ROOT}/pyproject.toml"

APPLY_FIX=1
UNSAFE_FIXES=0
RUFF=""

# Filled in by the steps below, printed by summary().
FAILURES=()

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
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
            --check)        APPLY_FIX=0 ;;
            --fix)          APPLY_FIX=1 ;;
            --unsafe-fixes) UNSAFE_FIXES=1 ;;
            -h|--help)      usage; exit 0 ;;
            *)              die "unknown option: $1 (try --help)" ;;
        esac
        shift
    done
}

# ---------------------------------------------------------------------------
# Toolchain
# ---------------------------------------------------------------------------

# The repo venv wins over whatever else is on PATH, so the version that runs
# here is the one uv.lock resolved.
resolve_ruff() {
    step "Resolving ruff"
    if [ -x "$VENV_RUFF" ]; then
        RUFF="$VENV_RUFF"
    elif have ruff; then
        RUFF="$(command -v ruff)"
    else
        printf '\nerror: ruff not found in .venv or on PATH.\n' >&2
        printf '       Run ./scripts/bootstrap.sh to provision the environment.\n' >&2
        exit 3
    fi
    ok "$("$RUFF" --version) at ${RUFF}"
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
        ok "matches the pin in pyproject.toml"
    else
        warn "running ruff ${running} but pyproject.toml pins ${pinned}"
        warn "CI uses the pin; re-run ./scripts/bootstrap.sh to line them up"
    fi
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

lint() {
    step "Lint (ruff check)"
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

format() {
    step "Format (ruff format)"
    local args=(format)

    if [ "$APPLY_FIX" -eq 1 ]; then
        if "$RUFF" "${args[@]}" .; then
            ok "formatted"
        else
            fail "ruff format failed"
        fi
        return
    fi

    args+=(--check)
    if "$RUFF" "${args[@]}" .; then
        ok "all files formatted"
    else
        fail "files are not formatted (run ./scripts/lintme.sh without --check)"
    fi
}

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

    resolve_ruff
    check_pin

    if [ "$APPLY_FIX" -eq 1 ]; then
        info "mode: fix (files are rewritten)"
    else
        info "mode: check (nothing is written)"
    fi

    lint
    format
    summary
}

main "$@"
