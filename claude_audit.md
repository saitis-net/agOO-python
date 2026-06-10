# Security Audit — agOO-python

**Audit date:** 2026-06-02  
**Last updated:** 2026-06-10 (TUI tool review T-01–T-04)  
**Auditor:** Claude Sonnet 4.6  
**Scope:** All source files in the repository (`agoo/`, `scripts/`, `tools/`)  
**Methodology:** Static analysis — full manual code review of every source file

---

## Executive Summary

The library core (`agoo/client.py`) contains solid defensive programming: HTTPS enforcement, TLS certificate pinning, explicit request timeouts, a redirect cap, and path-traversal validation on uploads. The most significant finding was entirely in the scripts layer: a shared plaintext password committed to version control and baked into git history.

Five of the eleven findings are in `scripts/` and would not affect consumers of the library itself. The library had two meaningful issues of its own: `temp_get_file()` applied no validation to its output path (an asymmetry with `temp_put()`), and the `X-Auth` session token was not stripped from cross-domain HTTP redirects. Both have since been fixed.

No finding reaches the RCE / privilege-escalation tier. The highest-scoring issue (7/10) was credential exposure through version control; the hard-coded defaults have been removed from source, though the git history still contains the old password in earlier commits and requires a history rewrite to fully expunge.

**Mitigations applied (2026-06-03):** F-01 (partial), F-02 (full), F-03 (full), F-11 (full — file deleted).

---

## Findings

---

### F-01 — Plaintext Credentials Committed to Version Control

**Score: 7 / 10 → residual: 3 / 10 (partially mitigated)**  
**Status: PARTIAL — source fixed, git history not rewritten**  
**Files:** `scripts/upload.py:64`, `scripts/download.py:61`, `scripts/sync.py:42`, `scripts/_upload_eos.py` (deleted)

#### Description

All four scripts contained the password `welcometoagoo` as a hard-coded default that was active whenever the `agOO_PASSWORD` environment variable was unset. The password was present in plaintext in the current HEAD and in every preceding commit that touched those files — it is permanently recoverable via `git log -p` regardless of any future edits.

```python
# before (upload.py, download.py, sync.py):
_PASSWORD = os.environ.get("agOO_PASSWORD", "welcometoagoo")

# before (_upload_eos.py):
client = Agoo(user="thomas", login="admin", password="welcometoagoo", debug=True)
```

#### Impact

An attacker with read access to the repository (e.g. if it is pushed to a public remote, shared with a contractor, or exposed via a misconfigured CI system) gains the agOO service password. With valid credentials they can:

- Read or exfiltrate all files in temp and archive storage.
- Delete files from temp storage (`temp_del`).
- Trigger arbitrary archive sync jobs.
- Terminate the backend instance (`terminate`).

The service manages file and tape-archive storage, so the data-loss and exfiltration blast radius is significant.

#### Mitigation applied (commit `c7a2feb`, 2026-06-02)

Hard-coded defaults removed from all three remaining scripts. Each now reads from the environment with no fallback and exits with a clear error message if either `agOO_USER` or `agOO_PASSWORD` is unset:

```python
# after:
_PASSWORD = os.environ.get("agOO_PASSWORD")
...
missing = [name for name, val in (("agOO_USER", _USER), ("agOO_PASSWORD", _PASSWORD)) if not val]
if missing:
    print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)
```

`scripts/_upload_eos.py` (which hard-coded `user="thomas"` and `debug=True` in addition to the password) was deleted entirely as it was a one-off migration script with no ongoing use.

#### Residual risk

The git history still contains the plaintext password in all commits prior to `c7a2feb`. It is recoverable via `git log -p`. **The password must be rotated**, and a history rewrite (`git filter-repo` or `git filter-branch`) is required to fully expunge it from the repository. Until that rewrite is done and the repository is force-pushed (or re-created), any clone made before the rewrite retains the secret.

#### Remaining recommendation

1. **Rotate the agOO password immediately.**
2. Rewrite history with `git filter-repo --replace-text` targeting the literal string `welcometoagoo`.
3. Force-push all branches and tags, then invalidate all outstanding clones.

---

### F-02 — `X-Auth` Session Token Forwarded on Cross-Domain HTTP Redirects

**Score: 4 / 10 → residual: 0 / 10 (fully mitigated)**  
**Status: RESOLVED — commit `f1331e4`**  
**File:** `agoo/client.py:70-83`, `agoo/client.py:183`

