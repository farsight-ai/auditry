#!/bin/bash
#
# runtests.sh -- the test gate for auditry.
#
# Runs pytest from the repository root against the interpreter in .venv. This
# is the same script the Tests workflow runs.
#
# The suite imports auditry as an installed package (some modules import
# `auditry.*` and others `src.auditry.*`), so the environment is checked before
# pytest starts -- a missing install shows up as a collection error otherwise.
#
# Usage:
#   ./scripts/runtests.sh                     the whole suite
#   ./scripts/runtests.sh -k redaction        any further arguments go to pytest
#   ./scripts/runtests.sh -x -vv tests/test_redaction.py
#   ./scripts/runtests.sh --help
#
# Environment overrides:
#   PYTEST_ADDOPTS   honoured by pytest itself, e.g. PYTEST_ADDOPTS=-q
#
# Exit codes:
#   0  the suite passed
#   3  the environment is not usable -- run ./scripts/bootstrap.sh
#   *  pytest's own exit code otherwise (1 failures, 2 interrupted, 5 no tests)
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT

readonly VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"

PYTHON_BIN=""

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }

# Exit 3 is reserved for "the environment is not usable", so a broken venv is
# never mistaken for a failing test.
die_env() {
    printf '\nerror: %s\n' "$*" >&2
    printf '       Run ./scripts/bootstrap.sh to provision the environment.\n' >&2
    exit 3
}

# Print the leading comment block: line 3 through the last consecutive '#' line.
usage() {
    awk 'NR < 3 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

# The repo venv wins; an already-activated environment is the fallback so a
# `uv run` or a hand-rolled shell still works.
resolve_python() {
    step "Resolving interpreter"
    if [ -x "$VENV_PYTHON" ]; then
        PYTHON_BIN="$VENV_PYTHON"
    elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/bin/python" ]; then
        PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
        info "using the activated environment at ${VIRTUAL_ENV}"
    else
        die_env "no interpreter at ${VENV_PYTHON} and no activated virtualenv"
    fi
    ok "$("$PYTHON_BIN" --version) at ${PYTHON_BIN}"
}

check_imports() {
    step "Checking the environment"

    "$PYTHON_BIN" -c 'import pytest' 2>/dev/null \
        || die_env "pytest is not installed in ${PYTHON_BIN%/bin/python}"

    local version
    if version="$("$PYTHON_BIN" -c 'import auditry; print(auditry.__version__)' 2>/dev/null)"; then
        ok "auditry ${version} imports"
    else
        die_env "auditry is not installed (the suite imports it as a package)"
    fi
}

# ---------------------------------------------------------------------------

main() {
    if [ $# -gt 0 ] && { [ "$1" = "-h" ] || [ "$1" = "--help" ]; }; then
        usage
        exit 0
    fi

    cd "$REPO_ROOT"
    resolve_python
    check_imports

    # No explicit path: testpaths in pyproject.toml decides what is collected,
    # and any argument given here overrides it the way pytest normally does.
    step "Running pytest"
    exec "$PYTHON_BIN" -m pytest "$@"
}

main "$@"
