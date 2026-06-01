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
| `stat(path) → dict \| None` | Retrieve file metadata as a dict. |
| `temp_put(path) → bool \| None` | Upload a local file using TUS resumable upload. |
| `temp_put_fake(path)` | Create an empty placeholder file on the server. |
| `temp_get(path) → str \| None` | Download a remote file's content as a string. |
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
- **Credential minimisation** — The plaintext password is erased from memory
  immediately after a successful login.  If the session token expires and
  re-authentication is needed, `login()` prompts interactively via
  `getpass.getpass()` rather than keeping the password in memory between calls.
  In non-interactive contexts (daemons, CI) where no terminal is available,
  a clear error is returned instead of crashing.
- **Redirect cap** — HTTP redirects are limited to 5 per request to prevent
  loops and token leakage to third-party domains.
- **No subprocess** — Date strings are generated with `datetime` instead of
  spawning a shell process.

---

## Known limitations

- `temp_get()` loads the entire remote file into memory.  This is a limitation
  inherited from the original Perl implementation.  For large files, a future
  streaming variant using `requests` `stream=True` is recommended.
- `logout()`, `system_stats()`, `schedule_archive_check_sums()`, and
  `schedule_archive_del()` are not yet implemented upstream.

---

## Origin

Ported from `lib/agoo.pm` (Perl).  Original dependencies: `LWP::UserAgent`,
`JSON`, `URI::Escape`, `Crypt::URandom`.
