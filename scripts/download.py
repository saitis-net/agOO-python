#!/usr/bin/env python3
"""download.py — Download a file from agOO temp storage to a local path.

Usage
-----
    python scripts/download.py <remote-path> [<local-output-path>]

If <local-output-path> is omitted the file is written to the current
directory using the basename of <remote-path>.

Example
-------
    # Download 'data/report.tar.gz' from agOO and save it locally:
    python scripts/download.py data/report.tar.gz

    # Save to an explicit path:
    python scripts/download.py data/report.tar.gz /tmp/report.tar.gz

The script exits with code 0 on success, 1 on any failure.

Credentials
-----------
Loaded from environment variables when set, otherwise fall back to the
defaults below.  To avoid storing the password in shell history, prefer:

    export agOO_PASSWORD=welcometoagoo
    python scripts/download.py <remote-path>

WARNING: hardcoded credentials below are provided for convenience during
development only.  Do not commit real passwords to version control.

NOTE: temp_get() loads the entire file into memory before writing it to
disk.  Avoid using this script for very large files until a streaming
variant is implemented (see changes.log — "Unchanged / Out of scope").
"""

import os
import sys

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agoo import Agoo

# ---------------------------------------------------------------------------
# Credentials — env vars take priority; literals are the fallback.
# ---------------------------------------------------------------------------
LOGIN    = os.environ.get("agOO_USER",     "thomas")
PASSWORD = os.environ.get("agOO_PASSWORD", "welcometoagoo")

def main() -> int:
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print(
            f"Usage: {sys.argv[0]} <remote-path> [<local-output-path>]",
            file=sys.stderr,
        )
        return 1

    remote_path = sys.argv[1]

    # Default output path: basename of the remote path, in the current directory.
    local_path = sys.argv[2] if len(sys.argv) == 3 else os.path.basename(remote_path)

    if not local_path:
        # remote_path had no basename component (e.g. it was just "/")
        print("Error: could not derive a local filename from the remote path.", file=sys.stderr)
        return 1

    if os.path.exists(local_path):
        print(f"Error: '{local_path}' already exists — refusing to overwrite.", file=sys.stderr)
        return 1

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

    print(f"Downloading '{remote_path}' from agOO…")
    content = client.temp_get(remote_path)
    if content is None:
        print(f"Download failed: {client.error()}", file=sys.stderr)
        return 1

    # Write the content to the local output file.
    # Using 'wb' with encode() preserves binary content faithfully; the
    # response.text from temp_get() is a str decoded by requests.
    try:
        with open(local_path, "wb") as fh:
            fh.write(content.encode("utf-8", errors="surrogateescape"))
    except OSError as exc:
        print(f"Error writing '{local_path}': {exc}", file=sys.stderr)
        return 1

    size = os.path.getsize(local_path)
    print(f"Saved '{remote_path}' → '{local_path}' ({size:,} bytes).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