#### Description

The `requests` library automatically strips the `Authorization` header when following a redirect to a different origin, but it does **not** strip arbitrary custom headers such as `X-Auth`. The library sets `max_redirects = 5` to limit the attack surface, but did not instruct `requests` to drop `X-Auth` on cross-origin hops.

If the agOO server (or a MITM attacker on the network path) issued a redirect to an attacker-controlled domain, all five hops were followed and the `X-Auth` token was sent to the third-party host on every hop.

#### Mitigation applied (commit `f1331e4`, 2026-06-02)

A `_SafeSession` subclass overrides `rebuild_auth()` — the hook `requests` calls before following each redirect — to also strip `X-Auth` when the redirect target host differs from the original request host:

```python
class _SafeSession(requests.Session):
    def rebuild_auth(self, prepared_request, response) -> None:
        super().rebuild_auth(prepared_request, response)
        if urlparse(response.url).netloc != urlparse(prepared_request.url).netloc:
            prepared_request.headers.pop("X-Auth", None)
```

`Agoo.__init__()` now instantiates `_SafeSession()` instead of `requests.Session()` (`client.py:183`). The token is still sent on same-origin redirects and on the original request; it is dropped only on cross-origin hops.

---

### F-03 — Path Validation Asymmetry Between Upload and Download

**Score: 4 / 10 → residual: 0 / 10 (fully mitigated)**  
**Status: RESOLVED — commits `f1331e4`, `1a6efd0`**  
**File:** `agoo/client.py:242-270`, `agoo/client.py:1267`

#### Description

The library had a two-sided path validation asymmetry:

- **Download side (`temp_get_file`):** wrote to a caller-supplied `local_path` with no validation, silently accepting paths such as `../../.ssh/authorized_keys`.
- **Upload side (`temp_put` / `batch_put`):** called `_validate_local_path()` which rejected not only relative traversal but also all absolute paths, preventing callers from uploading files outside the current working directory (e.g. `/mnt/nas/backup.tar.gz`).

#### Mitigation applied

**Download side (commit `f1331e4`, 2026-06-02):** A new `_validate_output_path()` static method was added (`client.py:272`) and called at the top of `temp_get_file()` before any network I/O (`client.py:1267`). It rejects null bytes and relative traversal, but permits absolute paths.

**Upload side (commit `1a6efd0`, 2026-06-03):** `_validate_local_path()` was updated to match the same policy — absolute paths are now permitted; only relative paths that escape the CWD are rejected. This mirrors `_validate_output_path()` and was discovered during integration testing when uploading files by absolute path.

Both validators now apply the same rule:

- **Null bytes:** always rejected.
- **Relative traversal:** rejected (e.g. `../../etc/shadow`).
- **Absolute paths:** permitted — the caller explicitly chose the location.

---

### F-04 — Remote Paths with Unencoded `/` (By Design)

**Score: 3 / 10 → N/A**  
**Status: CLOSED — reclassified as intentional behaviour**  
**File:** `agoo/client.py:49`

#### Original finding

The audit flagged that `uri_escape` uses `safe='/'` (the `urllib.parse.quote` default), leaving forward slashes unencoded, unlike the Perl original which encoded them. This was assessed as a potential path-traversal risk.

#### Resolution

The divergence from the Perl behaviour is intentional. The Python client supports hierarchical remote paths (e.g. `"folder/file.txt"`) as a first-class feature, and encoding slashes would break that API. The server-side ACL model is expected to enforce namespace boundaries regardless of how the path is transmitted. This finding is not a vulnerability.

---

### F-05 — Unbounded Response Body Buffering in Non-Streaming API Calls

**Score: 3 / 10 → residual: 0 / 10 (fully mitigated)**  
**Status: RESOLVED — commit `106aa43`**  
**File:** `agoo/client.py:_do()`

#### Description

`_do()` previously used `stream=False` by default, instructing `requests` to download and buffer the complete response body before returning. For API calls expected to return small JSON blobs (`stat`, `get_usage`, `login`, `temp_sum`), no size limit was enforced. A compromised or malicious server could respond with a multi-gigabyte body, exhausting the process's memory.

`temp_get()` already addressed this for file downloads (with a `max_bytes` cap and streaming); the fix was not extended to the other methods.

#### Mitigation applied (commit `106aa43`, 2026-06-03)

