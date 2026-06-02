# Security Audit — agOO-python

**Date:** 2026-06-02  
**Auditor:** Claude Sonnet 4.6  
**Scope:** All source files in the repository (`agoo/`, `scripts/`)  
**Methodology:** Static analysis — full manual code review of every source file

---

## Executive Summary

The library core (`agoo/client.py`) contains solid defensive programming: HTTPS enforcement, TLS certificate pinning, explicit request timeouts, a redirect cap, and path-traversal validation on uploads. The most significant finding is entirely in the scripts layer: a shared plaintext password committed to version control and baked into git history.

Five of the eleven findings are in `scripts/` and would not affect consumers of the library itself. The library has two meaningful issues of its own: `temp_get_file()` applies no validation to its output path (an asymmetry with `temp_put()`), and the `X-Auth` session token is not stripped from cross-domain HTTP redirects.

No finding reaches the RCE / privilege-escalation tier. The highest-scoring issue (7/10) is credential exposure through version control.

---

## Findings

---

### F-01 — Plaintext Credentials Committed to Version Control

**Score: 7 / 10**  
**Files:** `scripts/upload.py:68-69`, `scripts/download.py:64-65`, `scripts/sync.py:43-44`, `scripts/_upload_eos.py:19`

#### Description

All four scripts contain the password `welcometoagoo` as a hard-coded default that is active whenever the `agOO_PASSWORD` environment variable is unset. The password is present in plaintext in the current HEAD and in every preceding commit that touched those files — it is permanently recoverable via `git log -p` regardless of any future edits.

```python
# upload.py, download.py, sync.py:
_PASSWORD = os.environ.get("agOO_PASSWORD", "welcometoagoo")

# _upload_eos.py:
client = Agoo(user="thomas", login="admin", password="welcometoagoo", debug=True)
```

#### Impact

An attacker with read access to the repository (e.g. if it is pushed to a public remote, shared with a contractor, or exposed via a misconfigured CI system) gains the agOO service password. With valid credentials they can:

- Read or exfiltrate all files in temp and archive storage.
- Delete files from temp storage (`temp_del`).
- Trigger arbitrary archive sync jobs.
- Terminate the backend instance (`terminate`).

The service manages file and tape-archive storage, so the data-loss and exfiltration blast radius is significant.

#### Recommendation

1. **Remove the hard-coded defaults.** Raise `ValueError` if the env var is absent instead of falling back silently.
2. **Rotate the password immediately** — the git history cannot be redacted without a force-push rewrite.
3. If retaining a development default is deemed necessary, put it only in a `.env.example` file that is in `.gitignore` and never committed.

---

### F-02 — `X-Auth` Session Token Forwarded on Cross-Domain HTTP Redirects

**Score: 4 / 10**  
**File:** `agoo/client.py:377-384`, `agoo/client.py:61`

#### Description

The `requests` library automatically strips the `Authorization` header when following a redirect to a different origin, but it does **not** strip arbitrary custom headers such as `X-Auth`. The library sets `max_redirects = 5` to limit the attack surface, but does not instruct `requests` to drop `X-Auth` on cross-origin hops.

```python
headers["X-Auth"] = self._auth_token
# ...
response = self._session.request(method, url, headers=headers, ...)
```

If the agOO server (or a MITM attacker on the network path) issues a redirect to an attacker-controlled domain, all five hops are followed and the `X-Auth` token is sent to the third-party host on every hop.

#### Impact

Requires either server compromise or a network-level MITM to exploit. Successful exploitation gives the attacker a valid session token, granting full API access for the lifetime of that token. Since `logout()` is not implemented (see F-09), the token cannot be proactively invalidated.

#### Recommendation

Use a custom `requests.Session` subclass or a `redirect` hook to strip `X-Auth` (and any other sensitive headers) before following a redirect to a different origin:

```python
from urllib.parse import urlparse

def _safe_redirect(response, *args, **kwargs):
    original = urlparse(response.url)
    redirect = urlparse(response.headers.get("Location", ""))
    if original.netloc != redirect.netloc:
        response.request.headers.pop("X-Auth", None)

self._session.hooks["response"].append(_safe_redirect)
```

---

### F-03 — Arbitrary Local File Write in `temp_get_file()` (No Output Path Validation)

**Score: 4 / 10**  
**File:** `agoo/client.py:1183-1249`

#### Description

`temp_put()` calls `_validate_local_path()` before opening any local file, rejecting null bytes and paths that resolve outside the current working directory. `temp_get_file()` — which writes to a caller-supplied `local_path` — applies no equivalent validation:

```python
def temp_get_file(self, f: str, local_path: str, ...) -> bool | None:
    response = self._get_stream("api/raw/" + uri_escape(f))
    ...
    with open(local_path, "wb") as fh:   # local_path is never validated
        for chunk in response.iter_content(...):
            fh.write(chunk)
```

Paths such as `/etc/cron.d/backdoor`, `../../.ssh/authorized_keys`, or `../../../etc/passwd` are accepted silently.

#### Impact

The severity is determined by how `local_path` is sourced by the caller. Direct impact requires:

- A web application or automation script that passes a server-controlled or user-controlled value as `local_path`, or
- A developer mistake (e.g. using the remote filename as the local path without sanitisation).

In the worst case (writing to a cron directory or SSH `authorized_keys`) this provides persistent access to the host — but that requires the calling process to run as a privileged user and `local_path` to be attacker-influenced.

#### Recommendation

Apply `_validate_local_path()` to `local_path` in `temp_get_file()`, or introduce an equivalent `_validate_output_path()` that at minimum rejects null bytes and absolute paths outside a designated download directory.

---

### F-04 — Remote Path Traversal via Unencoded `..` Segments

**Score: 3 / 10**  
**File:** `agoo/client.py:49`, all methods that call `uri_escape()`

#### Description

The library imports `urllib.parse.quote` as `uri_escape` without specifying the `safe` parameter, so the default `safe='/'` applies. Forward slashes and dots in remote paths are left unencoded:

```python
from urllib.parse import quote as uri_escape  # default safe='/'

# A caller passing "../other-user/secret.tar" produces:
"api/resources/../other-user/secret.tar"
```

The Perl original used `URI::Escape::uri_escape`, which encodes forward slashes and dots by default (`safe=''`), so `foo/bar` became `foo%2Fbar` at the HTTP layer. The Python port changes this semantic silently.

Whether path traversal is achievable depends on how the agOO server normalises request paths before routing. HTTP servers that perform path canonicalisation before ACL checks are susceptible; servers that parse the path literally are not.

#### Impact

If the server is susceptible, a caller can read, stat, or delete files outside their own storage namespace by supplying a path like `../admin/sensitive-file`. Exploitation requires the caller to supply a malicious remote path, which is likely only relevant if the path is derived from untrusted input (e.g. a filename received from a third party).

#### Recommendation

Explicitly set `safe=''` to encode all non-unreserved characters including `/` and `.`:

```python
from urllib.parse import quote

def _remote_escape(path: str) -> str:
    return quote(path, safe='')
```

This matches the Perl behaviour and prevents path components from being interpreted as URL path separators by the server.

---

### F-05 — Unbounded Response Body Buffering in Non-Streaming API Calls

**Score: 3 / 10**  
**File:** `agoo/client.py:321-445` (`_do()`), called by `stat()`, `get_usage()`, `login()`, `temp_sum()`

#### Description

`_do()` uses `stream=False` by default, which instructs `requests` to download and buffer the complete response body before returning. For API calls that are expected to return small JSON blobs (`stat`, `get_usage`, `login`, `temp_sum`), no size limit is enforced on the response:

```python
response = self._session.request(method, url, data=body,
                                  headers=headers, timeout=_DEFAULT_TIMEOUT,
                                  stream=stream)   # stream=False for most calls
```

A compromised or malicious server can respond to `GET /api/usage` with a 10 GB body. The `requests` library will buffer the entire payload before returning, exhausting the process's memory.

`temp_get()` correctly addresses this for file downloads (with a `max_bytes` cap and streaming), but the fix was not extended to the other methods.

#### Impact

Denial-of-service against the calling process. Does not directly enable data exfiltration or code execution. Requires the server to be compromised or a MITM on the connection.

#### Recommendation

Set a hard response-size limit in `_do()` for non-streaming paths, either by switching those calls to `stream=True` and capping the read, or by checking `Content-Length` before calling `response.text`:

```python
MAX_API_RESPONSE = 1 * 1024 * 1024  # 1 MiB is generous for any metadata call
cl = response.headers.get("Content-Length")
if cl and int(cl) > MAX_API_RESPONSE:
    response.close()
    self._error = f"{path}: response too large ({cl} bytes)"
    return None
```

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

**Score: 1 / 10**  
**File:** `scripts/_upload_eos.py:19`

#### Description

The internal migration script unconditionally enables debug mode, which prints every HTTP method, path, offset, and job ID to stdout:

```python
client = Agoo(user="thomas", login="admin", password="welcometoagoo", debug=True)
```

Debug output includes remote file paths, TUS upload offsets, sync job IDs, and error details. If stdout is captured by a logging system or CI pipeline, this operational metadata is stored in potentially world-readable logs.

Note that the auth token and password are **not** printed (the library correctly avoids this), but the combination of username, file paths, and job IDs may be sensitive for operational security.

#### Impact

Information disclosure in logs. Low severity on its own, but combined with F-01 (credentials in the same file) the script is already a high-risk artefact.

#### Recommendation

Remove `debug=True` or gate it on an environment variable:

```python
debug = os.environ.get("agOO_DEBUG", "").lower() in ("1", "true", "yes")
client = Agoo(..., debug=debug)
```

---

## Summary Table

| ID | Title | Score | Location |
|---|---|---|---|
| F-01 | Plaintext credentials in version-controlled files and git history | **7** | `scripts/*.py` |
| F-02 | `X-Auth` token forwarded on cross-domain HTTP redirects | **4** | `client.py:_do()` |
| F-03 | Arbitrary local file write in `temp_get_file()` | **4** | `client.py:temp_get_file()` |
| F-04 | Remote path traversal via unencoded `..` in `uri_escape` | **3** | `client.py` (all remote-path calls) |
| F-05 | Unbounded response body buffering in non-streaming API calls | **3** | `client.py:_do()` |
| F-06 | TOCTOU race between path validation and `open()` | **2** | `client.py:temp_put()` |
| F-07 | Predictable sentinel path enables local symlink attack | **2** | `client.py:async_synchronize()` |
| F-08 | `Referer` header leaks API username to `start_url` | **1** | `client.py:_do()` |
| F-09 | No session revocation (`logout()` unimplemented) | **1** | `client.py:logout()` |
| F-10 | `requests.Session` silently accumulates server cookies | **1** | `client.py:__init__()` |
| F-11 | `debug=True` hard-coded in `_upload_eos.py` | **1** | `scripts/_upload_eos.py` |

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

1. **Rotate the agOO password** and rewrite git history to remove it from all commits (F-01). This is the only finding with an immediate, actionable risk.
2. **Add output-path validation to `temp_get_file()`** — a one-line fix that closes a meaningful asymmetry (F-03).
3. **Strip `X-Auth` on cross-origin redirects** using a response hook (F-02).
4. **Switch to `safe=''` in `uri_escape`** to match Perl semantics and prevent server-side path traversal (F-04).
5. Remaining findings (F-05 through F-11) are low priority and can be addressed in a single hardening pass.
