#!/usr/bin/env bash
# 05-installer.sh — Assemble the self-extracting installer for agOO-client-TUI.
#
# The installer is a POSIX shell script with an appended gzip-compressed tar
# archive containing:
#   agOO-client-TUI.aarch64          — static aarch64 binary
#   agOO-client-TUI.amd64            — static amd64 binary
#   man/man1/agOO-client-TUI.1.gz    — TUI man page
#   man/man3/agoo.3.gz               — library man page
#
# When executed, the installer:
#   1. Detects the host architecture.
#   2. Extracts the matching binary to PREFIX/bin/agOO-client-TUI.
#   3. Installs the man pages to PREFIX/share/man/.
#   4. Runs mandb/makewhatis if available.
#
# Output: dist/install-agOO-client-TUI.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
MISSING=()
[ -f dist/agOO-client-TUI.aarch64 ] || MISSING+=("dist/agOO-client-TUI.aarch64")
[ -f dist/agOO-client-TUI.amd64   ] || MISSING+=("dist/agOO-client-TUI.amd64")
[ -f dist/man/man1/agOO-client-TUI.1.gz ] || MISSING+=("dist/man/man1/agOO-client-TUI.1.gz")
[ -f dist/man/man3/agoo.3.gz            ] || MISSING+=("dist/man/man3/agoo.3.gz")

if [ ${#MISSING[@]} -ne 0 ]; then
    echo "[installer] ERROR: missing required files:" >&2
    printf '[installer]   %s\n' "${MISSING[@]}" >&2
    echo "[installer] Run steps 02, 03, and 04 first." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Shell header for the self-extracting installer
# ---------------------------------------------------------------------------
read -r -d '' HEADER <<'SCRIPT_EOF' || true
#!/bin/sh
# agOO-client-TUI installer — self-extracting POSIX shell script.
#
# Usage:
#   sh install-agOO-client-TUI.sh             # install to /usr/local
#   PREFIX=/opt/agoo sh install-agOO-client-TUI.sh
#   sh install-agOO-client-TUI.sh --help
#   sh install-agOO-client-TUI.sh --uninstall
#
# The payload (a gzip-compressed tar archive containing both architecture
# binaries and gzipped man pages) is appended below the __PAYLOAD_BELOW__
# marker. The installer extracts only the files appropriate for the current
# architecture.
set -eu

PREFIX="${PREFIX:-/usr/local}"
BINDIR="$PREFIX/bin"
MANDIR="$PREFIX/share/man"
TARGET="$BINDIR/agOO-client-TUI"

usage() {
    cat <<EOF
agOO-client-TUI installer

  Installs the agOO-client-TUI static binary and man pages for the
  current architecture.

  Install locations (override with PREFIX=/some/where):
    Binary  : $BINDIR/agOO-client-TUI
    Man page: $MANDIR/man1/agOO-client-TUI.1.gz
              $MANDIR/man3/agoo.3.gz

Options:
  -h, --help        Show this help and exit.
  --uninstall       Remove previously installed files.
EOF
}

# --uninstall: remove all installed files.
do_uninstall() {
    echo "Uninstalling agOO-client-TUI..."
    elev=""
    if [ ! -w "$BINDIR" ] && [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
        elev="sudo"
    fi
    for f in \
        "$TARGET" \
        "$MANDIR/man1/agOO-client-TUI.1.gz" \
        "$MANDIR/man3/agoo.3.gz"
    do
        if [ -f "$f" ]; then
            $elev rm -f "$f"
            echo "  removed $f"
        fi
    done
    if command -v mandb >/dev/null 2>&1; then
        $elev mandb --quiet 2>/dev/null || true
    elif command -v makewhatis >/dev/null 2>&1; then
        $elev makewhatis "$MANDIR" 2>/dev/null || true
    fi
    echo "Done."
    exit 0
}

case "${1:-}" in
    -h|--help)      usage; exit 0 ;;
    --uninstall)    do_uninstall ;;
    "")             ;;
    *)  echo "install: unknown argument '$1' (try --help)" >&2; exit 2 ;;
esac

# The payload must be read from a real file (not a pipe).
self="$0"
if [ ! -f "$self" ] || [ ! -r "$self" ]; then
    echo "install: cannot read self ('$self')." >&2
    echo "Download the installer to a local file and run it (not via a pipe)." >&2
    exit 1