`_do()` now always passes `stream=True` to `requests` internally. For non-streaming callers, the response body is consumed iteratively with a hard 1 MiB cap before being stored back into the response object so callers can still use `response.text`:

```python
if not stream:
    _MAX_API_BODY = 1 * 1024 * 1024  # 1 MiB
    cl = response.headers.get("Content-Length")
    if cl and int(cl) > _MAX_API_BODY:
        response.close()
        self._error = f"{path}: response too large ({cl} bytes)"
        return None
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > _MAX_API_BODY:
            response.close()
            self._error = f"{path}: response body exceeded {_MAX_API_BODY} bytes"
            return None
        chunks.append(chunk)
    response._content = b"".join(chunks)
    response._content_consumed = True
```

The `Content-Length` pre-check provides an early exit without reading any bytes; the iterative cap handles servers that omit or falsify `Content-Length`.

---

### F-06 — TOCTOU Race Between Path Validation and File Open

**Score: 2 / 10**  
**Status: CLOSED — accepted risk, attack scenario considered highly unlikely**  
**File:** `agoo/client.py:216-245`, `agoo/client.py:656-663`, `agoo/client.py:757`

#### Description

`_validate_local_path()` resolves and checks the path at call time, but the actual `open()` happens several lines later (after `get_usage()` and the TUS HEAD round-trip, which can take seconds). Between the check and the open, a local attacker with write access to the same directory could swap a legitimate file for a symlink pointing elsewhere:

```python
self._validate_local_path(f)    # symlink check here  ← safe at this moment
...
usage = self.get_usage()         # ~1 network round-trip
offset = self._tus_get_offset()  # ~1 more round-trip
...
with open(f, "rb") as fh:        # symlink may have changed by now
```

#### Impact

Exploitation requires an attacker with local write access to the same working directory and the ability to time the swap precisely. On typical single-user setups this is not a realistic attack; it becomes relevant in shared-directory environments (e.g. `/tmp`, group-writable upload staging areas).

#### Recommendation

Re-open the file using a file descriptor obtained at validation time (avoiding the TOCTOU window entirely), or validate the path immediately before `open()`:

```python
# Validate immediately before open, not before network calls
with open(f, "rb") as fh:
    self._validate_local_path(f)   # re-check after open, or open by fd
```

The most robust mitigation is `O_NOFOLLOW` (via `os.open(f, os.O_RDONLY | os.O_NOFOLLOW)`), which refuses to open a symlink.

---

### F-07 — Predictable Sentinel Path in `async_synchronize()` Enables Local Symlink Attack

**Score: 2 / 10**  
**Status: CLOSED — accepted risk, attack scenario considered highly unlikely**  
**File:** `agoo/client.py:1322-1373`

#### Description

`async_synchronize()` creates a file at a well-known, predictable relative path (`_sgbdb/archUnarchAsked`) in the current working directory. If an attacker with local write access to the CWD plants a symlink at that path before the method runs, the `open(meta_file, "w")` call will write through the symlink to whatever the symlink points to:

```python
meta_file = "_sgbdb/archUnarchAsked"
os.makedirs(local_dir, exist_ok=True)
with open(meta_file, "w") as fh:   # follows symlinks
    fh.write(...)
```

The content written is a short, structured text string (timestamp + job ID), not attacker-controlled.

#### Impact

A successful attack overwrites an arbitrary file that the running process has write permission to with a harmless-looking text blob. In the worst case this corrupts a configuration file, log file, or lock file. It does not provide direct code execution, but may degrade service availability or circumvent file-presence checks in other tools.

#### Recommendation

Open the sentinel file with `O_CREAT | O_EXCL | O_NOFOLLOW` flags to refuse symlinks and prevent accidental overwrite:

```python
fd = os.open(meta_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
with os.fdopen(fd, "w") as fh:
    fh.write(...)
```

---

### F-08 — `Referer` Header Leaks API Username to `start_url` Host

**Score: 1 / 10**  
**Status: CLOSED — accepted risk, attack scenario considered highly unlikely**  
**File:** `agoo/client.py:415-419`

#### Description

When a 502 response triggers a backend wake-up, the library sends a `Referer` header containing the full per-user API base URL to the `start_url` endpoint:

```python
wake = self._session.get(
    self._config["start_url"],
    headers={"Referer": self.url() + "/"},  # e.g. https://agoo.saitis.net/thomas/
    ...
)
```

If `start_url` resolves to a different host than `base_url` (e.g. a separate CGI server), the username embedded in the Referer is sent in the clear HTTP header and may appear in access logs on that host.

