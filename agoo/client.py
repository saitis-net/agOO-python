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
from urllib.parse import quote as uri_escape, urlparse    # equivalent of URI::Escape::uri_escape

import requests                                 # equivalent of LWP::UserAgent

# Connect timeout (s) and read timeout (s) applied to every HTTP request.
# Without a timeout, a stalled server hangs the client indefinitely.
# (connect, read) — the two-tuple form mirrors requests' recommendation.
_DEFAULT_TIMEOUT = (10, 60)

# Maximum number of HTTP redirects the session will follow per request.
# Unlimited redirects could be exploited to leak the X-Auth token to a
# third-party domain controlled by the server operator.
_MAX_REDIRECTS = 5

# Fraction of total cache capacity reserved as a safety margin.
# Effective usable space = total * (1 - _CACHE_SAFETY_MARGIN) - used.
# Keeping 10% headroom avoids filling the cache completely, which could
# interfere with server-side housekeeping that also needs cache space.
_CACHE_SAFETY_MARGIN = 0.10


class _SafeSession(requests.Session):
    """requests.Session that strips X-Auth on cross-origin redirects and
    disables cookie accumulation.

    requests strips the Authorization header on cross-origin redirects but
    leaves custom headers — including X-Auth — intact.  This subclass
    overrides rebuild_auth() to also drop X-Auth whenever the redirect
    target has a different host than the original request, preventing the
    session token from being forwarded to a third-party domain.

    The agOO protocol authenticates exclusively via X-Auth; cookie storage
    is disabled to prevent server-set cookies from interfering with that
    model or being silently replayed on future requests (F-10).
    """

    def __init__(self) -> None:
        super().__init__()
        self.cookies = requests.cookies.RequestsCookieJar()  # reject all server cookies

    def rebuild_auth(self, prepared_request, response) -> None:
        super().rebuild_auth(prepared_request, response)
        if urlparse(response.url).netloc != urlparse(prepared_request.url).netloc:
            prepared_request.headers.pop("X-Auth", None)


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
        self._session = _SafeSession()
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
        - Relative path traversal: a relative path must resolve within the CWD.
          Absolute paths are permitted — the caller explicitly chose the location.
        """
        if "\x00" in path:
            raise ValueError(f"path contains a null byte: {path!r}")

        if not os.path.isabs(path):
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
    def _validate_output_path(path: str) -> None:
        """Raise ValueError if `path` is unsafe as a local download destination.

        Unlike _validate_local_path() — which rejects all paths that resolve
        outside the CWD — this validator permits absolute paths so callers can
        legitimately write to external mounts (e.g. /mnt/nas/backup.tar.gz).

        Checks applied
        --------------
        - Null bytes: always rejected.
        - Relative path traversal: a relative path must resolve within the
          CWD (i.e. relative paths with '..' that escape the CWD are rejected).
          Absolute paths bypass this check since the caller explicitly chose them.
        """
        if "\x00" in path:
            raise ValueError(f"output path contains a null byte: {path!r}")

        if not os.path.isabs(path):
            cwd = Path.cwd().resolve()
            resolved = (cwd / path).resolve()
            try:
                resolved.relative_to(cwd)
            except ValueError:
                raise ValueError(
                    f"output path escapes the working directory: "
                    f"{path!r} resolves to {resolved}"
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

    def _get_stream(self, path: str, **headers):
        """Like _get() but instructs requests not to buffer the response body.

        Returns the response object with stream=True so the caller can
        consume the body incrementally via response.iter_content().  The
        caller is responsible for calling response.close() when done so
        the underlying TCP connection is returned to the pool.

        Used by temp_get() (for the memory-capped small-file path) and
        temp_get_file() (for the disk-streaming large-file path).
        """
        return self._do("GET", path, stream=True, **headers)

    def _post(self, path: str, body=None, **headers):
        return self._do("POST", path, body=body, **headers)

    def _patch(self, path: str, body=None, **headers):
        return self._do("PATCH", path, body=body, **headers)

    def _delete(self, path: str, **headers):
        return self._do("DELETE", path, **headers)

    def _tus_get_offset(self, tus_path: str) -> int | None:
        """Query the server for the current TUS upload offset via HEAD.

        Returns the number of bytes the server has already received for this
        upload slot, or None if the slot does not exist or the request fails.

        Used by temp_put() to resume an interrupted upload without restarting
        from byte zero.
        """
        response = self._do("HEAD", tus_path, **{"Tus-Resumable": "1.0.0"})
        if response is None:
            return None
        offset_str = response.headers.get("Upload-Offset")
        if offset_str is None:
            return None
        try:
            return int(offset_str)
        except ValueError:
            self._debug(f"_tus_get_offset: unexpected Upload-Offset value: {offset_str!r}")
            return None

    def _do(self, method: str, path: str, body=None,
            stream: bool = False, **extra_headers):
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
        stream        : if True, the response body is NOT downloaded immediately;
                        the caller must iterate response.iter_content() and call
                        response.close() when done.  Default False.
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
                #
                # stream=True tells requests not to download the body
                # Always use stream=True so that non-streaming callers can have
                # their response body capped before it is buffered into memory.
                # Streaming callers (stream=True) receive the raw response and
                # must call iter_content() / response.close() themselves.
                response = self._session.request(
                    method,
                    url,
                    data=body,              # raw bytes payload (used by POST/PATCH)
                    headers=headers,
                    timeout=_DEFAULT_TIMEOUT,
                    stream=True,
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
                if not stream:
                    # Buffer the body with a hard size cap to prevent OOM from a
                    # malicious or misconfigured server (F-05).
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

    def logout(self) -> None:
        """Clear the local session token.

        Discards the local auth token so the client object becomes inert.
        Authenticated calls will raise RuntimeError until login() is called again.
        The backend instance is left running so that other clients (e.g. the
        web UI) are not affected.
        """
        self._auth_token = None

    def __del__(self) -> None:
        self._auth_token = None

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

    def list_resources(self, path: str = "") -> list[dict] | None:
        """List the contents of a remote directory.

        Calls GET api/resources/<path> (or api/resources/ for the root) and
        returns the `items` array from the response.  Each element is a dict
        that includes at minimum:

          name           : entry basename (e.g. "vEOS-4.31.1F.swi")
          path           : absolute remote path (e.g. "/eos/vEOS-4.31.1F.swi")
          size           : file size in bytes
          isDir          : True for directory entries
          isOffline      : True when the file is in archive storage
          unarchiveAsked : True when an archive recall has been requested

        The _get_stream() variant is used so that the 1 MiB non-streaming
        response cap in _do() does not truncate large directory listings.

        Returns a list of dicts on success, None on failure.
        """
        endpoint = ("api/resources/" + uri_escape(path)) if path else "api/resources/"
        response = self._get_stream(endpoint)
        if response is None:
            return None
        try:
            data = json.loads(response.text)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("items", [])
            return []
        except json.JSONDecodeError as exc:
            self._error = f"list_resources: server returned invalid JSON: {exc}"
            return None
        finally:
            response.close()

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

    # Maximum number of consecutive PATCH failures before temp_put() gives up.
    # Each failure triggers a TUS HEAD to query the server's current offset and
    # resume from there, so this counts resume attempts, not raw retries.
    _TUS_MAX_RESUME = 5

    def temp_put(self, f: str, override: bool = False) -> bool | None:
        """Upload a local file to temp storage using the TUS resumable protocol.

        The file is read and sent in chunks of io_size bytes so that large
        files can be uploaded without consuming excessive memory.

        TUS upload sequence
        -------------------
        1. HEAD  /api/tus/<path>          Check whether a partial upload already
                                          exists.  If the server returns an
                                          Upload-Offset header the upload resumes
                                          from that byte; otherwise a new slot is
                                          created with POST.
        2. POST  /api/tus/<path>?override=<bool>  with Upload-Length: <total bytes>
                                          Registers the upload slot on the server
                                          (only when no existing slot was found).
                                          When override=True the server replaces
                                          any existing copy (in temp or archive).
        3. PATCH /api/tus/<path>          Sends file data starting from the
                                          current offset.  On connection failure
                                          the server is queried again via HEAD and
                                          the upload resumes from its reported
                                          offset, up to _TUS_MAX_RESUME times.

        Parameters
        ----------
        f        : local file path (must reside within the current working directory)
        override : if True, overwrite any existing copy of the file on the server
                   (in temp or archive).  If False (default), an existing file is
                   skipped and True is returned without re-uploading.

        Returns True on success or skip, None on failure.

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

        # Apply the safety margin: only count space up to (1 - margin) of total.
        # This reserves _CACHE_SAFETY_MARGIN of total capacity for server-side
        # housekeeping and prevents the cache from being filled to the brim.
        usable    = int(total * (1.0 - _CACHE_SAFETY_MARGIN))
        available = max(0, usable - used)

        if file_size > available:
            self._error = (
                f"insufficient cache space for '{f}': "
                f"file is {file_size:,} bytes but only {available:,} bytes available "
                f"({used:,} used of {total:,} total, "
                f"{_CACHE_SAFETY_MARGIN * 100:.0f}% safety margin reserved)"
            )
            return None

        self._debug(
            f"space check OK: file={file_size:,} B  "
            f"available={available:,} B  used={used:,}/{total:,} B  "
            f"margin={_CACHE_SAFETY_MARGIN * 100:.0f}%"
        )

        tus_path = "api/tus/" + uri_escape(f)

        # --- Step 1: Check for an existing partial upload (TUS HEAD) ---
        # If a previous attempt was interrupted the server may already hold
        # some bytes.  Resume from the server's reported offset instead of
        # restarting from zero and re-sending data the server already has.
        offset = self._tus_get_offset(tus_path)

        if offset is None:
            # No in-progress TUS slot — register a new upload.
            # override=true tells the server to replace any existing copy of
            # the file (in temp or archive); override=false leaves it intact
            # and returns 409 if a copy already exists.
            override_str = "true" if override else "false"
            response = self._post(
                tus_path + f"?override={override_str}",
                **{"Upload-Length": str(file_size)},
            )
            if response is None:
                # 409 Conflict: the file already exists on the server and
                # override=false was used.  The TUS slot was cleaned up on
                # the previous completed upload, so the HEAD above found
                # nothing, but the file itself is still present.
                #
                # Two sub-cases:
                #   (a) File is in temp  → stat() returns its metadata; skip
                #       if sizes match, fail if they differ (different content).
                #   (b) File is in archive → stat() returns 404 (the temp API
                #       does not surface archived files); treat as "already
                #       present" and skip rather than failing with a misleading
                #       error.
                if self._error and "409" in self._error:
                    existing = self.stat(f)
                    if existing is not None:
                        # File is in temp; check the size so we don't silently
                        # skip a file whose content has changed.
                        if existing.get("size") == file_size:
                            self._debug(
                                f"temp_put: '{f}' already in temp "
                                f"({file_size:,} bytes), skipping"
                            )
                            return True
                        # Size mismatch: content has changed but the caller did
                        # not pass override=True.  Report clearly.
                        self._error = (
                            f"temp_put: '{f}' already exists on server with a "
                            f"different size ({existing.get('size'):,} B on server "
                            f"vs {file_size:,} B locally); use override=True to replace"
                        )
                        return None
                    else:
                        # stat returned None → file is in archive (offline).
                        # Skip silently; the caller can use override=True to
                        # force a re-upload over the archived copy.
                        self._debug(
                            f"temp_put: '{f}' already in archive, skipping "
                            f"(use override=True to replace)"
                        )
                        return True
                self._error = f"could not create remote file '{f}': {self._error}"
                return None
            offset = 0
        elif offset >= file_size:
            # The server already has all bytes (e.g. a previous run completed
            # but the caller didn't record the result).
            self._debug(f"temp_put: '{f}' already fully uploaded ({offset:,} bytes)")
            return True
        else:
            self._debug(
                f"temp_put: resuming '{f}' from server offset "
                f"{offset:,} / {file_size:,} bytes"
            )

        # --- Step 2: Send file content in chunks, resuming on failure ---
        resume_attempts = 0
        try:
            with open(f, "rb") as fh:
                fh.seek(offset)
                while offset < file_size:
                    chunk = fh.read(self._config["io_size"])
                    if not chunk:
                        break   # EOF — upload complete

                    response = self._patch(
                        tus_path,
                        body=chunk,
                        **{
                            "Content-Type":  "application/offset+octet-stream",
                            "Tus-Resumable": "1.0.0",
                            "Upload-Offset": str(offset),
                        },
                    )

                    if response is None:
                        # PATCH failed (connection drop, timeout, etc.).
                        # Query the server for how many bytes it actually kept,
                        # then resume from there rather than restarting at zero.
                        if resume_attempts >= self._TUS_MAX_RESUME:
                            self._error = (
                                f"could not send data to remote file '{f}': "
                                f"gave up after {self._TUS_MAX_RESUME} resume attempts"
                            )
                            return None

                        resume_attempts += 1
                        server_offset = self._tus_get_offset(tus_path)
                        if server_offset is not None and server_offset >= offset:
                            self._debug(
                                f"temp_put: PATCH failed — resuming '{f}' from "
                                f"server offset {server_offset:,} "
                                f"(resume {resume_attempts}/{self._TUS_MAX_RESUME})"
                            )
                            offset = server_offset
                            fh.seek(offset)
                        else:
                            # Server has no record of the upload (slot lost).
                            self._error = (
                                f"could not send data to remote file '{f}': "
                                f"{self._error} — server offset lost"
                            )
                            return None
                        continue

                    offset += len(chunk)
                    resume_attempts = 0   # reset counter after each successful chunk

        except OSError as exc:
            self._error = f"read error from local file '{f}': {exc}"
            return None

        return True

    def batch_put(self, files: list[str], poll_interval: int = 30,
                  override: bool = False,
                  progress=None,
                  abort_event=None) -> bool | None:
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
             Usable space = total * (1 - _CACHE_SAFETY_MARGIN) - used, so a
             10% safety margin is always kept free for server housekeeping.
          2. Files are considered in order.  A file is added to the current
             batch as long as the running total of that batch would not exceed
             the usable space.  Files that would overflow are deferred.
          3. Every file in the batch is uploaded with temp_put().
          4. async_synchronize() is always called after every batch — even the
             last one — so files are migrated from cache to archive and the
             cache is emptied.  async_completed() is polled every poll_interval
             seconds until the job finishes before proceeding.
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
        override      : passed through to temp_put() for each file; if True,
                        existing copies on the server are replaced rather than
                        skipped (default False).
        progress      : optional callable invoked after each successful upload.
                        Signature: progress(path, files_done, files_total,
                                            bytes_done, bytes_total).
                        Called from the same thread as batch_put(); must be
                        non-blocking.
        abort_event   : optional threading.Event; when set, the upload loop
                        stops cleanly after the current file and returns None
                        with self.error() set to "upload aborted by user".

        Returns
        -------
        True  — every file was uploaded successfully (or skipped when override=False).
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
            # Compare against the safety-margin-adjusted capacity (90% of total,
            # i.e. 1 - _CACHE_SAFETY_MARGIN) rather than the raw total, because
            # that is the maximum space any single upload can ever use.
            if size > total_cache * (1.0 - _CACHE_SAFETY_MARGIN):
                self._error = (
                    f"batch_put: '{f}' ({size:,} bytes) exceeds the effective cache "
                    f"capacity ({int(total_cache * (1.0 - _CACHE_SAFETY_MARGIN)):,} bytes "
                    f"= {total_cache:,} total × {1.0 - _CACHE_SAFETY_MARGIN:.0%}) "
                    f"and can never be uploaded"
                )
                return None

        # ------------------------------------------------------------------
        # Phase 3 — greedy batching loop.
        #
        # `pending` holds the files not yet uploaded, in original order.
        # Each iteration of the while-loop processes one batch.
        # ------------------------------------------------------------------
        total_files = len(file_entries)
        total_bytes = sum(size for _, size in file_entries)
        files_done  = 0
        bytes_done  = 0

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

            used  = usage.get("used", 0)
            total = usage.get("total", 0)

            # Honour the safety margin: cap usable space at (1 - margin) of
            # total so the cache is never filled to the brim.
            usable    = int(total * (1.0 - _CACHE_SAFETY_MARGIN))
            available = max(0, usable - used)

            self._debug(
                f"batch_put: cache usable={usable:,} B  "
                f"available={available:,} B  "
                f"used={used:,}/{total:,} B  "
                f"margin={_CACHE_SAFETY_MARGIN * 100:.0f}%  "
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
                if abort_event is not None and abort_event.is_set():
                    self._error = "upload aborted by user"
                    return None
                self._debug(
                    f"batch_put: [{batch_number}] uploading '{f}' ({size:,} bytes)…"
                )
                if not self.temp_put(f, override=override):
                    # temp_put() already populated self._error.
                    return None
                self._debug(f"batch_put: [{batch_number}] '{f}' uploaded OK")
                files_done += 1
                bytes_done += size
                if progress is not None:
                    progress(f, files_done, total_files, bytes_done, total_bytes)
                if abort_event is not None and abort_event.is_set():
                    self._error = "upload aborted by user"
                    return None
                # Refresh usage after each upload so that space consumed by
                # other concurrent clients is reflected before the next file.
                usage = self.get_usage()
                if usage is not None:
                    used      = usage.get("used", 0)
                    total     = usage.get("total", 0)
                    usable    = int(total * (1.0 - _CACHE_SAFETY_MARGIN))
                    available = max(0, usable - used)

            self._debug(
                f"batch_put: batch {batch_number} complete "
                f"({len(current_batch)} file(s), {batch_total_size:,} bytes)"
            )

            # ---------------------------------------------------------------
            # Sync after every batch — not just when files remain.
            #
            # Triggering async_synchronize() unconditionally ensures that
            # every batch is migrated from temp (cache) to archive storage
            # before we return, keeping the cache consistently empty.
            # On the last batch this archives the final files; on earlier
            # batches it reclaims space so the next batch can proceed.
            #
            # We block on async_completed() so the caller knows the data is
            # safely in archive, not just sitting in temp, when we return.
            # ---------------------------------------------------------------
            if next_pending:
                sync_reason = (
                    f"{len(next_pending)} file(s) still pending — "
                    f"reclaiming cache space"
                )
            else:
                sync_reason = "final batch — archiving uploaded files"

            self._debug(f"batch_put: triggering archive sync ({sync_reason})…")

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
                break  # cache space reclaimed; proceed to next batch or finish

            # Advance to the deferred files (empty on the last batch).
            pending = next_pending

        self._debug(
            f"batch_put: all {len(file_entries)} file(s) uploaded "
            f"in {batch_number} batch(es)"
        )
        return True

    def temp_get(self, f: str,
                 max_bytes: int = 10 * 1024 * 1024) -> str | None:
        """Download a small file from temp storage and return its text content.

        This method is designed for small files — status blobs, metadata,
        short text documents.  It enforces a hard memory cap (max_bytes) and
        will refuse to return content that exceeds it.

        For large or binary files use temp_get_file() instead, which streams
        the response body directly to disk without any memory limit.

        How the memory cap is enforced
        -------------------------------
        The download uses HTTP streaming (stream=True) so that the response
        body is consumed in small chunks rather than downloaded all at once:

          1. If the server sends a Content-Length header that already exceeds
             max_bytes, the connection is closed immediately — no body bytes
             are read at all.
          2. Otherwise, chunks are accumulated as they arrive.  If the running
             total exceeds max_bytes mid-stream the connection is closed and an
             error is returned.

        This two-stage approach handles both honest servers (that advertise
        size up-front) and chunked/streaming responses (that do not).

        Parameters
        ----------
        f         : remote path of the file to download.
        max_bytes : maximum bytes to load into memory (default 10 MiB).
                    Raise this for legitimate small-file use cases; switch to
                    temp_get_file() for anything that might be large.

        Returns the decoded UTF-8 text on success, None on failure.
        """
        response = self._get_stream("api/raw/" + uri_escape(f))
        if response is None:
            return None

        # ------------------------------------------------------------------
        # Stage 1: fast rejection via Content-Length header.
        #
        # If the server declares the size up-front we can close the
        # connection immediately without reading a single body byte.
        # ------------------------------------------------------------------
        content_length_header = response.headers.get("Content-Length")
        if content_length_header is not None:
            try:
                declared_size = int(content_length_header)
                if declared_size > max_bytes:
                    response.close()   # release connection back to the pool
                    self._error = (
                        f"temp_get: '{f}' is {declared_size:,} bytes, which exceeds "
                        f"the {max_bytes:,}-byte in-memory limit. "
                        f"Use temp_get_file() to download large files to disk."
                    )
                    return None
            except ValueError:
                pass   # malformed header; fall through to the streaming read

        # ------------------------------------------------------------------
        # Stage 2: chunk-by-chunk read with a running total check.
        #
        # 65 536 bytes per chunk is a common network buffer size that
        # balances per-iteration overhead against granularity of the check.
        # ------------------------------------------------------------------
        chunks: list[bytes] = []
        total_read = 0

        try:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue   # keep-alive / empty delimiter chunk

                total_read += len(chunk)
                if total_read > max_bytes:
                    # Close before returning so the server-side connection is
                    # not left open waiting for an unread response body.
                    response.close()
                    self._error = (
                        f"temp_get: '{f}' exceeded the {max_bytes:,}-byte in-memory "
                        f"limit after receiving {total_read:,} bytes. "
                        f"Use temp_get_file() to download large files to disk."
                    )
                    return None

                chunks.append(chunk)
        finally:
            response.close()   # always return the connection to the pool

        raw = b"".join(chunks)

        # Decode as UTF-8 using surrogateescape so that arbitrary byte
        # sequences do not raise — this mirrors the original response.text
        # behaviour used before streaming was introduced.
        return raw.decode("utf-8", errors="surrogateescape")

    def temp_get_file(self, f: str, local_path: str,
                      chunk_size: int = 8 * 1024 * 1024) -> bool | None:
        """Download a file from temp storage, streaming directly to disk.

        Unlike temp_get(), this method never loads the response body into
        memory.  The body arrives from the network in chunks and each chunk
        is written to disk before the next one is requested, so memory usage
        stays bounded by chunk_size regardless of how large the file is.

        This is the correct method for:
          - Files whose size is unknown or potentially large.
          - Binary files (archives, images, databases, …).
          - Any context where memory exhaustion is a concern.

        Use temp_get() only for small, known-size text blobs (e.g. status
        files, short metadata documents).

        Parameters
        ----------
        f          : remote path of the file to download.
        local_path : local filesystem path to write the downloaded data to.
                     The file is created if it does not exist and truncated
                     if it does (i.e. existing content is overwritten).
        chunk_size : number of bytes requested from the network per iteration
                     (default 8 MiB).  The actual chunk may be smaller if the
                     server delivers data in smaller pieces.  Tune upward for
                     fast local storage, downward for constrained memory.

        Returns True on success, None on failure (self.error() set).
        """
        # SECURITY: validate the output path before any network I/O so that a
        # caller that derives local_path from untrusted input cannot be tricked
        # into writing to an arbitrary location (e.g. ../../.ssh/authorized_keys).
        try:
            self._validate_output_path(local_path)
        except ValueError as exc:
            self._error = str(exc)
            return None

        response = self._get_stream("api/raw/" + uri_escape(f))
        if response is None:
            return None

        bytes_written = 0
        try:
            # Open in binary mode — the response body is raw bytes.
            # We write exactly what the server sends, preserving binary
            # content faithfully without any encoding/decoding step.
            with open(local_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue   # skip keep-alive delimiter chunks

                    fh.write(chunk)
                    bytes_written += len(chunk)

                    # Log progress so callers with debug=True can watch large
                    # downloads proceed without silence.
                    self._debug(
                        f"temp_get_file: '{f}' — {bytes_written:,} bytes written…"
                    )

        except OSError as exc:
            # Disk-write failure (permission denied, no space left, etc.).
            self._error = f"temp_get_file: error writing '{local_path}': {exc}"
            return None
        finally:
            # Always close the response so the TCP connection is returned to
            # the pool, even if an exception is raised mid-download.
            response.close()

        self._debug(
            f"temp_get_file: '{f}' → '{local_path}' "
            f"({bytes_written:,} bytes, chunk_size={chunk_size:,})"
        )
        return True

    def temp_del(self, f: str):
        """Delete a file from temp storage.

        Returns the response object on success, None on failure.
        """
        return self._delete("api/resources/" + uri_escape(f))

    # -------------------------------------------------------------------
    # Actions on archive storage (queued until async_synchronize() is called)
    # -------------------------------------------------------------------

    def schedule_migrate(self, f: str):
        """Move a cached file back to archive storage (evict from cache).

        Uses the same PATCH endpoint as schedule_unmigrate() but with
        rename=true, which tells the server to move rather than copy:
          PATCH /api/resources/<path>?action=copy&override=true&rename=true&...

        After this call the file will appear as isOffline=true in
        list_resources() and can be recalled with schedule_unmigrate().

        Returns the response object on success, None on failure.
        """
        return self._patch(
            "api/resources/" + uri_escape(f)
            + "?action=copy&override=true&rename=true&destination=/"
            + uri_escape(f)
        )

    def schedule_unmigrate(self, f: str):
        """Mark an archived file for recall back to temp (cache) storage.

        Uses a PATCH action query-parameter to request a server-side copy:
          PATCH /api/resources/<path>?action=copy&override=true&rename=false&...

        rename=false means copy (archive copy is preserved); after a
        successful call the file will show unarchiveAsked=true in
        list_resources() but will remain isOffline=true until the recall
        job actually runs.

        IMPORTANT: this call only schedules the recall.  You must call
        async_synchronize() afterwards to trigger the server-side job that
        physically moves the data.  Poll async_completed() to wait for
        completion; the file becomes isOffline=false when done (typically
        within a minute for small files).

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
