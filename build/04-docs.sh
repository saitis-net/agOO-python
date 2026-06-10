#!/usr/bin/env bash
# 04-docs.sh — Generate gzipped man pages from RST sources.
#
# Sources : docs/man/agOO-client-TUI.1.rst  → dist/man/man1/agOO-client-TUI.1.gz
#           docs/man/agoo.3.rst             → dist/man/man3/agoo.3.gz
#
# Requires rst2man (python3-docutils package).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v rst2man >/dev/null 2>&1 && ! command -v rst2man.py >/dev/null 2>&1; then
    echo "[docs] ERROR: rst2man not found." >&2
    echo "[docs]   Install it with:  sudo apt-get install python3-docutils" >&2
    exit 1
fi

RST2MAN=$(command -v rst2man 2>/dev/null || command -v rst2man.py)

mkdir -p dist/man/man1 dist/man/man3

_build_page() {
    local src="$1" dst="$2" section="$3"
    echo "[docs] $src → $dst"
    "$RST2MAN" --no-datestamp "$src" | gzip -9 > "$dst"
}

_build_page \
    docs/man/agOO-client-TUI.1.rst \
    dist/man/man1/agOO-client-TUI.1.gz \
    1

_build_page \
    docs/man/agoo.3.rst \
    dist/man/man3/agoo.3.gz \
    3

echo "[docs] Man pages:"
ls -lh dist/man/man1/ dist/man/man3/ | sed 's/^/  /'
