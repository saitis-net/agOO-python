#!/usr/bin/env bash
# 03-bin-amd64.sh — Cross-build a fully-static amd64 binary of agOO-client-TUI
#                   from an aarch64 host using box64 emulation.
#
# PyInstaller cannot cross-compile, so we run an amd64 CPython build under
# box64 (registered via binfmt). box64 must be installed at /usr/local/bin.
#
# The amd64 CPython is downloaded from astral-sh/python-build-standalone.
# On amd64, staticx ships a pre-built wheel so no C compiler is needed.
#
# Output: dist/agOO-client-TUI.amd64
set -euo pipefail
cd "$(dirname "$0")/.."

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
if ! command -v box64 >/dev/null 2>&1; then
    echo "[amd64] ERROR: box64 not found. Install it and register binfmt." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Download amd64 CPython matching the host Python version
# ---------------------------------------------------------------------------
HOST_PY=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
PYVER_SHORT=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[amd64] Host Python: $HOST_PY  →  fetching amd64 cpython $PYVER_SHORT"

# python-build-standalone release tag — prefer a dated release matching the
# minor version. We query the GitHub API for the latest release that contains
# a matching cpython tarball.
AMD64_PYTHON_DIR=$(mktemp -d)
AMD64_VENV="$AMD64_PYTHON_DIR/venv"
AMD64_PYINSTALLER_OUT="$AMD64_PYTHON_DIR/dist"
trap 'rm -rf "$AMD64_PYTHON_DIR"' EXIT

ASSET_NAME="cpython-${HOST_PY}+$(date +%Y%m%d)-x86_64-unknown-linux-gnu-install_only.tar.gz"
PBS_RELEASES_URL="https://api.github.com/repos/astral-sh/python-build-standalone/releases"

echo "[amd64] Searching python-build-standalone for cpython-${PYVER_SHORT}.x amd64"

# Find the most recent release containing our minor-version asset.
DOWNLOAD_URL=$(curl -fsSL "$PBS_RELEASES_URL?per_page=20" 2>/dev/null | \
    python3 -c "
import sys, json, re
releases = json.load(sys.stdin)
pat = re.compile(r'cpython-${PYVER_SHORT}\.\d+\+\d+-x86_64-unknown-linux-gnu-install_only\.tar\.gz$')
for rel in releases:
    for asset in rel.get('assets', []):
        if pat.match(asset['name']):
            print(asset['browser_download_url'])
            sys.exit(0)
sys.exit(1)
" 2>/dev/null) || true

if [ -z "$DOWNLOAD_URL" ]; then
    echo "[amd64] Could not auto-detect python-build-standalone URL." >&2
    echo "[amd64] Set AMD64_PYTHON_TAR_URL to a direct download URL and re-run." >&2
    echo "[amd64] Example:" >&2
    echo "[amd64]   export AMD64_PYTHON_TAR_URL=https://github.com/astral-sh/python-build-standalone/releases/download/YYYYMMDD/cpython-${PYVER_SHORT}.x+YYYYMMDD-x86_64-unknown-linux-gnu-install_only.tar.gz" >&2
    exit 1
fi

echo "[amd64] Downloading: $(basename "$DOWNLOAD_URL")"
AMD64_TAR="$AMD64_PYTHON_DIR/cpython-amd64.tar.gz"
curl -fL --progress-bar "$DOWNLOAD_URL" -o "$AMD64_TAR"

echo "[amd64] Extracting amd64 CPython"
tar -xf "$AMD64_TAR" -C "$AMD64_PYTHON_DIR"
AMD64_PY=$(find "$AMD64_PYTHON_DIR/python" -name "python3*" -type f | head -1)
if [ -z "$AMD64_PY" ]; then
    echo "[amd64] ERROR: could not find python binary in extracted archive" >&2
    exit 1
fi
echo "[amd64] amd64 python binary: $AMD64_PY"

# ---------------------------------------------------------------------------
# Create venv and install deps under box64
# ---------------------------------------------------------------------------
echo "[amd64] Creating amd64 venv under box64"
box64 "$AMD64_PY" -m venv --copies "$AMD64_VENV"
AMD64_VENV_PY="$AMD64_VENV/bin/python3"
AMD64_VENV_PIP="$AMD64_VENV/bin/pip"

echo "[amd64] Installing PyInstaller, requests, staticx (amd64 has pre-built wheels)"
box64 "$AMD64_VENV_PIP" install --quiet --upgrade pip
box64 "$AMD64_VENV_PIP" install --quiet \
    --only-binary=:all: \
    pyinstaller requests staticx patchelf

# ---------------------------------------------------------------------------
# PyInstaller (slow under emulation — can take several minutes)
# ---------------------------------------------------------------------------
echo "[amd64] Running PyInstaller (under box64, this may take a few minutes)"
box64 "$AMD64_VENV_PY" -m PyInstaller \
    --onefile \
    --name agOO-client-TUI \
    --distpath "$AMD64_PYINSTALLER_OUT" \
    --workpath "$AMD64_PYTHON_DIR/pyinstaller-work" \
    --specpath "$AMD64_PYTHON_DIR/pyinstaller-spec" \
    --paths "$(pwd)" \
    --collect-submodules agoo \
    --noconfirm \
    tools/filebrowser.py

# ---------------------------------------------------------------------------
# staticx — make fully static
# ---------------------------------------------------------------------------
echo "[amd64] Making binary fully static with staticx"
box64 "$AMD64_VENV_PY" -m staticx \
    "$AMD64_PYINSTALLER_OUT/agOO-client-TUI" \
    dist/agOO-client-TUI.amd64 2>/dev/null || \
box64 "$AMD64_VENV/bin/staticx" \
    "$AMD64_PYINSTALLER_OUT/agOO-client-TUI" \
    dist/agOO-client-TUI.amd64

chmod +x dist/agOO-client-TUI.amd64

echo "[amd64] Verifying static linkage:"
file dist/agOO-client-TUI.amd64 | sed 's/^/  /'
echo "[amd64] Built: dist/agOO-client-TUI.amd64 ($(du -sh dist/agOO-client-TUI.amd64 | cut -f1))"
