#!/usr/bin/env bash
# build.sh — agOO-python build pipeline
#
# Produces:
#   dist/agoo-VERSION-py3-none-any.whl     Python wheel (pure-Python, any platform)
#   dist/agoo-VERSION.tar.gz               Python source distribution
#   dist/agOO-client-TUI.aarch64           Fully-static aarch64 binary
#   dist/agOO-client-TUI.amd64            Fully-static amd64 binary
#   dist/man/man1/agOO-client-TUI.1.gz    TUI man page (gzipped groff)
#   dist/man/man3/agoo.3.gz               Library man page (gzipped groff)
#   dist/install-agOO-client-TUI.sh       Self-extracting installer
#
# Usage:
#   ./build/build.sh                       Run all steps
#   ./build/build.sh wheel                 Step 01: Python wheel + sdist
#   ./build/build.sh aarch64               Step 02: static aarch64 binary
#   ./build/build.sh amd64                 Step 03: static amd64 binary
#   ./build/build.sh docs                  Step 04: man pages
#   ./build/build.sh installer             Step 05: self-extracting installer
#   ./build/build.sh wheel docs installer  Run selected steps in order
#
# Architecture support (steps 02 and 03):
#   Both steps run on both aarch64 and x86_64 hosts:
#
#   On aarch64 host:
#     step 02 (aarch64): native build
#     step 03 (amd64):   cross-build via box64 — requires box64 in PATH
#
#   On x86_64 host:
#     step 02 (aarch64): cross-build via qemu-aarch64-static
#                        requires: sudo apt install qemu-user-static
#                        for fully-static output: sudo apt install gcc-aarch64-linux-gnu
#     step 03 (amd64):   native build
#
#   If a required cross-build tool is missing the step exits with code 3
#   (shown as "skipped" in the summary rather than "failed").
#
# Environment:
#   AARCH64_PYTHON_TAR_URL  Override auto-detected python-build-standalone URL (step 02)
#   AMD64_PYTHON_TAR_URL    Override auto-detected python-build-standalone URL (step 03)
#
# Disk/time notes:
#   Cross-build steps download ~60 MB of CPython and use ~500 MB of build cache.
#   PyInstaller under emulation (box64 or QEMU) typically takes 5–15 minutes
#   on a Raspberry Pi 4/5 or a similarly-clocked machine.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# ANSI colours (suppressed when not a TTY)
if [ -t 1 ]; then
    C_BOLD='\033[1m'; C_GREEN='\033[0;32m'; C_CYAN='\033[0;36m'
    C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_RESET='\033[0m'
else
    C_BOLD=''; C_GREEN=''; C_CYAN=''; C_YELLOW=''; C_RED=''; C_RESET=''
fi

_log()  { printf "${C_CYAN}[build]${C_RESET} %s\n" "$*"; }
_ok()   { printf "${C_GREEN}[build] ✓ %s${C_RESET}\n" "$*"; }
_warn() { printf "${C_YELLOW}[build] ! %s${C_RESET}\n" "$*"; }
_err()  { printf "${C_RED}[build] ✗ %s${C_RESET}\n" "$*" >&2; }

_elapsed() {
    local s=$1
    printf '%dm%02ds' $((s/60)) $((s%60))
}

# Run one step script; returns 0 (success), 3 (skipped), or 1+ (failed).
# Exit code 3 from a step means "prerequisite not available, step voluntarily
# skipped" — build.sh shows it as skipped, not failed, and continues.
_run_step() {
    local name="$1" script="$2"
    local t0 rc
    t0=$(date +%s)
    printf "\n${C_BOLD}━━━ Step: %-12s ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RESET}\n" "$name"
    rc=0
    bash "$script" || rc=$?
    local t1; t1=$(date +%s)
    case $rc in
        0) _ok  "$name completed in $(_elapsed $((t1-t0)))" ;;
        3) _warn "$name skipped (prerequisite not available — see output above)" ;;
        *) _err  "$name FAILED (exit $rc)" ;;
    esac
    return $rc
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
ALL_STEPS=(wheel aarch64 amd64 docs installer)

