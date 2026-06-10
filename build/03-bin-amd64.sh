#!/usr/bin/env bash
# 03-bin-amd64.sh — Build a fully-static amd64 (x86_64) binary of agOO-client-TUI.
#
# Native build (x86_64 host):
#   system python3 → throwaway venv → PyInstaller → staticx
#   staticx ships a pre-built x86_64 wheel; no C compiler needed.
#
# Cross-build (aarch64 host):
#   PyInstaller cannot cross-compile, so an amd64 CPython build is downloaded
#   from astral-sh/python-build-standalone and run under box64 emulation.
#   Requires box64 — https://github.com/ptitSeb/box64 (typically /usr/local/bin/box64)
#   On amd64 staticx also ships a pre-built wheel, so no C compiler is needed here.
#   Override CPython download: export AMD64_PYTHON_TAR_URL=<url>
#
# Output: dist/agOO-client-TUI.amd64
# Exit 3: prerequisite not available (build.sh treats this as "skipped")
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=$(uname -m)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
VENV="$WORK/venv"
PI_OUT="$WORK/dist"
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")

# ---------------------------------------------------------------------------
# Download amd64 CPython from python-build-standalone (cross-build only)
# ---------------------------------------------------------------------------
_pbs_download_amd64() {
    local url="${AMD64_PYTHON_TAR_URL:-}"
    if [ -z "$url" ]; then
        echo "[amd64] Searching python-build-standalone for cpython-${PYVER}.x amd64"
        url=$(curl -fsSL \
            "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=20" \
            | python3 -c "
import sys, json, re
releases = json.load(sys.stdin)
pat = re.compile(r'cpython-${PYVER}\.\d+\+\d+-x86_64-unknown-linux-gnu-install_only\.tar\.gz$')
for rel in releases:
    for asset in rel.get('assets', []):
        if pat.match(asset['name']):
            print(asset['browser_download_url'])
            sys.exit(0)
sys.exit(1)
") || {
            echo "[amd64] ERROR: no python-build-standalone asset found for amd64." >&2
            echo "[amd64]   Set AMD64_PYTHON_TAR_URL to a direct URL and re-run." >&2
            exit 1
        }
    fi
    echo "[amd64] Downloading: $(basename "$url")"
    curl -fL --progress-bar "$url" -o "$WORK/cpython-amd64.tar.gz"
    tar -xf "$WORK/cpython-amd64.tar.gz" -C "$WORK"
    find "$WORK/python" -name "python3*" -type f | head -1
}

# ---------------------------------------------------------------------------
# Native x86_64 build
# ---------------------------------------------------------------------------
_build_native() {
    echo "[amd64] Native build"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip

    # staticx ships pre-built x86_64 wheels; --only-binary avoids source builds.
    # patchelf 0.17 has an assertion bug on PyInstaller ELFs — pin to 0.14.
    "$VENV/bin/pip" install --quiet --only-binary=:all: pyinstaller requests staticx "patchelf==0.14.5.0"

    echo "[amd64] Running PyInstaller"
    "$VENV/bin/python3" -m PyInstaller \
        --onefile --name agOO-client-TUI \
        --distpath "$PI_OUT" --workpath "$WORK/pi-work" --specpath "$WORK/pi-spec" \
        --paths "$(pwd)" --collect-submodules agoo --noconfirm \
        tools/filebrowser.py

    echo "[amd64] Making binary fully static"
    # patchelf (from the patchelf wheel) lives in the venv bin — add it to PATH.
    PATH="$VENV/bin:$PATH" \
    "$VENV/bin/staticx" "$PI_OUT/agOO-client-TUI" dist/agOO-client-TUI.amd64
}

# ---------------------------------------------------------------------------
# Cross-build from aarch64 via box64 emulation
# ---------------------------------------------------------------------------
_build_cross() {
    if ! command -v box64 >/dev/null 2>&1; then
        echo "[amd64] ERROR: box64 not found." >&2
        echo "[amd64]   box64 is required to run amd64 binaries on aarch64." >&2
        echo "[amd64]   See https://github.com/ptitSeb/box64" >&2
        exit 3
    fi
    echo "[amd64] Cross-build from aarch64 via box64"

    local amd64_py
    amd64_py=$(_pbs_download_amd64)
    [ -n "$amd64_py" ] || { echo "[amd64] ERROR: amd64 Python not found in archive" >&2; exit 1; }
    echo "[amd64] amd64 Python: $amd64_py"

    echo "[amd64] Creating amd64 venv under box64"
    box64 "$amd64_py" -m venv --copies "$VENV"
    box64 "$VENV/bin/pip" install --quiet --upgrade pip

    # staticx ships pre-built amd64 wheels — no C compiler or scons needed.
    # patchelf 0.17 has an assertion bug on PyInstaller ELFs — pin to 0.14.
    echo "[amd64] Installing PyInstaller, requests, staticx (pre-built amd64 wheels)"
    box64 "$VENV/bin/pip" install --quiet --only-binary=:all: pyinstaller requests staticx "patchelf==0.14.5.0"

    echo "[amd64] Running PyInstaller (under box64 — may take several minutes)"
    box64 "$VENV/bin/python3" -m PyInstaller \
        --onefile --name agOO-client-TUI \
        --distpath "$PI_OUT" --workpath "$WORK/pi-work" --specpath "$WORK/pi-spec" \
        --paths "$(pwd)" --collect-submodules agoo --noconfirm \
        tools/filebrowser.py

    echo "[amd64] Making binary fully static"
    PATH="$VENV/bin:$PATH" \
    box64 "$VENV/bin/staticx" "$PI_OUT/agOO-client-TUI" dist/agOO-client-TUI.amd64
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "$HOST" in
    x86_64)  _build_native ;;
    aarch64) _build_cross ;;
    *)
        echo "[amd64] ERROR: unsupported host architecture '$HOST'." >&2
        echo "[amd64]   Supported hosts: x86_64 (native), aarch64 (cross via box64)" >&2
        exit 3
        ;;
esac

chmod +x dist/agOO-client-TUI.amd64
echo "[amd64] Verifying:"
file dist/agOO-client-TUI.amd64 | sed 's/^/  /'
echo "[amd64] Built: dist/agOO-client-TUI.amd64 ($(du -sh dist/agOO-client-TUI.amd64 | cut -f1))"