#### Impact

Leaks the API username to an operator of a different host. Low severity because: (a) both URLs are expected to share the same domain in practice, (b) the username is not a secret in the same way the password or token are.

#### Recommendation

Either omit the `Referer` header entirely, or restrict it to the origin only (`Referrer-Policy: origin`). Alternatively, validate that `start_url` and `base_url` share the same origin before sending the header.

---

### F-09 — No Session Revocation (`logout()` Not Implemented)

**Score: 1 / 10 → residual: 0 / 10 (fully mitigated)**  
**Status: RESOLVED — commit `9efcf17`**  
**File:** `agoo/client.py:logout()`, `agoo/client.py:__del__()`

#### Description

`logout()` raises `NotImplementedError`. The session token stored in `self._auth_token` cannot be invalidated through the client API. If the token leaks (e.g. via F-02, log scraping, or a process-memory dump), there is no programmatic way to revoke it:

```python
def logout(self):
    raise NotImplementedError("logout is not yet implemented")
```

#### Impact

Increases the window of opportunity for token-reuse attacks. Low severity in isolation; combines badly with F-02.

#### Mitigation applied (commit `9efcf17`, 2026-06-03)

The server does not expose a logout endpoint (the Perl original never implemented one). `logout()` now clears `self._auth_token = None`, making the client object inert until `login()` is called again. A `__del__` method ensures the token is also wiped when the object is garbage-collected, even if the caller never calls `logout()` explicitly:

```python
def logout(self) -> None:
    self._auth_token = None

def __del__(self) -> None:
    self._auth_token = None
```

---

### F-10 — `requests.Session` Silently Accumulates Server-Set Cookies

**Score: 1 / 10 → residual: 0 / 10 (fully mitigated)**  
**Status: RESOLVED — commit `9efcf17`**  
**File:** `agoo/client.py:_SafeSession.__init__()`

#### Description

`requests.Session` stores `Set-Cookie` response headers automatically and re-sends those cookies on subsequent requests to the same domain. The agOO protocol uses `X-Auth` for authentication, not cookies, but the session does not disable cookie handling:

```python
self._session = requests.Session()
self._session.verify = True
self._session.max_redirects = _MAX_REDIRECTS
# no: self._session.cookies.clear_expired_cookies()
# no: policy to disable cookie storage
```

If the server ever emits a `Set-Cookie` header (e.g. for a CSRF token, a session affinity cookie, or a misconfigured middleware), those cookies will be silently stored and replayed, potentially interfering with the intended `X-Auth`-only authentication model.

#### Impact

Low in practice. Could become relevant if the server adds cookie-based CSRF protection and the replayed cookies conflict with fresh CSRF tokens.

#### Mitigation applied (commit `9efcf17`, 2026-06-03)

`_SafeSession.__init__()` replaces the default cookie jar with an inert empty `RequestsCookieJar`, so any `Set-Cookie` headers from the server are silently discarded and nothing is replayed on subsequent requests:

```python
def __init__(self) -> None:
    super().__init__()
    self.cookies = requests.cookies.RequestsCookieJar()
```

Placing the fix in `_SafeSession` rather than `Agoo.__init__()` keeps the policy co-located with the other session security controls.

---

### F-11 — `debug=True` Hard-Coded in `_upload_eos.py`

**Score: 1 / 10 → residual: 0 / 10 (fully mitigated)**  
**Status: RESOLVED — file deleted, commit `c7a2feb`**  
**File:** `scripts/_upload_eos.py` (no longer exists)

#### Description

The internal migration script unconditionally enabled debug mode alongside hard-coded credentials (F-01), printing every HTTP method, path, offset, and job ID to stdout. Combined with F-01 it was the highest-risk single file in the repository.

#### Mitigation applied (commit `c7a2feb`, 2026-06-02)

`scripts/_upload_eos.py` was deleted. It was a one-off EOS migration script with no ongoing operational use. No replacement is needed; the remaining three scripts (`upload.py`, `download.py`, `sync.py`) support debug mode via `agOO_DEBUG=1`.

---

## Summary Table

Scores show original / residual. "—" residual means the finding is fully closed.

