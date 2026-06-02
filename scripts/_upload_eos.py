#!/usr/bin/env python3
"""_upload_eos.py — One-shot batch upload of the local `eos/` directory tree.

Walks every file under /home/thomas/claude/eos/ and uploads them all to agOO
temp storage using batch_put(), which automatically splits the work into
cache-sized batches and triggers archive sync cycles between them.

This script is not a general-purpose tool — it hard-codes the source directory
and credentials for a specific one-time migration.  For general use, prefer
the upload.py script which accepts paths on the command line.
"""
import os, sys, time
from pathlib import Path

# Run from a fixed working directory so that relative paths inside the
# library (e.g. the TUS sentinel file) resolve consistently.
os.chdir("/home/thomas/claude")
sys.path.insert(0, "agOO-python")
from agoo import Agoo


def fmt(n: int) -> str:
    """Return a compact human-readable byte size (e.g. '3.14 GB')."""
    for u, t in (("GB", 1<<30), ("MB", 1<<20), ("KB", 1<<10)):
        if n >= t: return f"{n/t:.2f} {u}"
    return f"{n} B"


# Collect every regular file under eos/, sorted so the upload order is
# deterministic and progress can be resumed predictably if interrupted.
files = sorted(str(p) for p in Path("eos").rglob("*") if p.is_file())
total = sum(os.path.getsize(f) for f in files)

print(f"[{time.strftime('%H:%M:%S')}] {len(files)} files  {fmt(total)}", flush=True)

# `login` is the agOO form username; `user` is the per-user URL namespace.
# debug=True prints each HTTP request so progress is visible for long uploads.
client = Agoo(user="thomas", login="admin", password="welcometoagoo", debug=True)
if not client.login():
    sys.exit(f"Login failed: {client.error()}")

# Show the pre-upload cache state so it's easy to see how much headroom exists.
u = client.get_usage()
print(f"[{time.strftime('%H:%M:%S')}] Cache {fmt(u['used'])} used / {fmt(u['total'])} total  "
      f"({fmt(u['total']-u['used'])} free  10% margin = {fmt(int(u['total']*0.1))})", flush=True)

t0 = time.time()

# batch_put() handles splitting, uploading, syncing, and polling automatically.
# poll_interval=30 means the script checks for sync completion every 30 seconds.
ok = client.batch_put(files, poll_interval=30)
if not ok:
    sys.exit(f"Upload failed: {client.error()}")

# Show the post-upload cache state as a final sanity check.
u = client.get_usage()
print(f"\n[{time.strftime('%H:%M:%S')}] Done in {(time.time()-t0)/60:.1f} min — "
      f"cache now {fmt(u['used'])} used / {fmt(u['total'])} total", flush=True)
