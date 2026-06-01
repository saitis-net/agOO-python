#!/usr/bin/env python3
import os, sys, time
from pathlib import Path

os.chdir("/home/thomas/claude")
sys.path.insert(0, "agOO-python")
from agoo import Agoo

def fmt(n):
    for u, t in (("GB", 1<<30), ("MB", 1<<20), ("KB", 1<<10)):
        if n >= t: return f"{n/t:.2f} {u}"
    return f"{n} B"

files = sorted(str(p) for p in Path("eos").rglob("*") if p.is_file())
total = sum(os.path.getsize(f) for f in files)

print(f"[{time.strftime('%H:%M:%S')}] {len(files)} files  {fmt(total)}", flush=True)

client = Agoo(user="thomas", login="admin", password="welcometoagoo", debug=True)
if not client.login():
    sys.exit(f"Login failed: {client.error()}")

u = client.get_usage()
print(f"[{time.strftime('%H:%M:%S')}] Cache {fmt(u['used'])} used / {fmt(u['total'])} total  "
      f"({fmt(u['total']-u['used'])} free  10% margin = {fmt(int(u['total']*0.1))})", flush=True)

t0 = time.time()
ok = client.batch_put(files, poll_interval=30)
if not ok:
    sys.exit(f"Upload failed: {client.error()}")

u = client.get_usage()
print(f"\n[{time.strftime('%H:%M:%S')}] Done in {(time.time()-t0)/60:.1f} min — "
      f"cache now {fmt(u['used'])} used / {fmt(u['total'])} total", flush=True)
