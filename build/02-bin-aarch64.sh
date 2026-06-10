#!/usr/bin/env bash
# 02-bin-aarch64.sh — Build a fully-static aarch64 binary of agOO-client-TUI.
#
# Native build (aarch64 host):
#   system python3 → throwaway venv → PyInstaller → staticx
#   staticx has no pre-built aarch64 wheel; its bootloader is compiled with scons.
#
# Cross-build (x86_64 host):
#   Requires qemu-aarch64-static — install: sudo apt install qemu-user-static
#   Downloads aarch64 CPython from astral-sh/python-build-standalone, runs
#   PyInstaller under QEMU emulation, then builds staticx.
#   For a fully-static result the cross-compiler is also needed:
#     sudo apt install gcc-aarch64-linux-gnu
#   Without it the binary links glibc dynamically (a warning is shown).
#   Override CPython download: export AARCH64_PYTHON_TAR_URL=<url>
#
# Output: dist/agOO-client-TUI.aarch64
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
# Download aarch64 CPython from python-build-standalone (cross-build only)
# ---------------------------------------------------------------------------
_pbs_download_aarch64() {
    local url="${AARCH64_PYTHON_TAR_URL:-}"
    if [ -z "$url" ]; then
        echo "[aarch64] Searching python-build-standalone for cpython-${PYVER}.x aarch64"
        url=$(curl -fsSL \
            "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=20" \
            | python3 -c "
import sys, json, re
releases = json.load(sys.stdin)
pat = re.compile(r'cpython-${PYVER}\.\d+\+\d+-aarch64-unknown-linux-gnu-install_only\.tar\.gz$')
for rel in releases:
    for asset in rel.get('assets', []):
        if pat.match(asset['name']):
            print(asset['browser_download_url'])
            sys.exit(0)
sys.exit(1)
") || {
            echo "[aarch64] ERROR: no python-build-standalone asset found for aarch64." >&2
            echo "[aarch64]   Set AARCH64_PYTHON_TAR_URL to a direct URL and re-run." >&2
            exit 1
        }
    fi
    echo "[aarch64] Downloading: $(basename "$url")"
    curl -fL --progress-bar "$url" -o "$WORK/cpython-aarch64.tar.gz"
    tar -xf "$WORK/cpython-aarch64.tar.gz" -C "$WORK"
    find "$WORK/python" -name "python3*" -type f | head -1
}

# ---------------------------------------------------------------------------
# Native aarch64 build
# ---------------------------------------------------------------------------
_build_native() {
    echo "[aarch64] Native build"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet pyinstaller requests

    echo "[aarch64] Running PyInstaller"
    "$VENV/bin/python3" -m PyInstaller \
        --onefile --name agOO-client-TUI \
        --distpath "$PI_OUT" --workpath "$WORK/pi-work" --specpath "$WORK/pi-spec" \
        --paths "$(pwd)" --collect-submodules agoo --noconfirm \
        tools/filebrowser.py

    echo "[aarch64] Installing staticx (compiles bootloader from source)"
    # scons builds the staticx bootloader C code.
    # patchelf 0.17 has an assertion bug on PyInstaller ELFs — pin to 0.14.
    # setuptools is required by staticx's legacy setup.py (not in venv by default).
    # All three must be on PATH before staticx's setup.py/scons run.
    "$VENV/bin/pip" install --quiet setuptools scons "patchelf==0.14.5.0"
    PATH="$VENV/bin:$PATH" \
    "$VENV/bin/pip" install --quiet --no-build-isolation staticx

    echo "[aarch64] Making binary fully static"
    PATH="$VENV/bin:$PATH" \
    "$VENV/bin/staticx" "$PI_OUT/agOO-client-TUI" dist/agOO-client-TUI.aarch64
}

# ---------------------------------------------------------------------------
# Cross-build from x86_64 via qemu-aarch64-static
# ---------------------------------------------------------------------------
_build_cross() {
    # Detect emulator
    local emu=""
    if [ -f /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
        echo "[aarch64] Using binfmt-registered QEMU (transparent)"
        emu=""
    elif command -v qemu-aarch64-static >/dev/null 2>&1; then
        echo "[aarch64] Using qemu-aarch64-static"
        emu="qemu-aarch64-static"
    else
        echo "[aarch64] ERROR: qemu-aarch64-static not found." >&2
        echo "[aarch64]   Install: sudo apt install qemu-user-static" >&2
        exit 3
    fi

    local aarch64_py
    aarch64_py=$(_pbs_download_aarch64)
    [ -n "$aarch64_py" ] || { echo "[aarch64] ERROR: aarch64 Python not found in archive" >&2; exit 1; }
    echo "[aarch64] aarch64 Python: $aarch64_py"

    echo "[aarch64] Creating aarch64 venv"
    $emu "$aarch64_py" -m venv --copies "$VENV"
    $emu "$VENV/bin/pip" install --quiet --upgrade pip
    $emu "$VENV/bin/pip" install --quiet pyinstaller requests

    echo "[aarch64] Running PyInstaller (under QEMU — may take several minutes)"
    $emu "$VENV/bin/python3" -m PyInstaller \
        --onefile --name agOO-client-TUI \
        --distpath "$PI_OUT" --workpath "$WORK/pi-work" --specpath "$WORK/pi-spec" \
        --paths "$(pwd)" --collect-submodules agoo --noconfirm \
        tools/filebrowser.py

    # staticx requires the aarch64 cross-compiler to build its bootloader on x86_64.
    # scons (running under QEMU) sees CC in the environment and uses the cross-compiler,
    # which runs natively on x86_64 and produces aarch64 object code.
    # scons is installed first so it is on PATH when staticx's setup.py invokes it.
    if command -v aarch64-linux-gnu-gcc >/dev/null 2>&1; then
        echo "[aarch64] Found aarch64-linux-gnu-gcc — building fully-static binary"
        $emu "$VENV/bin/pip" install --quiet setuptools scons "patchelf==0.14.5.0"
        CC=aarch64-linux-gnu-gcc \
        STRIP=aarch64-linux-gnu-strip \
        PATH="$VENV/bin:$PATH" \
        $emu "$VENV/bin/pip" install --quiet --no-build-isolation staticx
        CC=aarch64-linux-gnu-gcc \
        PATH="$VENV/bin:$PATH" \
        $emu "$VENV/bin/staticx" "$PI_OUT/agOO-client-TUI" dist/agOO-client-TUI.aarch64
    else
        echo "[aarch64] WARNING: aarch64-linux-gnu-gcc not found." >&2
        echo "[aarch64]   For a fully-static binary: sudo apt install gcc-aarch64-linux-gnu" >&2
        echo "[aarch64]   The binary will dynamically link glibc on the target." >&2
        cp "$PI_OUT/agOO-client-TUI" dist/agOO-client-TUI.aarch64
    fi
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "$HOST" in
    aarch64) _build_native ;;
    x86_64)  _build_cross ;;
    *)
        echo "[aarch64] ERROR: unsupported host architecture '$HOST'." >&2
        echo "[aarch64]   Supported hosts: aarch64 (native), x86_64 (cross via QEMU)" >&2
        exit 3
        ;;
esac

chmod +x dist/agOO-client-TUI.aarch64
echo "[aarch64] Verifying:"
file dist/agOO-client-TUI.aarch64 | sed 's/^/  /'
echo "[aarch64] Built: dist/agOO-client-TUI.aarch64 ($(du -sh dist/agOO-client-TUI.aarch64 | cut -f1))"