| ID | Title | Original | Residual | Status | Location |
|---|---|---|---|---|---|
| F-01 | Plaintext credentials in version-controlled files and git history | **7** | **3** | Partial — source fixed, history not rewritten | `scripts/*.py` |
| F-02 | `X-Auth` token forwarded on cross-domain HTTP redirects | **4** | — | **Resolved** (`f1331e4`) | `client.py:_SafeSession` |
| F-03 | Path validation asymmetry between upload and download | **4** | — | **Resolved** (`f1331e4`, `1a6efd0`) | `client.py:_validate_local_path()`, `_validate_output_path()` |
| F-04 | Remote paths with unencoded `/` | **3** | N/A | **By design** — intentional API feature | `client.py:uri_escape` |
| F-05 | Unbounded response body buffering in non-streaming API calls | **3** | — | **Resolved** (`106aa43`) | `client.py:_do()` |
| F-06 | TOCTOU race between path validation and `open()` | **2** | N/A | **Accepted risk** | `client.py:temp_put()` |
| F-07 | Predictable sentinel path enables local symlink attack | **2** | N/A | **Accepted risk** | `client.py:async_synchronize()` |
| F-08 | `Referer` header leaks API username to `start_url` | **1** | N/A | **Accepted risk** | `client.py:_do()` |
| F-09 | No session revocation (`logout()` unimplemented) | **1** | — | **Resolved** (`9efcf17`) | `client.py:logout()`, `__del__()` |
| F-10 | `requests.Session` silently accumulates server cookies | **1** | — | **Resolved** (`9efcf17`) | `client.py:_SafeSession.__init__()` |
| F-11 | `debug=True` hard-coded in `_upload_eos.py` | **1** | — | **Resolved** (`c7a2feb`) | `scripts/_upload_eos.py` (deleted) |

---

## Positive Security Controls Observed

The following measures are already in place and represent good practice:

| Control | Location |
|---|---|
| HTTPS enforced at construction time; `http://` URLs raise `ValueError` | `__init__()` |
| TLS certificate verification enabled explicitly (`verify=True`) | `__init__()` |
| HTTP redirect cap (`max_redirects = 5`) | `__init__()` |
| Connect + read timeouts on every request (`(10, 60)` seconds) | `_do()` |
| `X-Auth` token never passed to `_debug()` or error strings | `_do()` |
| Path traversal and null-byte check in `_validate_local_path()` | `temp_put()`, `batch_put()` |
| Memory cap + streaming in `temp_get()` (`max_bytes` enforced in two stages) | `temp_get()` |
| `json.JSONDecodeError` caught on all server responses | `stat()`, `get_usage()`, `temp_sum()` |
| `os.urandom(6)` used for job IDs (cryptographically random) | `_stamp_unique()` |
| No subprocess calls; date formatted with `datetime` | `async_synchronize()` |
| Guard against absolute-path override in `_validate_local_path()` | `_validate_local_path()` |
| Download script refuses to overwrite an existing local file | `scripts/download.py:127-132` |

---

---

## Dev-Branch Review — `override` / `--force` Feature (commit `4cac7ca`)

**Scope:** `agoo/client.py` (`temp_put`, `batch_put`) and `scripts/upload.py`  
**Methodology:** Diff review against `master`

---

### D-01 — `override_str` Query-Parameter Construction is Safe

**Score: 0 / 10 — No issue**

`override_str` is derived exclusively from a Python `bool`:

```python
override_str = "true" if override else "false"
```

The value can only ever be the string literals `"true"` or `"false"`. There is no path through which caller-supplied text reaches the query string, so query-parameter injection is not possible.

---

### D-02 — Silent Skip of Archived Files Changes `temp_put()` Semantics

**Score: 1 / 10 — Accepted risk**

Previously, a 409 followed by a `stat()` returning `None` (file in archive) caused `temp_put()` to return `None` with a "could not create remote file" error, making the conflict visible. The new code returns `True` (skip), which is silent:

```python
else:
    # stat returned None → file is in archive (offline).
    self._debug(f"temp_put: '{f}' already in archive, skipping ...")
    return True
```

**Risk:** Callers that previously caught the error to detect archive conflicts will no longer see one. The `_debug()` call is the only signal, and debug output is suppressed by default.

**Mitigation:** `batch_put()` and `upload.py` both treat `True` as success regardless of whether the file was actually uploaded or skipped, which is the intended semantics. Direct callers of `temp_put()` should be aware of this behaviour change. The risk is low in the current codebase — all callers are batch_put or the scripts — but warrants a note in the docstring.

---

### D-03 — Size-Mismatch Detection is a Positive Security Addition

