#!/usr/bin/env python3
"""download.py — Download a file from agOO temp or archive storage to a local path.

Overview
--------
Uses temp_get_file() which streams the response body directly from the
network to disk one chunk at a time.  Memory usage is bounded by the chunk
size (default 8 MiB) regardless of how large the remote file is — a 500 GB
archive and a 1 KB text file consume the same peak memory.

Archive recall
--------------
Files that have been migrated to tape archive are not directly downloadable
via the temp API.  Pass --wait to trigger an automatic recall: the script
calls schedule_unmigrate(), then polls until the file comes back online and
the download can proceed.  Use --poll-interval and --timeout to tune the
polling cadence and maximum wait time.

Usage
-----
    python scripts/download.py [options] <remote-path> [<local-output-path>]

    <remote-path>        Path to the file in agOO storage.
    <local-output-path>  Where to write the file locally.  Defaults to the
                         basename of <remote-path> in the current directory.

Options
-------
    --chunk-size BYTES   Network read size per iteration in bytes.
                         Default: 8388608 (8 MiB).  Increase on fast links
                         with large files; decrease in memory-constrained
                         environments.
    --wait               If the file is in archive, trigger a recall and wait
                         for it to come back online before downloading.
    --poll-interval N    Seconds between recall-completion polls (default: 30).
                         Only relevant when --wait is active.
    --timeout N          Maximum seconds to wait for a recall before giving up
                         (default: 3600).  Only relevant when --wait is active.

Examples
--------
    # Download a file that is currently in temp storage
    python scripts/download.py data/backup.tar.gz

    # Save to an explicit path
    python scripts/download.py data/backup.tar.gz /mnt/nas/backup.tar.gz

    # Recall from archive and wait up to one hour (the default)
    python scripts/download.py --wait eos/EOS-4.31.1F.swi

    # Recall from archive, poll every 5 minutes, give up after 2 hours
    python scripts/download.py --wait --poll-interval 300 --timeout 7200 eos/EOS-4.31.1F.swi

    # Use a smaller chunk size (e.g. on a Raspberry Pi with limited RAM)
    python scripts/download.py --chunk-size 1048576 data/backup.tar.gz

Exit codes
----------
    0   File downloaded and written to disk successfully.
    1   An error occurred (reason printed to stderr).

Credentials
-----------
Both agOO_USER and agOO_PASSWORD environment variables must be set.
The script exits immediately with a clear error if either is missing.
"""

import argparse
import os
import sys
import time

# Allow running directly from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agoo import Agoo

# ---------------------------------------------------------------------------
# Credentials — must be supplied via environment variables; no fallback.
# ---------------------------------------------------------------------------
_USER     = os.environ.get("agOO_USER")
_PASSWORD = os.environ.get("agOO_PASSWORD")

# Default streaming chunk size: 8 MiB.
# This controls how many bytes are held in memory at any one time during
# a download.  Larger values reduce per-chunk overhead on fast connections;
# smaller values reduce peak memory usage on constrained devices.
_DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024

# Default polling cadence and timeout for archive recalls.
_DEFAULT_POLL_INTERVAL = 30
_DEFAULT_TIMEOUT       = 3600


def _fmt_bytes(n: int) -> str:
    """Return a human-readable byte count, e.g. 1,234,567 (1.18 MB)."""
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= threshold:
            return f"{n:,} ({n / threshold:.2f} {unit})"
    return f"{n:,} bytes"


def _cleanup(path: str) -> None:
    """Remove a partially written local file, ignoring errors.

    Called whenever a download attempt fails mid-stream so we don't leave
    an incomplete file that could be mistaken for a successful download.
    """
    try:
        os.unlink(path)
    except OSError:
        pass


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
        description="Download a file from agOO temp or archive storage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "remote_path",
        metavar="REMOTE_PATH",
        help="Path to the file in agOO storage.",
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
    parser.add_argument(
        "--wait",
        action="store_true",
        help=(
            "If the file is in archive, trigger a recall and wait for it "
            "to come back online before downloading."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=_DEFAULT_POLL_INTERVAL,
        metavar="SECONDS",
        help=f"Seconds between recall-completion polls when --wait is active (default: {_DEFAULT_POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"Max seconds to wait for a recall before giving up (default: {_DEFAULT_TIMEOUT}).",
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

    try:
        # -----------------------------------------------------------------------
        # First download attempt.
        #
        # temp_get_file() streams the HTTP response directly to disk in chunks
        # of args.chunk_size bytes.  Peak memory usage is bounded by chunk_size
        # regardless of the file size.
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
            # Remove any partial data written before the failure.
            _cleanup(local_path)

            if not args.wait:
                # Default behaviour: fail immediately with a hint that --wait
                # can trigger an archive recall if the file has been migrated.
                print(
                    f"Download failed: {client.error()}\n"
                    "Hint: if the file is in archive storage, retry with --wait"
                    " to trigger an automatic recall.",
                    file=sys.stderr,
                )
                return 1

            # ---------------------------------------------------------------
            # Archive recall path (--wait was supplied).
            #
            # A 404 on the download usually means one of two things:
            #   (a) the file exists in archive (tape) but not in temp — recall
            #       it with schedule_unmigrate(), then poll until it is online.
            #   (b) the file does not exist at all — schedule_unmigrate() will
            #       also fail in this case, so we surface that error instead.
            # ---------------------------------------------------------------
            print("Download returned 404 — requesting archive recall…")
            if client.schedule_unmigrate(args.remote_path) is None:
                # Unmigrate failed: the file is most likely not on the server.
                print(
                    f"Recall failed (file may not exist): {client.error()}",
                    file=sys.stderr,
                )
                return 1

            # Recall was accepted.  Poll until the file comes back online,
            # retrying the full download on each tick rather than just
            # checking status — this avoids a separate stat/listing call and
            # means the download starts the moment the file is available.
            print(
                f"Recall queued. Polling every {args.poll_interval}s "
                f"(timeout: {args.timeout}s, Ctrl-C to abort)…"
            )
            deadline = time.monotonic() + args.timeout

            while time.monotonic() < deadline:
                time.sleep(args.poll_interval)

                ok = client.temp_get_file(
                    args.remote_path,
                    local_path,
                    chunk_size=args.chunk_size,
                )

                if ok:
                    # File came online and the download completed.
                    break

                # Still not available — clean up the partial file written
                # during this attempt and report progress to the operator.
                _cleanup(local_path)
                remaining = int(deadline - time.monotonic())
                print(f"  Still recalling… ({remaining}s remaining)", flush=True)
            else:
                print(
                    f"Timed out after {args.timeout}s waiting for archive recall.",
                    file=sys.stderr,
                )
                return 1

        # Report the final size as a sanity check for the operator.
        size = os.path.getsize(local_path)
        print(f"Saved '{args.remote_path}' → '{local_path}' ({_fmt_bytes(size)}).")
        return 0

    finally:
        client.logout()


if __name__ == "__main__":
    sys.exit(main())
