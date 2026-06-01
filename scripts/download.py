#!/usr/bin/env python3
"""download.py — Download a file from agOO temp storage to a local path.

Overview
--------
Uses temp_get_file() which streams the response body directly from the
network to disk one chunk at a time.  Memory usage is bounded by the chunk
size (default 8 MiB) regardless of how large the remote file is — a 500 GB
archive and a 1 KB text file consume the same peak memory.

Usage
-----
    python scripts/download.py [options] <remote-path> [<local-output-path>]

    <remote-path>        Path to the file in agOO temp storage.
    <local-output-path>  Where to write the file locally.  Defaults to the
                         basename of <remote-path> in the current directory.

Options
-------
    --chunk-size BYTES   Network read size per iteration in bytes.
                         Default: 8388608 (8 MiB).  Increase on fast links
                         with large files; decrease in memory-constrained
                         environments.

Examples
--------
    # Download using the default chunk size
    python scripts/download.py data/backup.tar.gz

    # Save to an explicit path
    python scripts/download.py data/backup.tar.gz /mnt/nas/backup.tar.gz

    # Use a smaller chunk size (e.g. on a Raspberry Pi with limited RAM)
    python scripts/download.py --chunk-size 1048576 data/backup.tar.gz

Exit codes
----------
    0   File downloaded and written to disk successfully.
    1   An error occurred (reason printed to stderr).

Credentials
-----------
Resolved in this order (first match wins):
  1. agOO_USER / agOO_PASSWORD environment variables  (recommended for scripts)
  2. Hard-coded defaults below                        (development convenience)

WARNING: hard-coded passwords are provided for development convenience only.
         Never commit real credentials to version control.
"""

import argparse
import os
import sys

# Allow running directly from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agoo import Agoo

# ---------------------------------------------------------------------------
# Credentials — environment variables take priority over the hard-coded values.
# ---------------------------------------------------------------------------
_USER     = os.environ.get("agOO_USER",     "thomas")   # API namespace / user
_PASSWORD = os.environ.get("agOO_PASSWORD", "welcometoagoo")

# Default streaming chunk size: 8 MiB.
# This controls how many bytes are held in memory at any one time during
# a download.  Larger values reduce per-chunk overhead on fast connections;
# smaller values reduce peak memory usage on constrained devices.
_DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024


def _fmt_bytes(n: int) -> str:
    """Return a human-readable byte count, e.g. 1,234,567 (1.18 MB)."""
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= threshold:
            return f"{n:,} ({n / threshold:.2f} {unit})"
    return f"{n:,} bytes"


def main() -> int:
    # -----------------------------------------------------------------------
    # Argument parsing
    # -----------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        description="Download a file from agOO temp storage to a local path.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "remote_path",
        metavar="REMOTE_PATH",
        help="Path to the file in agOO temp storage.",
    )
    parser.add_argument(
        "local_path",
        metavar="LOCAL_PATH",
        nargs="?",    # optional; derived from remote_path if omitted
        default=None,
        help="Local path to write the file to (default: basename of REMOTE_PATH).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=_DEFAULT_CHUNK_SIZE,
        metavar="BYTES",
        help=f"Streaming chunk size in bytes (default: {_DEFAULT_CHUNK_SIZE:,}).",
    )
    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Resolve the local output path.
    # -----------------------------------------------------------------------
    local_path = args.local_path or os.path.basename(args.remote_path)

    if not local_path:
        # remote_path had no basename component (e.g. it was just "/").
        print(
            "Error: cannot derive a local filename from the remote path. "
            "Please supply an explicit local output path.",
            file=sys.stderr,
        )
        return 1

    # Refuse to overwrite an existing file — the operator should make
    # an explicit choice rather than silently losing their data.
    if os.path.exists(local_path):
        print(
            f"Error: '{local_path}' already exists — refusing to overwrite.",
            file=sys.stderr,
        )
        return 1

    # -----------------------------------------------------------------------
    # Authenticate
    # -----------------------------------------------------------------------
    # `login` is the credential username used in the POST /api/login form.
    # `user`  is the API namespace segment embedded in every URL path.
    client = Agoo(user=_USER, login="admin", password=_PASSWORD)

    print(f"Authenticating as '{_USER}'…")
    if not client.login():
        print(f"Login failed: {client.error()}", file=sys.stderr)
        return 1
    print("Authenticated.")

    # -----------------------------------------------------------------------
    # Download via streaming — no memory exhaustion regardless of file size.
    #
    # temp_get_file() opens the local file, then reads the HTTP response in
    # chunks of args.chunk_size bytes, writing each chunk to disk before
    # requesting the next.  Peak memory usage is bounded by chunk_size.
    # -----------------------------------------------------------------------
    print(
        f"Downloading '{args.remote_path}' "
        f"(chunk size: {_fmt_bytes(args.chunk_size)})…"
    )
    ok = client.temp_get_file(
        args.remote_path,
        local_path,
        chunk_size=args.chunk_size,
    )

    if ok is None:
        # Clean up the partially written local file so we don't leave
        # incomplete data behind that could be mistaken for a full download.
        try:
            os.unlink(local_path)
        except OSError:
            pass
        print(f"Download failed: {client.error()}", file=sys.stderr)
        return 1

    # Report the final size as a sanity check for the operator.
    size = os.path.getsize(local_path)
    print(f"Saved '{args.remote_path}' → '{local_path}' ({_fmt_bytes(size)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
