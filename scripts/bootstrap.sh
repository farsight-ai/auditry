#!/bin/bash
#
# bootstrap.sh -- local development setup for auditry on macOS.
#
# The Python interpreter is governed by pyenv. Project dependencies are
# resolved by uv from the committed uv.lock. Every step checks for what it
# needs before it installs anything, so this script is safe to re-run.
#
# Homebrew is the one prerequisite: install it yourself before running this.
#
# Usage:
#   ./scripts/bootstrap.sh                   full bootstrap
#   ./scripts/bootstrap.sh --no-shell-init   leave ~/.zshrc untouched
#   ./scripts/bootstrap.sh --run-tests       also run the test suite at the end
#   ./scripts/bootstrap.sh --help            show usage
#
# Environment overrides:
#   AUDITRY_PYTHON_VERSION   CPython version to pin (default below)
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Highest CPython series declared in the pyproject.toml classifiers.
readonly DEFAULT_PYTHON_VERSION="3.12.14"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT

readonly VENV_DIR="${REPO_ROOT}/.venv"
readonly PIN_FILE="${REPO_ROOT}/.python-version"
readonly LOCK_FILE="${REPO_ROOT}/uv.lock"

# Suggested pyenv build environment for macOS, per the pyenv wiki:
# https://github.com/pyenv/pyenv/wiki#suggested-build-environment
readonly BREW_BUILD_DEPS=(
    openssl@3
    readline
    sqlite3
    xz
    tcl-tk@8
    libb2
    zstd
    zlib
    pkgconfig
)