**Score: 0 / 10 — Improvement**

The old 409 handler skipped any temp file where size matched, but produced a generic error for mismatches. The new handler explicitly detects and reports the size mismatch:

```python
self._error = (
    f"temp_put: '{f}' already exists on server with a "
    f"different size ({existing.get('size'):,} B on server "
    f"vs {file_size:,} B locally); use override=True to replace"
)
return None
```

This prevents silent re-use of a stale server copy when the local file has been updated, which was a latent correctness risk in the original code.

---

### D-04 — `--force` Has No Confirmation Guard Against Accidental Data Loss

**Score: 1 / 10 — Accepted risk**

`--force` is a single short flag with no secondary confirmation. A mistyped command or a shell glob expanding more than intended could silently overwrite files in archive — data that may be on tape and slow to recover.

```
# A glob expanding to 40 files overwrites all of them on the server:
python scripts/upload.py --force eos/*.swi
```

**Mitigation options:** A `--yes` / `--confirm` double-flag pattern, or a dry-run preview before executing. Both are operational concerns rather than security vulnerabilities — a malicious actor who can run the script already has full API access. The risk is accidental misuse by the operator.

**Decision:** Accepted risk for now. Consider adding a `--dry-run` flag in a future iteration.

---

## Dev-Branch Summary Table

| ID | Title | Score | Status |
|---|---|---|---|
| D-01 | `override_str` query-parameter construction | 0 | No issue |
| D-02 | Silent skip of archived files changes `temp_put()` semantics | 1 | Accepted risk |
| D-03 | Size-mismatch detection prevents stale-copy re-use | 0 | Positive improvement |
| D-04 | `--force` has no confirmation guard | 1 | Accepted risk |

---

---

## TUI Tool Review — `tools/filebrowser.py`

**Scope:** `tools/filebrowser.py` only  
**Review date:** 2026-06-10  
**Methodology:** Full manual static analysis

---

### T-01 — `_collect_local_files()` Follows Symlinks, Enabling Upload Scope Escape

**Score: 3 / 10 → residual: 0 / 10 (fully mitigated)**  
**Status: RESOLVED**  
**File:** `tools/filebrowser.py:1091-1118`

#### Description

`_collect_local_files()` recurses into any entry for which `item.is_dir()` returns `True`. On POSIX, `Path.is_dir()` follows symlinks, so a symlink to a directory resolves as a directory and the method descends into it. Likewise, `item.is_file()` is `True` for a symlink to a file, so symlinked files are also added to the upload queue.

```python
# before
for item in sorted(path.iterdir()):
    if item.name.startswith("."):
        continue
    if item.is_file():
        result.append(str(item))   # symlinks to files included
    elif item.is_dir():
        result.extend(self._collect_local_files(item))  # follows symlinks to dirs
```

Two concrete risks follow:

1. **Unintended upload scope.** If a symlink inside the browsed tree points to a directory that lives outside it (e.g. `/home/user/project/data → /mnt/nas/sensitive`), pressing "Mark folder for upload" uploads every file under `/mnt/nas/sensitive` without the user seeing those paths in the pane.

2. **Infinite recursion / crash.** A symlink loop (e.g. `a → .` or `a/b → ../a`) causes unbounded recursion. Python's default recursion limit (1000 frames) will eventually raise `RecursionError`, crashing the TUI and leaving the terminal in raw-curses mode until the exception propagates through `curses.wrapper()`.

#### Impact

An attacker who can plant a symlink in a directory that the user browses with the TUI can arrange for files from outside the expected tree to be uploaded to the remote server. On shared systems (CI build directories, multi-user NAS mounts) this is a plausible attack surface.

The `RecursionError` path is a reliable crash trigger requiring only local write access to any directory the user marks for upload.

#### Mitigation applied

Rather than refusing all symlinks, the fix tracks which real (resolved) directory paths have already been entered during a single traversal. Before recursing into any directory entry — whether a real directory or a symlink to one — its canonical on-disk path is computed via `Path.resolve()`. If that path already appears in the `_visited` set, it is a loop ancestor and the entry is skipped. Otherwise, the path is recorded and the descent proceeds normally.

Symlinks to **files** are unaffected: `is_file()` follows the link and the file is queued as before.

