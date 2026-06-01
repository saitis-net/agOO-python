#!/usr/bin/env python3
"""upload.py — Upload a local file to agOO temp storage.

Usage
-----
    python scripts/upload.py <local-file-path>

The remote path mirrors the local path exactly (same relative name).
The script exits with code 0 on success, 1 on any failure.

Credentials
-----------
Loaded from environment variables when set, otherwise fall back to the
defaults below.  To avoid storing the password in shell history, prefer:

    export agOO_PASSWORD=welcometoagoo
    python scripts/upload.py myfile.tar.gz

WARNING: hardcoded credentials below are provided for convenience during
development only.  Do not commit real passwords to version control.
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
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <local-file-path>", file=sys.stderr)
        return 1

    local_path = sys.argv[1]

    if not os.path.isfile(local_path):
        print(f"Error: '{local_path}' is not a file or does not exist.", file=sys.stderr)
        return 1

    # Instantiate the client.  `login` is the agOO username used in the login
    # form; `user` is the API namespace segment that appears in every URL.
    # Both are "thomas" for this account.
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

    print(f"Uploading '{local_path}'…")
    result = client.temp_put(local_path)
    if result is None:
        print(f"Upload failed: {client.error()}", file=sys.stderr)
        return 1

    print(f"Upload complete: '{local_path}' is now in agOO temp storage.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