DO_SHELL_INIT=1
RUN_TESTS=0
PYTHON_VERSION=""
PYTHON_BIN=""

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
ok() { printf '    ok: %s\n' "$*"; }
act() { printf '    installing: %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }
die() {
    printf '\nerror: %s\n' "$*" >&2
    exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }

# Print the leading comment block: line 3 through the last consecutive '#' line.
usage() {
    awk 'NR < 3 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-shell-init) DO_SHELL_INIT=0 ;;
            --run-tests) RUN_TESTS=1 ;;
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
# Platform prerequisites
# ---------------------------------------------------------------------------

require_macos() {
    step "Checking platform"
    [ "$(uname -s)" = "Darwin" ] || die "this script targets macOS; found $(uname -s)"
    ok "macOS $(sw_vers -productVersion) on $(uname -m)"
}

ensure_command_line_tools() {
    step "Checking Xcode Command Line Tools"
    if xcode-select -p >/dev/null 2>&1; then
        ok "present at $(xcode-select -p)"
        return
    fi
    act "Xcode Command Line Tools"
    info "a system dialog will open; re-run this script once it finishes"
    xcode-select --install || true
    die "Command Line Tools are required to build CPython from source"
}

# First line of `brew --version`, without a pipe that could SIGPIPE brew.
brew_version() {
    local raw
    raw="$(brew --version)"
    printf '%s\n' "${raw%%$'\n'*}"
}

ensure_homebrew() {
    step "Checking Homebrew"
    if have brew; then
        ok "$(brew_version)"
        return
    fi
    # Homebrew's installer is interactive and wants sudo, so it is the one
    # prerequisite this script asks you to install rather than installing itself.
    die "Homebrew not found. Install it, then re-run this script:
    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
}

# Install a single Homebrew formula only when it is not already present.
ensure_brew_formula() {
    local formula="$1"
    if brew list --versions "$formula" >/dev/null 2>&1; then
        ok "${formula} $(brew list --versions "$formula" | awk '{print $2}')"
    else
        act "$formula"
        brew install "$formula"
    fi
}

ensure_build_deps() {
    step "Checking CPython build dependencies"
    local formula
    for formula in "${BREW_BUILD_DEPS[@]}"; do
        ensure_brew_formula "$formula"
    done
}

# ---------------------------------------------------------------------------
# pyenv
# ---------------------------------------------------------------------------

ensure_pyenv() {
    step "Checking pyenv"
    have pyenv || {
        act "pyenv"
        brew install pyenv
    }

    export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
    [ -d "${PYENV_ROOT}/bin" ] && export PATH="${PYENV_ROOT}/bin:${PATH}"
    eval "$(pyenv init -)"

    have pyenv || die "pyenv is installed but not resolvable on PATH"
    ok "pyenv $(pyenv --version | awk '{print $2}'), root ${PYENV_ROOT}"
}

# pyenv is only useful in later shells if it is wired into the shell rc file.
ensure_shell_init() {
    step "Checking pyenv shell initialization"
    if [ "$DO_SHELL_INIT" -eq 0 ]; then
        info "skipped (--no-shell-init)"
        return
    fi

    local rc="${HOME}/.zshrc"
    if [ -f "$rc" ] && grep -q 'pyenv init' "$rc"; then
        ok "${rc} already initializes pyenv"
        return
    fi

    local stamp backup
    stamp="$(date +%Y%m%d%H%M%S)"
    backup="${rc}.bootstrap-${stamp}.bak"
    [ -f "$rc" ] && cp "$rc" "$backup" && info "backed up ${rc} to ${backup}"

    act "pyenv init lines in ${rc}"
    # SC2016: these lines are written to the rc file verbatim. They have to be
    # expanded by the shell that later sources it, not by this one.
    # shellcheck disable=SC2016
    {
        printf '\n# Added by auditry scripts/bootstrap.sh\n'
        printf 'export PYENV_ROOT="$HOME/.pyenv"\n'
        printf '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"\n'
        printf 'eval "$(pyenv init - zsh)"\n'
    } >>"$rc"
    info "open a new shell, or run: source ${rc}"
}

# Precedence: explicit env override, then an existing repo pin, then default.
resolve_python_version() {
    step "Resolving target Python version"
    if [ -n "${AUDITRY_PYTHON_VERSION:-}" ]; then
        PYTHON_VERSION="$AUDITRY_PYTHON_VERSION"
        info "from AUDITRY_PYTHON_VERSION"
    elif [ -f "$PIN_FILE" ]; then
        PYTHON_VERSION="$(tr -d '[:space:]' <"$PIN_FILE")"
        info "from existing ${PIN_FILE##*/}"
    else
        PYTHON_VERSION="$DEFAULT_PYTHON_VERSION"
        info "using script default"
    fi
    [ -n "$PYTHON_VERSION" ] || die "could not determine a Python version to pin"
    ok "target ${PYTHON_VERSION}"
}

ensure_python_version() {
    step "Checking CPython ${PYTHON_VERSION}"
    # Capture before matching. Piping into `grep -q` makes grep exit on the
    # first match and close the pipe, which SIGPIPEs pyenv; under `pipefail`
    # that turns a successful match into a failed test and we would reinstall
    # a version that is already present. -F keeps the dots literal.
    local installed
    installed="$(pyenv versions --bare --skip-aliases)"
    if grep -qxF "$PYTHON_VERSION" <<<"$installed"; then
        ok "already installed"
    else
        act "CPython ${PYTHON_VERSION} (built from source, this takes a few minutes)"
        # --skip-existing so a stray or partial version directory can never
        # stop the run on an interactive prompt.
        pyenv install --skip-existing "$PYTHON_VERSION"
    fi

    PYTHON_BIN="$(pyenv prefix "$PYTHON_VERSION")/bin/python"
    [ -x "$PYTHON_BIN" ] || die "expected interpreter not found at ${PYTHON_BIN}"
    ok "interpreter ${PYTHON_BIN}"
}

ensure_python_pin() {
    step "Checking repository Python pin"
    if [ -f "$PIN_FILE" ] && [ "$(tr -d '[:space:]' <"$PIN_FILE")" = "$PYTHON_VERSION" ]; then
        ok "${PIN_FILE##*/} already pins ${PYTHON_VERSION}"
        return
    fi
    act "${PIN_FILE##*/} -> ${PYTHON_VERSION}"
    (cd "$REPO_ROOT" && pyenv local "$PYTHON_VERSION")
}

# ---------------------------------------------------------------------------
# uv and the project environment
# ---------------------------------------------------------------------------

ensure_uv() {
    step "Checking uv"
    if have uv; then
        ok "$(uv --version)"
        return
    fi
    ensure_brew_formula uv
    have uv || die "uv installed but not resolvable on PATH"
    ok "$(uv --version)"
}

# The interpreter path is passed explicitly and downloads are disabled, so uv
# can only ever use the pyenv build selected above.
# https://docs.astral.sh/uv/concepts/python-versions/
ensure_venv() {
    step "Checking virtual environment"
    if [ -x "${VENV_DIR}/bin/python" ]; then
        local current
        current="$("${VENV_DIR}/bin/python" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
        if [ "$current" = "$PYTHON_VERSION" ]; then
            ok ".venv already on ${PYTHON_VERSION}"
            return
        fi
        warn ".venv is on ${current:-unknown}, recreating on ${PYTHON_VERSION}"
    fi
    act ".venv"
    uv venv --python "$PYTHON_BIN" --no-python-downloads "$VENV_DIR"
}

sync_dependencies() {
    step "Syncing dependencies from uv.lock"
    [ -f "$LOCK_FILE" ] || die "uv.lock not found at ${LOCK_FILE}"

    local before after
    before="$(shasum -a 256 "$LOCK_FILE" | awk '{print $1}')"

    # --all-extras covers fastapi, quart, all, and dev from pyproject.toml.
    (cd "$REPO_ROOT" && uv sync --all-extras --python "$PYTHON_BIN" --no-python-downloads)

    after="$(shasum -a 256 "$LOCK_FILE" | awk '{print $1}')"
    if [ "$before" != "$after" ]; then
        warn "uv.lock was updated to match pyproject.toml; review and commit the change"
    else
        ok "uv.lock unchanged"
    fi
}

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

# 744 per the repo convention: owner runs them, nobody else needs to.
ensure_script_permissions() {
    step "Checking script permissions"
    local script
    for script in "${SCRIPT_DIR}"/*.sh; do
        [ -f "$script" ] || continue
        if [ "$(stat -f '%OLp' "$script")" = "744" ]; then
            ok "${script##*/} 744"
        else
            act "${script##*/} -> 744"
            chmod 744 "$script"
        fi
    done
}