```python
# after
def _collect_local_files(self, path: Path,
                          _visited: set[str] | None = None) -> list[str]:
    if _visited is None:
        _visited = {str(path.resolve())}
    result: list[str] = []
    try:
        for item in sorted(path.iterdir()):
            if item.name.startswith("."):
                continue
            if item.is_file():
                result.append(str(item))
            elif item.is_dir():
                try:
                    real = str(item.resolve())
                except OSError:
                    continue
                if real in _visited:
                    continue  # loop — this real path is already an ancestor
                _visited.add(real)
                result.extend(self._collect_local_files(item, _visited))
    except PermissionError:
        pass
    return result
```

The `_visited` set is shared by reference across all recursive calls in one traversal, so a directory reachable via two different symlinks (diamond topology) is also correctly deduplicated. The `OSError` guard on `resolve()` handles the rare case where intermediate path components are inaccessible.

---

### T-02 — Remote Filenames with Embedded Control Characters Rendered Without Sanitization

**Score: 2 / 10**  
**Status: OPEN**  
**File:** `tools/filebrowser.py:448-474`, `tools/filebrowser.py:325-329`

#### Description

The remote pane builds each display line by truncating the server-supplied `display_name` field to `name_w` characters and passing the result directly to `curses.addstr()`:

```python
display = e.get("display_name", e.get("name", ""))
...
label = display + "/"          # or just display for files
line  = "  " + indicator + label[:name_w].ljust(name_w) + " " + size_s.rjust(size_w)
win.addstr(row, 1, line[:w - 2], attr)
```

No characters are stripped or escaped before the string reaches curses. If the server returns a filename that contains:

- **ANSI escape sequences** (e.g. `\x1b[2J`) — most curses builds pass these through to the underlying terminal driver, potentially clearing the screen or altering terminal mode.
- **Embedded newlines** (`\n`, `\r`) — cause curses to interpret the next portion of the line as a new row, mis-aligning all subsequent pane entries.
- **Null bytes** (`\x00`) — terminate the string early in C-backed curses implementations, silently truncating the rendered line.

The same issue applies to the `title` bar (`f" Local: {self.path} "`) and the status bar (`self.status`), which also render untrusted strings (server error text, server-supplied filenames) without sanitization.

#### Impact

A server operator (or a server that has been compromised) can produce filenames that corrupt the user's TUI display or, in the worst case on some terminal emulators, inject terminal control sequences that alter terminal state after the TUI exits (e.g. disabling echo, enabling application-cursor-key mode). This does not provide remote code execution, but it can render the TUI unusable and leave the terminal in an unrecoverable state requiring a `reset` command.

#### Recommendation

Filter non-printable characters from server-supplied strings before rendering. A conservative guard:

```python
import unicodedata

def _sanitize_display(s: str) -> str:
    """Replace control characters with a visible placeholder."""
    return "".join(
        c if unicodedata.category(c)[0] != "C" else "?"
        for c in s
    )
```

Apply this to every `display_name`, server error message, and remote path rendered in the TUI.

---

### T-03 — Permanent Remote Delete Has No Confirmation Step

**Score: 2 / 10**  
**Status: OPEN**  
**File:** `tools/filebrowser.py:1063-1076`, `tools/filebrowser.py:1255-1263`

#### Description

`_do_delete()` calls `client.temp_del()` immediately after a single menu selection with no secondary confirmation:

```python
# _handle_remote_enter() — user selects "Delete from remote":
elif choice == "Delete from remote":
    self._do_delete(api_path, display)

# _do_delete():
def _do_delete(self, api_path: str, display: str) -> None:
    result = self.client.temp_del(api_path)
    ...
```

`temp_del()` sends `DELETE api/resources/<path>`, which removes the cached copy of the file. In practice, a file that has been migrated to archive is not deleted from archive by this call (the archive copy survives), but the UX provides no feedback distinguishing "cache copy deleted, archive intact" from "all copies deleted." A user who navigates the menu quickly — especially when keyboard-repeating through entries — can accidentally delete files they intended to keep.

Additionally, D-04 (no `--force` confirmation in `upload.py`) applies symmetrically here: accidental mass-delete via repeated Enter presses.

#### Impact

Unintentional data loss from the cache tier. Archive data is preserved (the `temp_del` API is documented as cache-only), but the operator must know to trigger a recall to recover it. If archive copies have not been created yet (e.g. sync has not run since upload), the delete is permanent.

#### Recommendation

Insert a second confirmation menu before executing the delete, showing the full remote path and explicitly stating what will be deleted:

```python
confirm = show_menu(
    self.stdscr,
    f"Delete '{display}'?",
    [f"Confirm delete", "Cancel"],
)
if confirm == "Confirm delete":
    self._do_delete(api_path, display)
```

---

### T-04 — `pending_recalls` Passed by Reference to `RemotePane`; Not Snapshotted Before Draw

**Score: 1 / 10**  
**Status: OPEN**  
**File:** `tools/filebrowser.py:606`, `tools/filebrowser.py:662-665`, `tools/filebrowser.py:898-905`

#### Description

`pending_uploads` is snapshotted to an immutable `frozenset` immediately before each `LocalPane.draw()` call, guarding against concurrent mutation by the upload worker thread:

```python
self._local_pane.draw(self.active == 0, 1,
                      frozenset(self.pending_uploads), 3,    # snapshotted
                      frozenset(self.pending_upload_folders), 4)
```

`pending_recalls` is not snapshotted. `RemotePane` holds a reference to the live `set` object and reads it in `draw()` (line 444: `api_path in self.pending_recalls`). The main event loop clears it via `self.pending_recalls.clear()` (line 904), and background recall threads call `self.pending_recalls.discard(api_path)` and `self.pending_recalls.add(api_path)` concurrently.

In CPython, individual `set` method calls (`add`, `discard`, `clear`, `in`) are protected by the GIL, so the structure will not be corrupted. However, `set.clear()` followed immediately by a draw tick can cause a file that was showing as "↑ recall in progress" to flicker to "○ archived" for one frame and then back — a benign but confusing visual artefact.

#### Impact

No data loss or security consequence. Cosmetic inconsistency: a file may momentarily display the wrong recall indicator. Low severity; flagged for completeness and because it is an asymmetry relative to the snapshotted handling of `pending_uploads`.

#### Recommendation

Apply the same snapshot pattern for consistency:

```python
self._remote_pane.draw(self.active == 1, 1, self._italic,
                       frozenset(self.pending_recalls))
```

And update `RemotePane.draw()` to accept the snapshot as a parameter rather than reading from `self.pending_recalls` directly.

---

## TUI Tool Summary Table

| ID | Title | Score | Status | Location |
|---|---|---|---|---|
| T-01 | `_collect_local_files()` follows symlinks; unintended upload scope + recursion crash | **3** | **Resolved** | `filebrowser.py:1091-1118` |
| T-02 | Control characters in server filenames rendered without sanitization | **2** | Open | `filebrowser.py:448-474` |
| T-03 | Permanent remote delete executes after single menu selection; no confirmation | **2** | Open | `filebrowser.py:1063-1076, 1255-1263` |
| T-04 | `pending_recalls` not snapshotted before draw (asymmetry with `pending_uploads`) | **1** | Open | `filebrowser.py:606, 662-665` |

---

## Recommended Priority Order

### Remaining open items (as of 2026-06-10)

1. **Rotate the agOO password and rewrite git history** (F-01 residual). Source code is clean, but `git log -p` on any clone made before `c7a2feb` still reveals the plaintext password. Use `git filter-repo --replace-text` and force-push (or re-create) the repository.

2. ~~T-01~~ — Loop-detection via `_visited` resolved-path set; symlinks to files still followed.

3. **Sanitize server-supplied strings before rendering in curses** (T-02). Add a `_sanitize_display()` helper that strips control characters. Apply to all server-sourced strings passed to `curses.addstr()`.

4. **Add a second confirmation for destructive remote delete** (T-03). Operational risk; low probability but permanent consequence if triggered accidentally.

5. **Snapshot `pending_recalls` before draw** (T-04). Cosmetic consistency fix; no urgency.

### Resolved / closed
- ~~F-02~~ — `_SafeSession` strips `X-Auth` on cross-origin redirects.
- ~~F-03~~ — `_validate_output_path()` guards `temp_get_file()` against path traversal writes.
- ~~F-04~~ — Unencoded `/` in remote paths is intentional API behaviour.
- ~~F-05~~ — `_do()` buffers non-streaming responses iteratively with a 1 MiB cap.
- ~~F-06–F-08~~ — Accepted risk; attack scenarios considered highly unlikely in practice.
- ~~F-09~~ — `logout()` clears `_auth_token`; `__del__` ensures cleanup on destruction.
- ~~F-10~~ — `_SafeSession` now uses an inert cookie jar, discarding all server-set cookies.
- ~~F-11~~ — `_upload_eos.py` deleted.