declare -A STEP_MAP=(
    [wheel]="01-wheel.sh"
    [aarch64]="02-bin-aarch64.sh"
    [amd64]="03-bin-amd64.sh"
    [docs]="04-docs.sh"
    [installer]="05-installer.sh"
)

SELECTED=()

if [ $# -eq 0 ]; then
    SELECTED=("${ALL_STEPS[@]}")
else
    for arg in "$@"; do
        case "$arg" in
            -h|--help)
                sed -n '/^# build.sh/,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
                exit 0 ;;
            wheel|aarch64|amd64|docs|installer)
                SELECTED+=("$arg") ;;
            all)
                SELECTED=("${ALL_STEPS[@]}") ;;
            *)
                _err "Unknown step '$arg'. Valid steps: ${ALL_STEPS[*]}"
                exit 2 ;;
        esac
    done
fi

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
_log "Host architecture: $(uname -m)"
_log "Working directory: $(pwd)"
_log "Steps to run: ${SELECTED[*]}"

VERSION=$(python3 -c "
import tomllib, pathlib
with open('pyproject.toml','rb') as f:
    d = tomllib.load(f)
print(d['project']['version'])
" 2>/dev/null || \
python3 -c "
import re, pathlib
m = re.search(r'version\s*=\s*\"([^\"]+)\"', pathlib.Path('pyproject.toml').read_text())
print(m.group(1))
")
_log "Package version: $VERSION"

mkdir -p dist

# ---------------------------------------------------------------------------
# Run steps
# ---------------------------------------------------------------------------
T_START=$(date +%s)
FAILED=()
SKIPPED=()

for step in "${SELECTED[@]}"; do
    script="$SCRIPT_DIR/${STEP_MAP[$step]}"
    rc=0
    _run_step "$step" "$script" || rc=$?
    case $rc in
        0) ;;
        3) SKIPPED+=("$step") ;;
        *) FAILED+=("$step")
           _warn "Continuing with remaining steps" ;;
    esac
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
T_END=$(date +%s)
printf "\n${C_BOLD}━━━ Build summary (%s) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RESET}\n" \
    "$(_elapsed $((T_END - T_START)))"

_list_if_exists() {
    local f="$1"
    if [ -f "$f" ]; then
        printf "  ${C_GREEN}✓${C_RESET}  %-52s %s\n" "$f" "$(du -sh "$f" 2>/dev/null | cut -f1)"
    fi
}

_list_if_exists "dist/agoo-${VERSION}-py3-none-any.whl"
_list_if_exists "dist/agoo-${VERSION}.tar.gz"
_list_if_exists "dist/agOO-client-TUI.aarch64"
_list_if_exists "dist/agOO-client-TUI.amd64"
_list_if_exists "dist/man/man1/agOO-client-TUI.1.gz"
_list_if_exists "dist/man/man3/agoo.3.gz"
_list_if_exists "dist/install-agOO-client-TUI.sh"

if [ ${#SKIPPED[@]} -ne 0 ]; then
    printf "\n  ${C_YELLOW}skipped (prerequisite missing): %s${C_RESET}\n" "${SKIPPED[*]}"
fi

if [ ${#FAILED[@]} -ne 0 ]; then
    printf "\n  ${C_RED}FAILED steps: %s${C_RESET}\n" "${FAILED[*]}"
    exit 1
fi

if [ ${#SKIPPED[@]} -ne 0 ] && [ ${#FAILED[@]} -eq 0 ]; then
    printf "\n${C_YELLOW}Completed with skipped steps (see above for missing prerequisites).${C_RESET}\n"
else
    printf "\n${C_GREEN}All selected steps completed successfully.${C_RESET}\n"
fi
