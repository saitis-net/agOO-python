# agOO-python

A Python client library for the **agOO** remote file-storage and archive service.

This is a Python port of the original Perl `agoo.pm` support library, preserving
the full API surface while adding type hints, structured error handling, and
security hardening.

---

## What is agOO?

agOO is a web-based file management service that provides two storage tiers:

| Tier | Description |
|---|---|
| **temp** | Fast, online storage — files are immediately accessible via the REST API |
| **archive** | Slower, offline/tape storage — files are migrated asynchronously |

The service exposes a REST API protected by a session token (returned at login
and sent on every subsequent request as the `X-Auth` HTTP header).  Large file
uploads use the [TUS resumable-upload protocol](https://tus.io).

---

## Requirements

- Python 3.10 or later
- [`requests`](https://docs.python-requests.org/) (the only third-party dependency)

```
pip install requests
```

---

## Installation

Clone the repository and install in editable mode:

```bash
git clone <repo-url> agOO-python
cd agOO-python
pip install -e .
```

Or install the package directory directly without cloning:

```bash
pip install /path/to/agOO-python
```

---

## Quick start

```python
from agoo import Agoo

# Credentials are read from environment variables (recommended):
#   export agOO_USER=myuser
#   export agOO_PASSWORD=s3cr3t
client = Agoo(user="myuser", login="admin", password="s3cr3t")

# Authenticate — must be called before any other operation.
if not client.login():
    raise SystemExit(f"Login failed: {client.error()}")

# Upload a local file to temp storage.
if not client.temp_put("data/report.tar.gz"):
    raise SystemExit(f"Upload failed: {client.error()}")

# Get metadata for a remote file (returns None if the file is in archive).
info = client.stat("data/report.tar.gz")
print(info)

# Stream a file from temp storage to disk (memory usage bounded by chunk_size).
ok = client.temp_get_file("data/report.tar.gz", "/mnt/nas/report.tar.gz")

# Trigger an async archive sync and poll for completion.
job_id = client.async_synchronize()
while True:
    status = client.async_completed(job_id)
    if status is not None:
        print(f"Job finished with status {status}")
        break
    time.sleep(30)

# Shut down the remote instance and clear the local session token.
client.logout()
```

---

## Configuration

All options are passed as keyword arguments to the constructor.

| Option | Default | Env var | Description |
|---|---|---|---|
| `base_url` | `https://agoo.saitis.net` | — | Root URL of the agOO installation |
| `start_url` | `https://agoo.saitis.net/cgi-bin/start-fb.cgi` | — | URL that wakes a stopped instance |
| `login` | `admin` | — | agOO credential username sent in the login form |
| `user` | *(required)* | `agOO_USER` | Per-user API namespace embedded in every URL path |
| `password` | *(required)* | `agOO_PASSWORD` | Password for `login` |
| `debug` | `False` | — | Print diagnostic output to stdout |
| `io_size` | `10485760` (10 MiB) | — | Upload chunk size in bytes |

> **Note:** `base_url` and `start_url` **must** use `https://`.  Passing an
> `http://` URL raises `ValueError` at construction time.

---

## API reference

### Authentication

| Method | Description |
|---|---|
| `login() → bool` | Authenticate and store the session token.  Returns `True` on success. |
| `logout()` | Terminate the backend instance and clear the local session token. |

### Temp storage

| Method | Description |
|---|---|
| `get_usage() → dict \| None` | Fetch cache and archive usage statistics. |
| `stat(path) → dict \| None` | Retrieve file metadata. Returns `None` for archived (offline) files. |
| `temp_put(path, override=False) → bool \| None` | Upload a single local file using TUS resumable upload. Existing files are skipped unless `override=True`. |
| `batch_put(files, poll_interval=30, override=False) → bool \| None` | Upload a list of files, automatically batching and syncing when the combined size exceeds cache space. |
| `temp_put_fake(path)` | Create an empty placeholder file on the server. |
| `temp_get(path, max_bytes=10MiB) → str \| None` | Download a small file into memory (hard cap enforced). Use for metadata blobs only. |
| `temp_get_file(path, local_path, chunk_size=8MiB) → bool \| None` | Stream a file of any size directly to disk with bounded memory use. |
| `temp_del(path)` | Delete a file from temp storage. |
| `temp_sum(path, hash_type) → str \| None` | Retrieve a checksum for a remote file. |

### Archive operations

| Method | Description |
|---|---|
| `schedule_migrate() → bool` | No-op; migration is handled implicitly by the server. |
| `schedule_unmigrate(path)` | Request a copy of an archive file back to temp storage. |

### Async sync

| Method | Description |
|---|---|
| `async_synchronize() → str \| None` | Trigger an archive sync job; returns a job ID. |
| `async_completed(job_id) → int \| None` | Poll for job completion; returns status code, or `None` if still running. |

### System

| Method | Description |
|---|---|
| `terminate()` | Ask the backend to shut down its current instance. |
| `error() → str \| None` | Return the last error message. |

---

## Error handling

All methods return `None` on failure.  The reason is available via `client.error()`:

```python
result = client.temp_put("myfile.dat")
if result is None:
    print(f"Failed: {client.error()}")
```

Methods that cannot recover gracefully raise standard Python exceptions:

| Exception | When |
|---|---|
| `ValueError` | Bad constructor arguments (missing credentials, non-HTTPS URL, unsafe path) |
| `RuntimeError` | Calling `stat()` before `login()` |
| `NotImplementedError` | Calling unimplemented methods (`system_stats`, `schedule_archive_check_sums`, …) |

---

## Pre-existing files

`temp_put()` and `batch_put()` handle files that already exist on the server:

| File location | `override=False` (default) | `override=True` |
|---|---|---|
| In **temp**, same size | Silently skipped | Replaced |
| In **temp**, different size | Error — size mismatch reported | Replaced |
| In **archive** | Silently skipped | Replaced |

---

## Batch uploads

When uploading more data than fits in the cache in one go, use `batch_put()`:

```python
files = ["backup1.tar.gz", "backup2.tar.gz", "backup3.tar.gz"]
result = client.batch_put(files, poll_interval=30)
if result is None:
    print(f"Failed: {client.error()}")
```

`batch_put()` works as follows:

1. Validates every path up-front (no network I/O yet).
2. Rejects any individual file larger than the total cache capacity.
3. Greedily fills a batch with as many files as fit in the available cache.
4. Uploads the batch using TUS resumable upload.
5. If files remain, calls `async_synchronize()` and polls until the archive
   sync finishes (freeing the cache space), then repeats from step 3.

---

## Security notes

The following hardening measures are built into the library.
See `claude_audit.md` for the full security audit and findings.

- **HTTPS enforced** — `http://` URLs are rejected at construction time.
- **TLS verification** — Certificate validation is enabled explicitly and
  cannot be overridden by environment variables.
- **Request timeouts** — All HTTP requests carry a 10 s connect / 60 s read
  timeout to prevent indefinite hangs.
- **Response body cap** — Non-streaming API responses are buffered with a
  hard 1 MiB limit to prevent memory exhaustion from a malicious server.
- **Path traversal prevention** — `temp_put()` and `temp_get_file()` both
  validate local paths; relative traversal sequences are rejected.
- **Redirect token protection** — The `X-Auth` session token is stripped
  from requests before following a redirect to a different origin.
- **Cookie isolation** — The session rejects all server-set cookies;
  authentication is exclusively via `X-Auth`.
- **Redirect cap** — HTTP redirects are limited to 5 per request.
- **No subprocess** — Date strings are generated with `datetime`, not a shell.
- **Session cleanup** — `logout()` terminates the backend and clears the
  in-memory token; `__del__` also clears it on object destruction.

---

## Scripts

The `scripts/` directory contains ready-to-run command-line tools built on the library.
All scripts read credentials from the `agOO_USER` and `agOO_PASSWORD` environment
variables and call `logout()` on exit.

### upload.py — Upload files to temp or archive storage

```
python scripts/upload.py [--poll-interval SECONDS] [--force] <file> [<file> ...]
```

Uploads one or more local files using `batch_put()`.  Files that already exist
on the server are silently skipped unless `--force` is passed.

| Option | Default | Description |
|---|---|---|
| `--poll-interval N` | `30` | Seconds between sync-completion polls. |
| `--force` | off | Overwrite files that already exist on the server (temp or archive). |

```bash
# Upload a single file (skipped if already present)
python scripts/upload.py report.tar.gz

# Force-overwrite an existing copy
python scripts/upload.py --force report.tar.gz

# Upload a directory glob; sync cycles fire automatically if needed
python scripts/upload.py data/*.tar.gz
```

### download.py — Download a file from temp storage

```
python scripts/download.py [options] <remote-path> [<local-output-path>]
```

Streams a file from agOO temp storage to disk one chunk at a time.  Peak
memory usage is bounded by `--chunk-size` regardless of file size.  The script
refuses to overwrite an existing local file.

For files that have been migrated to archive, pass `--wait` to trigger an
automatic recall and wait for the file to come back online.

| Argument | Description |
|---|---|
| `remote-path` | Path to the file in agOO storage. |
| `local-output-path` | Where to save the file locally (default: basename of remote path). |

| Option | Default | Description |
|---|---|---|
| `--chunk-size BYTES` | `8388608` (8 MiB) | Network read chunk size in bytes. |
| `--wait` | off | Trigger an archive recall and wait for the file to come online. |
| `--poll-interval N` | `30` | Seconds between recall-completion polls (requires `--wait`). |
| `--timeout N` | `3600` | Max seconds to wait for a recall before giving up (requires `--wait`). |

```bash
# Download to the current directory
python scripts/download.py data/backup.tar.gz

# Save to an explicit path
python scripts/download.py data/backup.tar.gz /mnt/nas/backup.tar.gz

# Recall from archive and wait up to one hour (the default timeout)
python scripts/download.py --wait eos/EOS-4.31.1F.swi

# Recall from archive, poll every 5 minutes, give up after 2 hours
python scripts/download.py --wait --poll-interval 300 --timeout 7200 eos/EOS-4.31.1F.swi
```

### sync.py — Trigger an archive sync and wait for completion

```
python scripts/sync.py [--poll-interval SECONDS]
```

Logs in, calls `async_synchronize()` to queue a migration job from temp to
archive storage, then polls until the job finishes.

| Option | Default | Description |
|---|---|---|
| `--poll-interval N` | `30` | Seconds between completion polls. |

```bash
python scripts/sync.py
python scripts/sync.py --poll-interval 120
```

---

## Known limitations

- `temp_get()` enforces a hard 10 MiB memory cap and will refuse to return
  larger content.  Use `temp_get_file()` for large or binary files.
- `stat()` returns `None` for files that are in archive (offline) storage.
  Use the resources listing API directly if you need to enumerate archived files.
- `system_stats()`, `schedule_archive_check_sums()`, and `schedule_archive_del()`
  are not yet implemented upstream.

---

## Origin

Ported from `lib/agoo.pm` (Perl).  Original dependencies: `LWP::UserAgent`,
`JSON`, `URI::Escape`, `Crypt::URandom`.
