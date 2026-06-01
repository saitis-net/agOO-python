# agoo/client.py
#
# DESCRIPTION
#   Python port of the Perl agoo.pm support library.
#   Provides a client for the agOO remote file-storage / archive service.
#
# ARCHITECTURE OVERVIEW
#   The agOO service is a web application that manages a file hierarchy with
#   two storage tiers:
#     - "temp"    : fast, online storage accessible via REST API calls
#     - "archive" : slower, tape/offline storage managed asynchronously
#
#   Authentication uses a session token returned by POST /api/login and then
#   sent on every subsequent request in the custom `X-Auth` HTTP header
#   (not a cookie, not Bearer/Basic).
#
#   Large file uploads use the TUS resumable-upload protocol (tus.io):
#     1. POST  /api/tus/<path>  announces the total file length
#     2. PATCH /api/tus/<path>  sends the payload in chunks, each chunk
#        carrying an `Upload-Offset` header so the server can resume after
#        a partial failure.
#
#   Async archive operations are triggered by uploading a small "sentinel"
#   file (_sgbdb/archUnarchAsked) whose presence causes the backend to
#   queue a migration job.  The caller polls for completion by checking
#   whether a corresponding "finished.<id>" file has appeared in temp.
#
# NOTES
#   - Authenticates with X-Auth header only (not cookies).
#   - If the backend is idle it may be in a "stopped" state; a 502 response
#     triggers an automatic wake-up via the start_url, followed by a re-login
#     and a transparent retry (up to 4 total attempts).
#   - Assumes a POSIX environment; no Windows path handling.
#   - base_url must use HTTPS; HTTP is rejected at construction time.
#
# BUGS
#   - temp_get() loads the entire file into memory (see LWP comment in original).
#   - No checksum support in stat() yet.
#
# DEPENDENCIES
#   requests  (pip install requests)

import json
import os
import re                                       # moved to module level (was inside async_completed)
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as uri_escape    # equivalent of URI::Escape::uri_escape

import requests                                 # equivalent of LWP::UserAgent

# Connect timeout (s) and read timeout (s) applied to every HTTP request.
# Without a timeout, a stalled server hangs the client indefinitely.
# (connect, read) — the two-tuple form mirrors requests' recommendation.
_DEFAULT_TIMEOUT = (10, 60)

# Maximum number of HTTP redirects the session will follow per request.
# Unlimited redirects could be exploited to leak the X-Auth token to a
# third-party domain controlled by the server operator.
_MAX_REDIRECTS = 5


