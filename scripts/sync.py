#!/usr/bin/env python3
"""sync.py — Trigger an async archive sync on the agOO service and wait for it.

Usage
-----
    python scripts/sync.py [--poll-interval SECONDS]

The script:
  1. Logs in.
  2. Calls async_synchronize(), which uploads the sentinel file that tells
     the agOO backend to start a migration job from temp to archive.
  3. Polls async_completed() every POLL_INTERVAL seconds until the backend
     reports the job is done (or the script is interrupted with Ctrl-C).

Exits with code 0 if the job reports status 0 (success),
       code 1 on any communication failure or non-zero job status.

Credentials
-----------
Loaded from environment variables when set, otherwise fall back to the
defaults below.  To avoid storing the password in shell history, prefer:

    export agOO_PASSWORD=welcometoagoo
    python scripts/sync.py

WARNING: hardcoded credentials below are provided for convenience during
development only.  Do not commit real passwords to version control.
"""

import os
import sys
import time
import argparse

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agoo import Agoo

# ---------------------------------------------------------------------------
# Credentials — env vars take priority; literals are the fallback.
# ---------------------------------------------------------------------------
LOGIN    = os.environ.get("agOO_USER",     "thomas")
PASSWORD = os.environ.get("agOO_PASSWORD", "welcometoagoo")

# How often to poll for job completion, in seconds.
DEFAULT_POLL_INTERVAL = 30

def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger and wait for an agOO archive sync.")
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        metavar="SECONDS",
        help=f"Seconds between completion polls (default: {DEFAULT_POLL_INTERVAL})",
    )
    args = parser.parse_args()

    client = Agoo(
        user=LOGIN,
        login=LOGIN,
        password=PASSWORD,
    )

    print(f"Authenticating as '{LOGIN}'…")
    if not client.login():
        print(f"Login failed: {client.error()}", file=sys.stderr)
        return 1
    print("Authenticated.")

    print("Triggering archive synchronisation…")
    job_id = client.async_synchronize()
    if job_id is None:
        print(f"Failed to trigger sync: {client.error()}", file=sys.stderr)
        return 1

    print(f"Sync job queued — ID: {job_id}")
    print(f"Polling for completion every {args.poll_interval}s (Ctrl-C to abort)…")

    try:
        while True:
            status = client.async_completed(job_id)

            if status is None:
                # None means the finished file does not exist yet — still running.
                print(f"  [{job_id}] still running…", flush=True)
                time.sleep(args.poll_interval)
                continue

            # Any integer value means the job has finished.
            if status == 0:
                print(f"  [{job_id}] completed successfully (status 0).")
                return 0
            else:
                print(
                    f"  [{job_id}] finished with non-zero status {status}.",
                    file=sys.stderr,
                )
                return 1

    except KeyboardInterrupt:
        print(f"\nAborted — job '{job_id}' may still be running on the server.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
