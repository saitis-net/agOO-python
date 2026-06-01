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

# Credentials can be passed as keyword arguments …
client = Agoo(user="myuser", password="s3cr3t")

# … or supplied via environment variables (recommended for scripts):
#   export agOO_USER=myuser
#   export agOO_PASSWORD=s3cr3t
client = Agoo()

# Authenticate — must be called before any other operation.
if not client.login():
    raise SystemExit(f"Login failed: {client.error()}")

# Upload a local file to temp storage.
if not client.temp_put("data/report.tar.gz"):
    raise SystemExit(f"Upload failed: {client.error()}")

# Get metadata for a remote file.
info = client.stat("data/report.tar.gz")
print(info)

# Trigger an async archive sync and poll for completion.
job_id = client.async_synchronize()
while True:
    status = client.async_completed(job_id)
    if status is not None:
        print(f"Job finished with status {status}")
        break
    time.sleep(30)

# Shut down the remote instance when done.
client.terminate()
```

---

## Configuration

All options are passed as keyword arguments to the constructor.  Any option not
provided falls back to its default value or environment variable.

| Option | Default | Env var | Description |
|---|---|---|---|
| `base_url` | `https://agoo.saitis.net` | — | Root URL of the agOO installation |
| `start_url` | `https://agoo.saitis.net/cgi-bin/start-fb.cgi` | — | URL that wakes a stopped instance |
| `login` | `admin` | — | agOO username sent in the login form |
| `user` | *(required)* | `agOO_USER` | Per-user API namespace |
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
| `logout()` | *Not yet implemented.* |

### Temp storage

| Method | Description |
|---|---|
| `get_usage() → dict \| None` | Fetch cache and archive usage statistics. |
| `stat(path) → dict \| None` | Retrieve file metadata as a dict. |
| `temp_put(path) → bool \| None` | Upload a single local file using TUS resumable upload; checks available space first. |
| `batch_put(files, poll_interval=30) → bool \| None` | Upload a list of files, automatically splitting into batches and running sync cycles when the combined size exceeds available cache space. |
| `temp_put_fake(path)` | Create an empty placeholder file on the server. |
| `temp_get(path, max_bytes=10MiB) → str \| None` | Download a **small** file into memory (hard cap enforced). Use for status blobs and metadata only. |
| `temp_get_file(path, local_path, chunk_size=8MiB) → bool \| None` | Stream a file of any size directly to disk with bounded memory use. |
| `temp_del(path)` | Delete a file from temp storage. |
| `temp_sum(path, hash_type) → str \| None` | Retrieve a checksum for a remote file. |

### Archive operations

| Method | Description |
|---|---|
| `schedule_migrate() → bool` | No-op; migration is implicit. |
| `schedule_unmigrate(path)` | Request a copy of an archive file back to temp. |

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
| `NotImplementedError` | Calling unimplemented methods (`logout`, `system_stats`, …) |

---

## Security notes

The following hardening measures are applied by this library.
See `changes.log` for full details of the audit findings and mitigations.

- **HTTPS enforced** — `http://` URLs are rejected at construction time.
- **TLS verification** — Certificate validation is enabled explicitly and
  cannot be overridden by environment variables.
- **Request timeouts** — All HTTP requests have a 10 s connect / 60 s read
  timeout to prevent indefinite hangs.
- **Path traversal prevention** — `temp_put()` validates that the local file
  path stays within the current working directory.
- **Redirect cap** — HTTP redirects are limited to 5 per request to prevent
  loops and token leakage to third-party domains.
- **No subprocess** — Date strings are generated with `datetime` instead of
  spawning a shell process.

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

The `upload.py` script accepts multiple files and uses `batch_put()` automatically:

```bash
python scripts/upload.py --poll-interval 60 data/*.tar.gz
```

## Known limitations

- `temp_get()` now enforces a hard memory cap (default 10 MiB) and will refuse
  to return content larger than that limit.  Use `temp_get_file()` for large or
  binary files — it streams directly to disk with memory usage bounded by
  `chunk_size` (default 8 MiB).
- `logout()`, `system_stats()`, `schedule_archive_check_sums()`, and
  `schedule_archive_del()` are not yet implemented upstream.

---

## Origin

Ported from `lib/agoo.pm` (Perl).  Original dependencies: `LWP::UserAgent`,
`JSON`, `URI::Escape`, `Crypt::URandom`.