class Agoo:
    """Client for the agOO remote storage service.

    Instantiate once, call login(), then use the temp_* / schedule_* /
    async_* methods to interact with the service.

    Keyword arguments accepted by the constructor
    ---------------------------------------------
    base_url   : root URL of the agOO installation (must be https://)
    start_url  : URL that wakes a stopped agOO instance (must be https://)
    login      : agOO username (defaults to 'admin')
    password   : agOO password  (or set env var agOO_PASSWORD)
    user       : API user name   (or set env var agOO_USER)
    debug      : set to True to print diagnostic output to stderr
    io_size    : read chunk size for uploads, in bytes (default 10 MB)
    """

    # -------------------------------------------------------------------
    # Construction
    # -------------------------------------------------------------------

    def __init__(self, **cfg):
        # ----------------------------------------------------------------
        # Start with a dict of hard-coded defaults.
        # Using a nested dict mirrors the Perl `$self->{config}` hash-ref.
        # ----------------------------------------------------------------
        self._config = {
            "base_url":  "https://agoo.saitis.net",
            "start_url": "https://agoo.saitis.net/cgi-bin/start-fb.cgi",
            "login":     "admin",           # the agOO *username* used at login time
            "io_size":   10 * 1024 * 1024,  # 10 MiB upload chunk size
        }

        # ----------------------------------------------------------------
        # Apply any caller-supplied overrides for the recognised keys.
        # Unknown keys are silently ignored (mirrors "# ignore the rest").
        # ----------------------------------------------------------------
        for key in ("base_url", "start_url", "login", "user", "password", "debug", "io_size"):
            if key in cfg:
                self._config[key] = cfg[key]

        # ----------------------------------------------------------------
        # SECURITY: Enforce HTTPS-only for both endpoint URLs.
        # Allowing http:// would transmit the X-Auth session token and the
        # login password in cleartext over the network.
        # ----------------------------------------------------------------
        for url_key in ("base_url", "start_url"):
            if not self._config[url_key].startswith("https://"):
                raise ValueError(
                    f"'{url_key}' must use HTTPS (got: {self._config[url_key]!r})"
                )

        # ----------------------------------------------------------------
        # Resolve the two required credentials:
        #   user     -> env var agOO_USER
        #   password -> env var agOO_PASSWORD
        # If neither the kwarg nor the env var is present, raise immediately
        # so callers get a clear error at construction time rather than on
        # the first API call.
        # ----------------------------------------------------------------
        auth_env = {
            "user":     "agOO_USER",
            "password": "agOO_PASSWORD",
        }
        for key, env_var in auth_env.items():
            if key not in self._config:                   # not supplied as kwarg
                env_value = os.environ.get(env_var)
                if env_value is not None:
                    self._config[key] = env_value         # fall back to env var
                else:
                    raise ValueError(
                        f"you called Agoo() without setting '{key}' "
                        f"(nor the {env_var} environment variable)"
                    )

        # ----------------------------------------------------------------
        # Session state:
        #   _auth_token : the opaque string returned by POST /api/login;
        #                 None until login() is called successfully.
        #   _error      : human-readable description of the last failure.
        # ----------------------------------------------------------------
        self._auth_token: str | None = None
        self._error: str | None = None

        # ----------------------------------------------------------------
        # Persistent HTTP session (equivalent to LWP::UserAgent instance).
        # Using requests.Session() allows connection keep-alive and a single
        # place to attach default headers or limits.
        #
        # SECURITY: verify=True makes TLS certificate validation explicit
        # rather than relying on the library default (which is also True,
        # but an explicit setting resists accidental override via env vars
        # like CURL_CA_BUNDLE or REQUESTS_CA_BUNDLE pointing to a rogue CA).
        #
        # SECURITY: max_redirects prevents redirect-loop DoS and limits the
        # window for cross-domain redirect attacks that could leak X-Auth.
        # ----------------------------------------------------------------
        self._session = requests.Session()
        self._session.verify = True
        self._session.max_redirects = _MAX_REDIRECTS

    # -------------------------------------------------------------------
    # Public helpers
    # -------------------------------------------------------------------

    def url(self) -> str:
        """Return the per-user base URL: <base_url>/<user>.

        All API paths are relative to this URL.
        """
        # Mirrors: $self->{config}->{base_url} . '/' . $self->{config}->{user}
        return self._config["base_url"] + "/" + self._config["user"]

    def error(self) -> str | None:
        """Return the human-readable error from the last failed operation."""
        return self._error

    # -------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------

    def _debug(self, *args) -> None:
        """Print a diagnostic line to stderr when debug mode is enabled.

        Mirrors: print STDERR @rest, "\\n" if defined($self->{config}->{debug})

        SECURITY: callers must never pass the auth token or password as
        arguments; debug output may end up in log files.
        """
        if self._config.get("debug"):
            # Use sep="" so the caller can pass multiple string fragments
            # just like the Perl varargs `@rest`.
            print("DEBUG:", *args, flush=True)

    @staticmethod
    def _dirname(path: str) -> str:
        """Return the directory component of a path, stripping the filename.

        Mirrors the Perl regex: if ($path =~ /^(.*)[/]+[^/]*$/) { return $1 }
        Returns the path unchanged when there is no directory separator.
        """
        # Path.parent returns '.' for a bare filename; we want the original
        # path back in that case to match the Perl fallback `return $path`.
        parent = str(Path(path).parent)
        return path if parent == "." else parent

    @staticmethod
    def _validate_local_path(path: str) -> None:
        """Raise ValueError if `path` looks unsafe for local file operations.

        SECURITY: temp_put() opens a local file by caller-supplied path and
        sends its contents to the remote server.  Without this check a caller
        (or a compromised config) could exfiltrate arbitrary files, e.g.
        temp_put("../../etc/shadow").

        Checks applied
        --------------
        - Null bytes: rejected; they terminate C strings and may confuse the OS.
        - Path traversal: the resolved (canonical) path must sit inside the
          current working directory so that only files the caller explicitly
          placed there can be uploaded.
        """
        if "\x00" in path:
            raise ValueError(f"path contains a null byte: {path!r}")

        cwd = Path.cwd().resolve()
        resolved = (cwd / path).resolve()   # resolve symlinks and ".." components

        # resolved.is_relative_to(cwd) requires Python 3.9+; the equivalent
        # comparison below works on 3.8+ as well.
        try:
            resolved.relative_to(cwd)
        except ValueError:
            raise ValueError(
                f"path escapes the working directory: {path!r} resolves to {resolved}"
            )

    @staticmethod
    def _stamp_unique() -> str:
        """Generate a timestamp + 6 random bytes expressed as a hex string.

        The result is used as a unique job identifier for async operations.
        Mirrors Crypt::URandom::urandom(6) + sprintf timestamp.

        Format: YYYYMMDDHHmmss + 12 hex digits  (26 characters total)
        """
        # Timestamp portion: local time formatted as YYYYMMDDHHmmss
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d%H%M%S")

        # 48-bit random portion: os.urandom is the POSIX equivalent of
        # Crypt::URandom::urandom and reads from /dev/urandom.
        random_bytes = os.urandom(6)                  # 6 bytes = 48 bits
        random_hex = random_bytes.hex()               # equivalent of unpack("H*", $r)

        return timestamp + random_hex

    # -------------------------------------------------------------------
    # HTTP method shims
    #
    # Each of _get / _post / _patch / _delete is a thin wrapper around
    # _do() that pre-fills the HTTP verb.  This mirrors the four Perl subs
    # that each call $self->_do("verb", ...).
    # -------------------------------------------------------------------

    def _get(self, path: str, **headers):
        return self._do("GET", path, **headers)

    def _post(self, path: str, body=None, **headers):
        return self._do("POST", path, body=body, **headers)

    def _patch(self, path: str, body=None, **headers):
        return self._do("PATCH", path, body=body, **headers)

    def _delete(self, path: str, **headers):
        return self._do("DELETE", path, **headers)

    def _do(self, method: str, path: str, body=None, **extra_headers):
        """Execute an authenticated HTTP request with automatic retry.

        Retry logic (mirrors the Perl `foreach my $attempt (qw/one two three four/)`)
        -------------------------------------------------------------------------------
        Up to 4 attempts are made.  On a 502 (Bad Gateway) the agOO instance
        is probably stopped; we try to wake it by hitting start_url, wait 5 s,
        then re-authenticate before the next attempt.

        Special case: a 502 on 'api/terminate' is treated as success because
        the server may shut down before it can send a 200 response.

        Parameters
        ----------
        method        : HTTP verb in uppercase ('GET', 'POST', ...)
        path          : API path relative to self.url() (e.g. 'api/resources/foo')
        body          : raw request body bytes, or None
        extra_headers : additional HTTP headers as keyword arguments

        SECURITY: the X-Auth token is added to headers here but is never
        passed to _debug() to avoid token exposure in logs.
        """
        self._debug(f"_do: {method} {path}")

        do_login = False   # flag: re-authenticate before the next attempt

        for attempt in ("one", "two", "three", "four"):
            # Re-authenticate if the previous attempt received a 502 and the
            # start_url wake-up succeeded.
            if do_login:
                self.login()   # errors are intentionally ignored here
                do_login = False

            # Build the full URL and the header dict.
            url = self.url() + "/" + path

            headers = {}
            if self._auth_token is not None:
                # The X-Auth header carries the opaque session token
                # (not "Bearer <token>", just the raw token string).
                headers["X-Auth"] = self._auth_token
            headers.update(extra_headers)   # caller-supplied headers take precedence

            try:
                # SECURITY: timeout=_DEFAULT_TIMEOUT ensures a stalled or
                # slow-responding server cannot block the caller indefinitely.
                # The two-tuple is (connect_timeout, read_timeout) in seconds.
                response = self._session.request(
                    method,
                    url,
                    data=body,              # raw bytes payload (used by POST/PATCH)
                    headers=headers,
                    timeout=_DEFAULT_TIMEOUT,
                )
            except requests.Timeout:
                self._error = f"{path}: request timed out (attempt {attempt})"
                self._debug("_do: TIMEOUT", self._error)
                return None
            except requests.ConnectionError as exc:
                self._error = f"{path}: connection error: {exc} (attempt {attempt})"
                self._debug("_do: CONNECTION ERROR", self._error)
                return None

            if response.ok:                 # HTTP 2xx
                self._debug(f"_do: {method} {path} SUCCESS")
                return response

            # Request failed -- record the error with attempt metadata.
            self._error = (
                f"{path}: failed {response.status_code}/{response.reason}"
                f" (attempt {attempt})"
            )
            self._debug("_do: FAILED", self._error)

            if response.status_code == 502:
                # 502 means the agOO backend process is not running.

                if path == "api/terminate":
                    # Termination may race the server shutdown; treat as OK.
                    self._debug("_do: api/terminate assumed OK")
                    return True

                # Try to start the stopped instance via the start_url endpoint.
                try:
                    wake = self._session.get(
                        self._config["start_url"],
                        headers={"Referer": self.url() + "/"},
                        timeout=_DEFAULT_TIMEOUT,   # also guard the wake-up call
                    )
                except requests.Timeout:
                    self._error += f" (start_url timed out)"
                    return None
                except requests.ConnectionError as exc:
                    self._error += f" (start_url connection error: {exc})"
                    return None

                if wake.ok:
                    time.sleep(5)            # give the backend time to initialise

                    if path != "api/login":
                        # We will need a fresh token on the next attempt.
                        do_login = True
                else:
                    self._error += (
                        f" (in addition {self._config['start_url']}"
                        f" failed: {wake.reason})"
                    )
                    return None
            else:
                # Non-502 errors are not retried.
                return None

        # Exhausted all four attempts without success.
        self._error += " (starting instance did not work)"
        return None

    # -------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------

    def login(self) -> bool:
        """Authenticate with the agOO service and store the session token.

        Sends the credentials as JSON to POST /api/login.
        The response body is a plain (non-JSON) session token string.

        Returns True on success, False on failure.
        """
        self._debug("login")

        # Discard any existing token before attempting a fresh login so that
        # _do() will not send a stale X-Auth header on the login request.
        self._auth_token = None

        payload = {
            "username":  self._config["login"],
            "recaptcha": "",                    # field required by the form but unused
            "password":  self._config["password"],
        }

        # POST the credentials as a JSON body.
        response = self._post(
            "api/login",
            body=json.dumps(payload).encode(),
            **{"Content-Type": "application/json"},
        )

        if response is not None:
            # The server returns the token as a plain text body, not JSON.
            token = response.text.strip()
            if not token:
                self._error = "login succeeded but server returned an empty token"
                return False
            self._auth_token = token
            return True

        return False

    def logout(self):
        """Log out from the agOO service.

        Not yet implemented in the original Perl module.
        The session is probably invalidated automatically when the instance stops.
        """
        raise NotImplementedError("logout is not yet implemented")

    # -------------------------------------------------------------------
    # Actions on temp storage (and archive metadata)
    # -------------------------------------------------------------------

    def stat(self, what: str) -> dict | None:
        """Retrieve metadata for a file in temp or archive storage.

        Parameters
        ----------
        what : remote path to the file

        Returns a dict parsed from the JSON response, or None on failure.
        Note: checksums are not yet included in the response (see BUGS).
        """
        if self._auth_token is None:
            raise RuntimeError("call login() first")

        response = self._get("api/resources/" + uri_escape(what))
        if response is not None:
            # SECURITY: guard against malformed JSON from the server, which
            # would otherwise raise an unhandled exception to the caller.
            try:
                return json.loads(response.text)
            except json.JSONDecodeError as exc:
                self._error = f"stat: server returned invalid JSON: {exc}"
                return None

        return None

    # -------------------------------------------------------------------
    # Actions on temp storage
    # -------------------------------------------------------------------

    def get_usage(self) -> dict | None:
        """Fetch storage usage statistics from the agOO service.

        Calls GET api/usage and returns a dict with the following fields:

          total     : total cache (temp) space in bytes
          used      : used cache space in bytes
          totalArch : total archival drive size in bytes
          usedArch  : used archival drive space in bytes

        Available cache space = total - used.

        Returns the parsed dict on success, or None on failure.
        """
        response = self._get("api/usage")
        if response is not None:
            try:
                return json.loads(response.text)
            except json.JSONDecodeError as exc:
                self._error = f"get_usage: server returned invalid JSON: {exc}"
                return None
        return None

    def temp_sum(self, f: str, hash_type: str) -> str | None:
        """Retrieve a checksum for a file stored in temp.

        Useful until stat() includes sha256 sums in its response.

        Parameters
        ----------
        f         : remote path to the file
        hash_type : hash algorithm name as understood by the server (e.g. 'sha256')

        Returns the hex checksum string, or None on failure.
        """
        response = self._get(
            "api/resources/" + uri_escape(f) + "?checksum=" + uri_escape(hash_type)
        )
        if response is not None:
            # SECURITY: guard against malformed JSON from the server.
            try:
                data = json.loads(response.text)
            except json.JSONDecodeError as exc:
                self._error = f"temp_sum: server returned invalid JSON: {exc}"
                return None
            # Navigate the nested JSON: {"checksums": {"sha256": "<hex>"}}
            checksums = data.get("checksums", {})
            if hash_type in checksums:
                return checksums[hash_type]

        return None

    def temp_put_fake(self, f: str):
        """Upload an empty (zero-byte) placeholder file to temp storage.

        Uses the TUS resumable-upload protocol:
          1. POST  announces Upload-Length: 0
          2. PATCH sends the empty body

        This is used when the server needs to know a file exists before its
        content is ready (e.g. to reserve a remote path).

        Returns the PATCH response object, or None on failure.
        """
        fake_body = b""   # zero-byte payload

        # Step 1: Announce the upload (TUS initiation request).
        response = self._post(
            "api/tus/" + uri_escape(f) + "?override=false",
            **{"Upload-Length": str(len(fake_body))},
        )
        if response is None:
            self._error = f"could not create remote file {f}: {self._error}"
            return None

        # Step 2: Send the (empty) content (TUS PATCH request).
        response = self._patch(
            "api/tus/" + uri_escape(f),
            body=fake_body,
            **{
                "Content-Type":   "application/offset+octet-stream",
                "Tus-Resumable":  "1.0.0",
                "Upload-Offset":  "0",
            },
        )
        if response is None:
            self._error = f"could not send (fake) data to remote file {f}: {self._error}"
            return None

        return response

    def temp_put(self, f: str) -> bool | None:
        """Upload a local file to temp storage using the TUS resumable protocol.

        The file is read and sent in chunks of io_size bytes so that large
        files can be uploaded without consuming excessive memory.

        TUS upload sequence
        -------------------
        1. POST  /api/tus/<path>?override=false  with Upload-Length: <total bytes>
           Registers the upload slot on the server.
        2. PATCH /api/tus/<path>  (repeated for each chunk)
           Sends file data; each chunk declares its byte offset via
           Upload-Offset so the server can detect gaps or duplicates.

        Parameters
        ----------
        f : local file path (must reside within the current working directory)

        Returns True on success, None on failure.

        SECURITY: _validate_local_path() is called first to reject null bytes
        and path-traversal sequences that could exfiltrate arbitrary files.
        """
        # SECURITY: reject paths that escape the working directory before any
        # file I/O is attempted.
        try:
            self._validate_local_path(f)
        except ValueError as exc:
            self._error = str(exc)
            return None

        try:
            file_size = os.path.getsize(f)   # total bytes, equivalent to CORE::stat size
        except OSError as exc:
            self._error = f"cannot open local file {f}: {exc}"
            return None

        # --- Space check: refuse early if the cache cannot fit the file ---
        # Fetching usage before the TUS POST avoids allocating a remote slot
        # for an upload that is guaranteed to fail or fill the cache.
        usage = self.get_usage()
        if usage is None:
            self._error = (
                f"cannot verify available cache space before uploading '{f}': "
                + (self._error or "get_usage() returned no data")
            )
            return None

        total = usage.get("total")
        used  = usage.get("used")
        if total is None or used is None:
            # The server responded but the expected fields are absent; treat
            # this as a fatal error rather than silently skipping the check.
            self._error = (
                f"get_usage: response is missing 'total' or 'used' fields: {usage}"
            )
            return None

        available = total - used
        if file_size > available:
            self._error = (
                f"insufficient cache space for '{f}': "
                f"file is {file_size:,} bytes but only {available:,} bytes available "
                f"({used:,} of {total:,} bytes used)"
            )
            return None

        self._debug(
            f"space check OK: file={file_size:,} B  "
            f"available={available:,} B  used={used:,}/{total:,} B"
        )

        # --- Step 1: Announce the upload ---
        response = self._post(
            "api/tus/" + uri_escape(f) + "?override=false",
            **{"Upload-Length": str(file_size)},
        )
        if response is None:
            self._error = f"could not create remote file {f}: {self._error}"
            return None

        # --- Step 2: Send file content in chunks ---
        offset = 0
        try:
            with open(f, "rb") as fh:
                while True:
                    # Read up to io_size bytes from the current file position.
                    chunk = fh.read(self._config["io_size"])

                    if not chunk:
                        # EOF reached -- upload complete.
                        break

                    response = self._patch(
                        "api/tus/" + uri_escape(f),
                        body=chunk,
                        **{
                            "Content-Type":  "application/offset+octet-stream",
                            "Tus-Resumable": "1.0.0",
                            "Upload-Offset": str(offset),  # server uses this to detect gaps
                        },
                    )
                    if response is None:
                        self._error = f"could not send data to remote file {f}: {self._error}"
                        return None

                    offset += len(chunk)    # advance the logical byte offset

        except OSError as exc:
            self._error = f"read error from local file {f}: {exc}"
            return None

        return True

    def batch_put(self, files: list[str], poll_interval: int = 30) -> bool | None:
        """Upload a list of local files, automatically batching and syncing.

        Use this method when the combined size of all files may exceed the
        available cache (temp) space.  The algorithm splits the file list into
        batches that each fit in the available cache, uploads one batch, waits
        for an archive sync to reclaim that cache space, then continues with
        the next batch — repeating until every file has been uploaded.

        For a single file that is already known to fit, temp_put() is simpler.

        Batching algorithm (greedy, preserves file order)
        -------------------------------------------------
        At the start of each batch:
          1. get_usage() is called to measure currently available cache space.
          2. Files are considered in order.  A file is added to the current
             batch as long as the running total of that batch would not exceed
             available space.  Files that would overflow the batch are deferred
             to the next batch.
          3. Every file in the batch is uploaded with temp_put().
          4. If deferred files remain, async_synchronize() is called to trigger
             an archive migration of the uploaded batch, then async_completed()
             is polled every poll_interval seconds until the job finishes.
             The freed cache space allows the next batch to proceed.
          5. Steps 1-4 repeat until all files have been uploaded.

        A file whose size alone exceeds the total cache capacity can never be
        uploaded; this is detected up-front and reported as a clear error.

        If, after a sync, no files fit in the newly available space (i.e. the
        server did not reclaim the expected cache), the method fails rather
        than looping forever.

        Parameters
        ----------
        files         : ordered list of local file paths to upload.
        poll_interval : seconds to wait between async_completed() polls
                        while waiting for an archive sync to finish (default 30).

        Returns
        -------
        True  — every file was uploaded successfully.
        None  — an error occurred; self.error() contains a description.
        """

        # ------------------------------------------------------------------
        # Phase 1 — validate paths and collect sizes before any network I/O.
        #
        # Doing this up-front means we catch bad paths, missing files, and
        # path-traversal attempts in one pass rather than discovering them
        # mid-upload after part of the data is already on the server.
        # ------------------------------------------------------------------
        file_entries: list[tuple[str, int]] = []  # (local_path, size_in_bytes)

        for f in files:
            # Reject null bytes and paths that escape the working directory
            # (delegates to the same helper used by temp_put).
            try:
                self._validate_local_path(f)
            except ValueError as exc:
                self._error = str(exc)
                return None

            try:
                size = os.path.getsize(f)
            except OSError as exc:
                self._error = f"cannot stat local file '{f}': {exc}"
                return None

            file_entries.append((f, size))

        if not file_entries:
            # Nothing to do — treat as success so callers can pass empty lists.
            return True

        # ------------------------------------------------------------------
        # Phase 2 — fetch current usage and verify that each individual file
        # fits within the total cache capacity.
        #
        # This is a permanent constraint: a file larger than the total cache
        # can never be uploaded regardless of how many sync cycles we run.
        # Catching this now avoids an infinite loop later.
        # ------------------------------------------------------------------
        usage = self.get_usage()
        if usage is None:
            self._error = (
                "batch_put: cannot fetch storage usage: "
                + (self._error or "get_usage() returned no data")
            )
            return None

        total_cache = usage.get("total")
        if total_cache is None:
            self._error = f"batch_put: usage response is missing 'total' field: {usage}"
            return None

        for f, size in file_entries:
            if size > total_cache:
                self._error = (
                    f"batch_put: '{f}' ({size:,} bytes) exceeds the total cache "
                    f"capacity ({total_cache:,} bytes) and can never be uploaded"
                )
                return None

        # ------------------------------------------------------------------
        # Phase 3 — greedy batching loop.
        #
        # `pending` holds the files not yet uploaded, in original order.
        # Each iteration of the while-loop processes one batch.
        # ------------------------------------------------------------------
        pending = list(file_entries)   # mutable working copy
        batch_number = 0

        while pending:
            # Re-read usage at the top of every batch so that space freed by
            # the previous sync cycle is correctly reflected.
            usage = self.get_usage()
            if usage is None:
                self._error = (
                    "batch_put: cannot fetch storage usage at start of batch: "
                    + (self._error or "get_usage() returned no data")
                )
                return None

            used      = usage.get("used", 0)
            total     = usage.get("total", 0)
            available = total - used   # bytes free in the cache right now

            self._debug(
                f"batch_put: cache available={available:,} B  "
                f"used={used:,}/{total:,} B  "
                f"{len(pending)} file(s) remaining"
            )

            # ---------------------------------------------------------------
            # Build the current batch.
            #
            # Walk `pending` in order and keep adding files as long as their
            # cumulative size does not exceed `available`.  Files that do not
            # fit are deferred to `next_pending` (the next batch).
            #
            # `batch_committed` tracks how many bytes are already earmarked
            # for this batch so we don't double-count them when considering
            # the next file.
            # ---------------------------------------------------------------
            current_batch: list[tuple[str, int]] = []
            batch_committed = 0   # bytes already assigned to this batch
            next_pending: list[tuple[str, int]] = []

            for f, size in pending:
                if batch_committed + size <= available:
                    # This file fits in the current batch alongside everything
                    # already assigned to it.
                    current_batch.append((f, size))
                    batch_committed += size
                else:
                    # This file would overflow the available space; defer it.
                    next_pending.append((f, size))

            if not current_batch:
                # Nothing fit at all.  The cache was not freed after the last
                # sync (or it was consumed by an external process).  Failing
                # here prevents an infinite loop.
                self._error = (
                    f"batch_put: no files fit in available cache space "
                    f"({available:,} bytes after sync). "
                    f"The cache may not have been reclaimed by the server. "
                    f"Remaining files: {[f for f, _ in pending]}"
                )
                return None

            batch_number += 1
            batch_total_size = sum(size for _, size in current_batch)
            self._debug(
                f"batch_put: starting batch {batch_number} — "
                f"{len(current_batch)} file(s), {batch_total_size:,} bytes"
            )

            # ---------------------------------------------------------------
            # Upload every file in the current batch via temp_put().
            #
            # temp_put() performs its own per-file space check (calling
            # get_usage() again internally).  This provides a second safety
            # net in case conditions changed between our batch-planning step
            # above and the actual upload of each file.
            # ---------------------------------------------------------------
            for f, size in current_batch:
                self._debug(
                    f"batch_put: [{batch_number}] uploading '{f}' ({size:,} bytes)…"
                )
                if not self.temp_put(f):
                    # temp_put() already populated self._error.
                    return None
                self._debug(f"batch_put: [{batch_number}] '{f}' uploaded OK")

            self._debug(
                f"batch_put: batch {batch_number} complete "
                f"({len(current_batch)} file(s), {batch_total_size:,} bytes)"
            )

            # ---------------------------------------------------------------
            # If there are more files to upload, trigger an archive sync now
            # so the server can migrate the current batch from temp (cache)
            # to archive storage, reclaiming cache space for the next batch.
            #
            # We poll async_completed() until the job finishes before
            # proceeding to avoid starting the next batch while the cache is
            # still full.
            # ---------------------------------------------------------------
            if next_pending:
                self._debug(
                    f"batch_put: {len(next_pending)} file(s) deferred — "
                    f"triggering archive sync to reclaim cache space…"
                )

                job_id = self.async_synchronize()
                if job_id is None:
                    self._error = (
                        "batch_put: failed to trigger archive sync: "
                        + (self._error or "async_synchronize() returned no job ID")
                    )
                    return None

                self._debug(f"batch_put: sync job queued — ID={job_id}")

                # Poll until the server reports the job is done.
                while True:
                    status = self.async_completed(job_id)

                    if status is None:
                        # None means the "finished.<id>" file does not exist
                        # yet — the job is still running.
                        self._debug(
                            f"batch_put: sync {job_id} still running — "
                            f"waiting {poll_interval}s…"
                        )
                        time.sleep(poll_interval)
                        continue

                    # Any integer means the job completed.
                    if status != 0:
                        self._error = (
                            f"batch_put: sync job {job_id} finished with "
                            f"non-zero status {status}"
                        )
                        return None

                    self._debug(f"batch_put: sync {job_id} completed (status 0)")
                    break  # cache space should now be reclaimed; start next batch

            # Advance to the deferred files (may be empty, ending the loop).
            pending = next_pending

        self._debug(
            f"batch_put: all {len(file_entries)} file(s) uploaded "
            f"in {batch_number} batch(es)"
        )
        return True

    def temp_get(self, f: str) -> str | None:
        """Download the content of a file from temp storage.

        WARNING: the entire file is loaded into memory.  For large files use
        the streaming variant described in the original TODO (requests stream=True).

        Returns the decoded text content, or None on failure.
        """
        response = self._get("api/raw/" + uri_escape(f))
        if response is not None:
            return response.text   # decoded_content equivalent

        return None

    def temp_del(self, f: str):
        """Delete a file from temp storage.

        Returns the response object on success, None on failure.
        """
        return self._delete("api/resources/" + uri_escape(f))

    # -------------------------------------------------------------------
    # Actions on archive storage (queued until async_synchronize() is called)
    # -------------------------------------------------------------------

    def schedule_migrate(self) -> bool:
        """Schedule a migration of temp files to archive storage.

        Currently a no-op; migration is handled implicitly by the server.
        Returns True to indicate "scheduled" (mirrors Perl `return 1`).
        """
        return True

    def schedule_unmigrate(self, f: str):
        """Schedule a copy of an archive file back to temp storage.

        Uses a PATCH action query-parameter to request a server-side copy:
          PATCH /api/resources/<path>?action=copy&override=true&...

        Returns the response object on success, None on failure.
        """
        return self._patch(
            "api/resources/" + uri_escape(f)
            + "?action=copy&override=true&rename=false&destination=/"
            + uri_escape(f)
        )

    def schedule_archive_check_sums(self):
        """Check checksums for archived files.

        Not yet supported by the agOO server.
        """
        raise NotImplementedError("schedule_archive_check_sums is not yet implemented")

    def schedule_archive_del(self):
        """Delete a file from archive storage.

        Not yet implemented.
        """
        raise NotImplementedError("schedule_archive_del is not yet implemented")

    # -------------------------------------------------------------------
    # Archive synchronisation
    # -------------------------------------------------------------------

    def async_synchronize(self) -> str | None:
        """Trigger an asynchronous archive migration job.

        Mechanism
        ---------
        The agOO backend watches for the appearance of a sentinel file at
        the well-known path `_sgbdb/archUnarchAsked`.  Uploading that file
        is the signal that causes the backend to start a migration job.

        The sentinel file contains a unique ID (generated by _stamp_unique)
        so the caller can later check whether the job completed via
        async_completed(id).

        Returns the unique job ID string on success, or None on failure.

        BUGS
        ----
        - No handling for concurrent callers or GUI interactions.
        """
        result = None
        meta_file = "_sgbdb/archUnarchAsked"   # well-known sentinel path

        # Generate a unique ID to tag this particular synchronisation request.
        job_id = self._stamp_unique()

        # SECURITY: replaced Perl-style `date` subprocess with datetime.now()
        # to avoid any possibility of environment-variable-driven command
        # injection (e.g. a malformed PATH) and to remove the subprocess
        # dependency entirely.
        date_str = datetime.now().strftime("%a %b %d %H:%M:%S %Z %Y")

        # Ensure the local directory exists before writing the sentinel file.
        local_dir = self._dirname(meta_file)
        os.makedirs(local_dir, exist_ok=True)   # err. ign equivalent

        try:
            with open(meta_file, "w") as fh:
                # Write a human-readable header plus the unique ID marker.
                # The %ID:...% pattern is parsed by the backend.
                fh.write(
                    f"Automatic request queued on {date_str} by {__file__}\n"
                    f"%ID:{job_id}%\n"
                )

            # Upload the sentinel file; the server will detect it and queue the job.
            # _validate_local_path is skipped here because meta_file is a
            # hardcoded relative path that always resolves inside the CWD.
            put_result = self.temp_put(meta_file)
            if put_result is not None:
                result = job_id
            else:
                self._error = (
                    f"{job_id} error putting meta file {meta_file}: {self._error}"
                )

        except OSError as exc:
            self._error = f"{job_id} could not create meta file {meta_file}: {exc}"

        finally:
            # Always attempt local cleanup of the temporary sentinel file,
            # regardless of whether the upload succeeded (mirrors `unlink`).
            try:
                os.unlink(meta_file)
            except OSError:
                pass

            try:
                os.rmdir(local_dir)   # only removes the dir if it is now empty
            except OSError:
                pass

        return result

    def async_completed(self, job_id: str) -> int | None:
        """Poll for completion of an async archive job.

        The backend writes a result file at `_sgbdb/finished.<id>` when the
        job finishes.  Its content is expected to contain a line of the form:
            Status: <integer>

        Returns
        -------
        0      if the job completed successfully (Status: 0)
        int    the non-zero status code on failure
        255    if the finished file exists but could not be parsed
        None   if the finished file does not exist yet (job still running)
        """
        text = self.temp_get(f"_sgbdb/finished.{job_id}")

        if text is not None:
            # Search for "Status: <digits>" anywhere in the response body.
            # re module imported at top of file (was previously imported here
            # on every call, which is both slow and confusing to auditors).
            match = re.search(r"^Status: (\d+)$", text, re.MULTILINE)
            if match:
                return int(match.group(1))
            # File exists but is malformed -- return sentinel error code 255.
            return 255

        # File does not exist yet: the job is still running (or hasn't started).
        return None

    # -------------------------------------------------------------------
    # System / lifecycle
    # -------------------------------------------------------------------

    def system_stats(self):
        """Retrieve system statistics (upload/download bytes, changer ops, etc.).

        Not yet implemented.
        """
        raise NotImplementedError("system_stats is not yet implemented")

    def terminate(self):
        """Ask the agOO backend to terminate its current instance.

        A 502 response is treated as success inside _do() because the server
        may shut down before it can send a 200 reply.

        Returns the response object (or True on 502) on success, None on failure.
        """
        return self._get("api/terminate")
