#!/usr/bin/env python3
"""upload.py — Upload one or more local files to agOO temp or archive storage.

Overview
--------
Handles any number of files, including sets that collectively exceed the
available cache space.  When the files do not all fit at once the script
relies on the library's batch_put() method, which automatically:

  1. Fits as many files as possible into the current available cache space.
  2. Uploads that batch.
  3. Triggers an archive sync (async_synchronize) and waits for it to finish,
     giving the server a chance to migrate the batch from cache to archive and
     reclaim cache space.
  4. Repeats from step 1 until every file has been uploaded.

For a single file that fits in the available cache the process completes
in one shot with no sync cycle.

Pre-existing files
------------------
Files that already exist on the server (in temp or archive) are silently
skipped by default.  Pass --force to overwrite them instead.

Usage
-----
    python scripts/upload.py [options] <file> [<file> ...]

Options
-------
    --poll-interval N   Seconds between sync-completion polls (default: 30).
                        Only relevant when a mid-upload sync cycle is needed.
    --force             Overwrite files that already exist on the server,
                        whether they are currently in temp or archive storage.

Examples
--------
    # Upload a single file (skipped silently if already on the server)
    python scripts/upload.py report.tar.gz

    # Force-overwrite existing copies in temp or archive
    python scripts/upload.py --force report.tar.gz

    # Upload multiple files; sync cycles fire automatically if needed
    python scripts/upload.py data/*.tar.gz

    # Override the default 30-second poll interval
    python scripts/upload.py --poll-interval 60 backup.tar

Exit codes
----------
    0   All files uploaded or skipped successfully.
    1   An error occurred (reason printed to stderr).

Credentials
-----------
Both agOO_USER and agOO_PASSWORD environment variables must be set.
The script exits immediately with a clear error if either is missing.
"""

import argparse
import os
import sys

# Allow running directly from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agoo import Agoo

# ---------------------------------------------------------------------------
# Credentials — must be supplied via environment variables; no fallback.
# ---------------------------------------------------------------------------
_USER     = os.environ.get("agOO_USER")
_PASSWORD = os.environ.get("agOO_PASSWORD")


def _fmt_bytes(n: int) -> str:
    """Return a human-readable byte count, e.g. 1,234,567 (1.18 MB)."""
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= threshold:
            return f"{n:,} ({n / threshold:.2f} {unit})"
    return f"{n:,} bytes"


def _print_usage(label: str, usage: dict) -> None:
    """Print a one-line cache usage summary to stdout."""
    total     = usage.get("total", 0)
    used      = usage.get("used", 0)
    available = total - used
    pct       = (used / total * 100) if total else 0
    print(
        f"{label}: "
        f"{_fmt_bytes(used)} used / "
        f"{_fmt_bytes(total)} total  "
        f"({pct:.1f}% full, {_fmt_bytes(available)} free)"
    )


def main() -> int:
    # -----------------------------------------------------------------------
    # Credential check — fail fast before touching the network or filesystem.
    # -----------------------------------------------------------------------
    missing = [name for name, val in (("agOO_USER", _USER), ("agOO_PASSWORD", _PASSWORD))
               if val is None]
    if missing:
        for name in missing:
            print(f"Error: environment variable {name} is not set.", file=sys.stderr)
        print("Export agOO_USER and agOO_PASSWORD before running this script.", file=sys.stderr)
        return 1

    # -----------------------------------------------------------------------
    # Argument parsing
    # -----------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        description="Upload local files to agOO temp storage, batching as needed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Exit codes")[0].strip(),  # reuse the module docstring
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="Local file path(s) to upload.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        metavar="SECONDS",
        help="Seconds between sync-completion polls when a cache flush is needed (default: 30).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite files that already exist on the server "
            "(in temp or archive storage)."
        ),
    )
    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Validate that every supplied path exists locally before touching the
    # network so the user gets immediate feedback on typos.
    # -----------------------------------------------------------------------
    bad = [f for f in args.files if not os.path.isfile(f)]
    if bad:
        for f in bad:
            print(f"Error: '{f}' is not a file or does not exist.", file=sys.stderr)
        return 1

    total_bytes = sum(os.path.getsize(f) for f in args.files)
    print(
        f"Files to upload : {len(args.files)}\n"
        f"Total size      : {_fmt_bytes(total_bytes)}"
    )

    # -----------------------------------------------------------------------
    # Authenticate
    # -----------------------------------------------------------------------
    # `login` is the credential username used in the POST /api/login form.
    # `user`  is the API namespace segment embedded in every URL path.
    client = Agoo(user=_USER, login="admin", password=_PASSWORD)

    print(f"\nAuthenticating as '{_USER}'…")
    if not client.login():
        print(f"Login failed: {client.error()}", file=sys.stderr)
        return 1
    print("Authenticated.")

    try:
        # -----------------------------------------------------------------------
        # Show current cache usage so the operator can see the available headroom
        # before the upload begins.
        # -----------------------------------------------------------------------
        usage = client.get_usage()
        if usage:
            print()
            _print_usage("Cache before upload", usage)

        # -----------------------------------------------------------------------
        # Upload — batch_put() handles all the complexity:
        #   • single file that fits  → one TUS upload, no sync
        #   • files that exceed cache → automatic batch + sync cycles
        #
        # override=True (--force) tells the server to replace any existing copy
        # of each file, whether it is currently in temp or archive storage.
        # Without --force, existing files are silently skipped (default).
        # -----------------------------------------------------------------------
        if args.force:
            print(f"\nUploading {len(args.files)} file(s) (--force: overwriting existing)…")
        else:
            print(f"\nUploading {len(args.files)} file(s) (existing files will be skipped)…")

        result = client.batch_put(
            args.files,
            poll_interval=args.poll_interval,
            override=args.force,
        )

        if result is None:
            print(f"\nUpload failed: {client.error()}", file=sys.stderr)
            return 1

        # -----------------------------------------------------------------------
        # Show updated cache usage after the upload completes.
        # -----------------------------------------------------------------------
        print("\nAll files uploaded successfully.")
        usage = client.get_usage()
        if usage:
            _print_usage("Cache after upload ", usage)

        return 0
    finally:
        client.logout()


if __name__ == "__main__":
    sys.exit(main())
