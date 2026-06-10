#!/usr/bin/env bash
# 02-bin-aarch64.sh — Build a fully-static aarch64 binary of agOO-client-TUI.
#
# Must run natively on an aarch64 host.
# Output: dist/agOO-client-TUI.aarch64
#
# staticx has no pre-built aarch64 wheel; its bootloader is compiled from
# source using scons. libc.a must be present (glibc-static or equivalent).
set -euo pipefail
cd "$(dirname "$0")/.."

ARCH=$(uname -m)
if [ "$ARCH" != "aarch64" ]; then
    echo "[aarch64] ERROR: must run on aarch64 host (detected: $ARCH)" >&2
    exit 1
fi

VENV="$(mktemp -d)/agoo-build-aarch64"
trap 'rm -rf "$(dirname "$VENV")"' EXIT

echo "[aarch64] Creating build venv at $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip

echo "[aarch64] Installing PyInstaller and runtime deps"
"$VENV/bin/pip" install --quiet pyinstaller requests

echo "[aarch64] Running PyInstaller"
"$VENV/bin/pyinstaller" \
    --onefile \
    --name agOO-client-TUI \
    --distpath /tmp/agoo-pyinstaller-aarch64 \
    --workpath /tmp/agoo-pyinstaller-aarch64-work \
    --specpath /tmp/agoo-pyinstaller-aarch64-spec \
    --paths . \
    --collect-submodules agoo \
    --noconfirm \
    tools/filebrowser.py

echo "[aarch64] Installing staticx (compiles bootloader from source)"
# scons is required to compile the staticx bootloader on aarch64.
"$VENV/bin/pip" install --quiet --no-build-isolation scons staticx patchelf-wrapper

echo "[aarch64] Making binary fully static with staticx"
"$VENV/bin/staticx" \
    /tmp/agoo-pyinstaller-aarch64/agOO-client-TUI \
    dist/agOO-client-TUI.aarch64

chmod +x dist/agOO-client-TUI.aarch64

echo "[aarch64] Verifying static linkage:"
ldd dist/agOO-client-TUI.aarch64 2>&1 | sed 's/^/  /'
echo "[aarch64] Built: dist/agOO-client-TUI.aarch64 ($(du -sh dist/agOO-client-TUI.aarch64 | cut -f1))"