verify_install() {
    step "Verifying the environment"
    local version
    version="$("${VENV_DIR}/bin/python" -c 'import auditry; print(auditry.__version__)')"
    ok "auditry ${version} imports cleanly"

    local tool
    for tool in pytest ruff mypy shellcheck shfmt; do
        if [ -x "${VENV_DIR}/bin/${tool}" ]; then
            ok "${tool} available"
        else
            warn "${tool} is missing from .venv/bin"
        fi
    done
}

run_tests() {
    [ "$RUN_TESTS" -eq 1 ] || return 0
    step "Running the test suite"
    # Via the script, so bootstrap and CI agree on what "the tests" means.
    local status=0
    "${SCRIPT_DIR}/runtests.sh" -q || status=$?
    if [ "$status" -ne 0 ]; then
        warn "pytest exited with status ${status}; the environment itself is still usable"
    fi
}

print_next_steps() {
    cat <<NEXT

==> Bootstrap complete

    Lint and format:
        ./scripts/lintme.sh              apply ruff's autofixes and formatting
        ./scripts/lintme.sh --check      report only, exactly what CI runs

    Tests:
        ./scripts/runtests.sh            the whole suite
        ./scripts/runtests.sh -k redaction

    Activate the environment to reach the tools directly:
        source .venv/bin/activate

NEXT
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"
    cd "$REPO_ROOT"

    require_macos
    ensure_command_line_tools
    ensure_homebrew
    ensure_build_deps
    ensure_pyenv
    ensure_shell_init
    resolve_python_version
    ensure_python_version
    ensure_python_pin
    ensure_uv
    ensure_venv
    sync_dependencies
    ensure_script_permissions
    verify_install
    run_tests
    print_next_steps
}

main "$@"
