# Security Audit — agOO-python

**Audit date:** 2026-06-02  
**Last updated:** 2026-06-03 (mitigations applied — F-01, F-02, F-03, F-11)  
**Auditor:** Claude Sonnet 4.6  
**Scope:** All source files in the repository (`agoo/`, `scripts/`)  
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

### F-03 — Arbitrary Local File Write in `temp_get_file()` (No Output Path Validation)

**Score: 4 / 10 → residual: 0 / 10 (fully mitigated)**  
**Status: RESOLVED — commit `f1331e4`**  
**File:** `agoo/client.py:264-290`, `agoo/client.py:1267`

#### Description

`temp_put()` calls `_validate_local_path()` before opening any local file, rejecting null bytes and paths that resolve outside the current working directory. `temp_get_file()` — which writes to a caller-supplied `local_path` — previously applied no equivalent validation, silently accepting paths such as `../../.ssh/authorized_keys`.

#### Mitigation applied (commit `f1331e4`, 2026-06-02)

A new `_validate_output_path()` static method was added to `Agoo` (`client.py:264`) and called at the top of `temp_get_file()` before any network I/O (`client.py:1267`).

The validator is deliberately less restrictive than `_validate_local_path()` to support legitimate use cases such as writing to external mounts:

- **Null bytes:** always rejected.
- **Relative traversal:** relative paths that resolve outside the CWD (e.g. `../../etc/passwd`) are rejected.
- **Absolute paths:** permitted — the caller is assumed to have explicitly chosen the destination (e.g. `/mnt/nas/backup.tar.gz`).

```python
@staticmethod
def _validate_output_path(path: str) -> None:
    if "\x00" in path:
        raise ValueError(f"output path contains a null byte: {path!r}")
    if not os.path.isabs(path):
        cwd = Path.cwd().resolve()
        resolved = (cwd / path).resolve()
        resolved.relative_to(cwd)   # raises ValueError if outside CWD
```

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

**Score: 1 / 10**  
**File:** `agoo/client.py:489-495`

#### Description

`logout()` raises `NotImplementedError`. The session token stored in `self._auth_token` cannot be invalidated through the client API. If the token leaks (e.g. via F-02, log scraping, or a process-memory dump), there is no programmatic way to revoke it:

```python
def logout(self):
    raise NotImplementedError("logout is not yet implemented")
```

#### Impact

Increases the window of opportunity for token-reuse attacks. Low severity in isolation; combines badly with F-02.

#### Recommendation

Implement a `POST /api/logout` call (if the server supports it) and clear `self._auth_token = None` on completion. As a minimum, clear the in-memory token on object destruction via `__del__` so at least the client-side copy is discarded.

---

### F-10 — `requests.Session` Silently Accumulates Server-Set Cookies

**Score: 1 / 10**  
**File:** `agoo/client.py:167-169`

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

#### Recommendation

Disable cookie storage explicitly to make the authentication model unambiguous:

```python
self._session.cookies = requests.cookies.RequestsCookieJar()
# or: install a null cookie policy
```

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
| F-03 | Arbitrary local file write in `temp_get_file()` | **4** | — | **Resolved** (`f1331e4`) | `client.py:_validate_output_path()` |
| F-04 | Remote paths with unencoded `/` | **3** | N/A | **By design** — intentional API feature | `client.py:uri_escape` |
| F-05 | Unbounded response body buffering in non-streaming API calls | **3** | — | **Resolved** (`106aa43`) | `client.py:_do()` |
| F-06 | TOCTOU race between path validation and `open()` | **2** | **2** | Open | `client.py:temp_put()` |
| F-07 | Predictable sentinel path enables local symlink attack | **2** | **2** | Open | `client.py:async_synchronize()` |
| F-08 | `Referer` header leaks API username to `start_url` | **1** | **1** | Open | `client.py:_do()` |
| F-09 | No session revocation (`logout()` unimplemented) | **1** | **1** | Open | `client.py:logout()` |
| F-10 | `requests.Session` silently accumulates server cookies | **1** | **1** | Open | `client.py:__init__()` |
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

## Recommended Priority Order

### Remaining open items (as of 2026-06-03)

1. **Rotate the agOO password and rewrite git history** (F-01 residual). Source code is clean, but `git log -p` on any clone made before `c7a2feb` still reveals the plaintext password. Use `git filter-repo --replace-text` and force-push (or re-create) the repository.
2. Remaining findings (F-06 through F-10) are low priority and can be addressed in a single hardening pass.

### Resolved
- ~~F-02~~ — `_SafeSession` strips `X-Auth` on cross-origin redirects.
- ~~F-03~~ — `_validate_output_path()` guards `temp_get_file()` against path traversal writes.
- ~~F-05~~ — `_do()` buffers non-streaming responses iteratively with a 1 MiB cap.
- ~~F-11~~ — `_upload_eos.py` deleted.
