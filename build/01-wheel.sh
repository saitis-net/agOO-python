#!/usr/bin/env bash
# 01-wheel.sh — Build the agoo Python wheel and source distribution.
#
# Output: dist/agoo-VERSION-py3-none-any.whl
#         dist/agoo-VERSION.tar.gz
#
# The wheel name follows PEP 427:
#   {name}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl
# agoo is pure Python so the tags are always py3-none-any.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV="$(mktemp -d)/build-wheel-venv"
trap 'rm -rf "$VENV"' EXIT

echo "[wheel] Creating build venv at $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip build

echo "[wheel] Building wheel and sdist"
"$VENV/bin/python" -m build --wheel --sdist --outdir dist/

echo "[wheel] Built:"
ls -lh dist/agoo-*.whl dist/agoo-*.tar.gz 2>/dev/null | sed 's/^/  /'