fi

# Map machine type to embedded binary name.
machine=$(uname -m)
case "$machine" in
    x86_64|amd64)  bin_member="agOO-client-TUI.amd64"   ;;
    aarch64|arm64) bin_member="agOO-client-TUI.aarch64" ;;
    *)
        echo "install: unsupported architecture '$machine'." >&2
        echo "  Supported: aarch64 (arm64) and x86_64 (amd64)." >&2
        exit 1 ;;
esac
echo "Architecture: $machine → $bin_member"

# Locate the first line after the payload marker.
payload_line=$(awk '/^__PAYLOAD_BELOW__$/{print NR+1; exit}' "$self")
if [ -z "${payload_line:-}" ]; then
    echo "install: payload marker not found — corrupt installer?" >&2
    exit 1
fi

# Choose an elevation command if the target directories are not writable.
elev=""
# Walk up from BINDIR to find the highest ancestor that actually exists.
need_dir="$BINDIR"
while [ ! -d "$need_dir" ]; do
    need_dir=$(dirname "$need_dir")
done
if [ ! -w "$need_dir" ] || { [ -e "$TARGET" ] && [ ! -w "$TARGET" ]; }; then
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            elev="sudo"
            echo "Note: $BINDIR is not writable; using sudo."
        else
            echo "install: $BINDIR is not writable and sudo is not available." >&2
            echo "  Run as root or set PREFIX to a writable location." >&2
            exit 1
        fi
    fi
fi

# Extract payload into a temp directory.
TMPDIR_INST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_INST"' EXIT INT TERM

echo "Extracting payload..."
tail -n +"$payload_line" "$self" | gzip -d | tar -x -C "$TMPDIR_INST"

# Install binary.
$elev mkdir -p "$BINDIR"
$elev install -m 0755 "$TMPDIR_INST/$bin_member" "$TARGET"
echo "  installed $TARGET"

# Install man pages.
$elev mkdir -p "$MANDIR/man1" "$MANDIR/man3"
$elev install -m 0644 "$TMPDIR_INST/man/man1/agOO-client-TUI.1.gz" "$MANDIR/man1/"
$elev install -m 0644 "$TMPDIR_INST/man/man3/agoo.3.gz"            "$MANDIR/man3/"
echo "  installed $MANDIR/man1/agOO-client-TUI.1.gz"
echo "  installed $MANDIR/man3/agoo.3.gz"

# Update the man-page index.
if command -v mandb >/dev/null 2>&1; then
    $elev mandb --quiet 2>/dev/null && echo "  mandb updated" || true
elif command -v makewhatis >/dev/null 2>&1; then
    $elev makewhatis "$MANDIR" 2>/dev/null && echo "  makewhatis updated" || true
fi

echo ""
echo "agOO-client-TUI installed successfully."
echo "  Run:  agOO-client-TUI"
echo "  Docs: man agOO-client-TUI"
exit 0
__PAYLOAD_BELOW__
SCRIPT_EOF

# ---------------------------------------------------------------------------
# Build the payload tarball
# ---------------------------------------------------------------------------
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

echo "[installer] Staging payload"
cp dist/agOO-client-TUI.aarch64          "$STAGING/"
cp dist/agOO-client-TUI.amd64            "$STAGING/"
mkdir -p "$STAGING/man/man1" "$STAGING/man/man3"
cp dist/man/man1/agOO-client-TUI.1.gz   "$STAGING/man/man1/"
cp dist/man/man3/agoo.3.gz              "$STAGING/man/man3/"

echo "[installer] Compressing payload"
PAYLOAD_TGZ=$(mktemp)
trap 'rm -rf "$STAGING" "$PAYLOAD_TGZ"' EXIT
tar -C "$STAGING" -czf "$PAYLOAD_TGZ" .

# ---------------------------------------------------------------------------
# Write the self-extracting installer:
# header (text) + newline + binary payload — never via a shell variable so
# null bytes in the binary data are preserved exactly.
# ---------------------------------------------------------------------------
OUT="dist/install-agOO-client-TUI.sh"
printf '%s\n' "$HEADER" > "$OUT"
cat "$PAYLOAD_TGZ" >> "$OUT"
chmod +x "$OUT"

echo "[installer] Built: $OUT ($(du -sh "$OUT" | cut -f1))"
