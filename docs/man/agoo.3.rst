====
agoo
====

----------------------------------------------
Python client library for the agOO storage API
----------------------------------------------

:Manual section: 3
:Manual group: agOO library functions

SYNOPSIS
========

.. code-block:: python

    from agoo import Agoo

    client = Agoo(user="myuser", password="s3cr3t")
    client.login()

    client.temp_put("data/report.tar.gz")
    client.temp_get_file("data/report.tar.gz", "/local/report.tar.gz")
    client.logout()

DESCRIPTION
===========

The **agoo** Python package provides a client for the agOO remote
file-storage and archive service.  agOO exposes a REST API protected by a
session token and uses the TUS resumable-upload protocol for large file
transfers.

All methods return ``None`` on failure.  The reason for the failure is
available via ``client.error()``.

CONSTRUCTOR
===========

``Agoo(user, password, login="admin", base_url=..., start_url=..., debug=False, io_size=10485760)``
    Create a client instance.

    ``user``
        Per-user namespace embedded in every URL path (required).  Also read
        from the ``agOO_USER`` environment variable if not supplied.

    ``password``
        Password for authentication (required).  Also read from the
        ``agOO_PASSWORD`` environment variable if not supplied.

    ``login``
        Username sent in the login form (default: ``admin``).

    ``base_url``
        Root URL of the agOO installation
        (default: ``https://agoo.saitis.net``).  Must use ``https://``.

    ``start_url``
        URL used to wake a stopped instance
        (default: ``https://agoo.saitis.net/cgi-bin/start-fb.cgi``).
        Must use ``https://``.

    ``debug``
        When ``True``, print diagnostic output to stdout (default: ``False``).

    ``io_size``
        Upload chunk size in bytes (default: 10 485 760 — 10 MiB).

    Raises ``ValueError`` if credentials are missing, if ``base_url`` or
    ``start_url`` do not use HTTPS, or if a path argument contains unsafe
    traversal sequences.

AUTHENTICATION METHODS
======================

``login() → bool``
    Authenticate and store the session token.  Must be called before any
    other operation.  Returns ``True`` on success.

``logout()``
    Terminate the backend instance and clear the local session token.
    Also called automatically by ``__del__``.

STORAGE METHODS
===============

``get_usage() → dict | None``
    Return cache and archive usage statistics.  The dict contains at least
    ``total`` and ``used`` keys (bytes).

``stat(path) → dict | None``
    Return metadata for *path*.  Returns ``None`` for files currently in
    archive (offline) storage.  Raises ``RuntimeError`` if called before
    ``login()``.

``temp_put(path, override=False) → bool | None``
    Upload the local file at *path* to temp storage using the TUS resumable-
    upload protocol.  If the file already exists on the server it is silently
    skipped unless *override* is ``True``.  Returns ``True`` on success or
    skip; ``None`` on error.

``batch_put(files, poll_interval=30, override=False, progress=None) → bool | None``
    Upload a list of local files, automatically batching uploads when the
    combined size exceeds the available cache space.  For each batch the
    method uploads as many files as fit, then calls ``async_synchronize()``
    and polls until the archive migration finishes before uploading the
    remainder.

    *poll_interval* — seconds between completion polls during a sync cycle.

    *progress* — optional callback ``progress(path, done, total, bytes_done,
    bytes_total)`` called after each file completes.

``temp_put_fake(path)``
    Create an empty placeholder file on the server.

``temp_get(path, max_bytes=10485760) → str | None``
    Download a small file into memory.  Enforces a hard cap of *max_bytes*
    (default 10 MiB) to prevent memory exhaustion.  Use ``temp_get_file()``
    for large or binary files.

``temp_get_file(path, local_path, chunk_size=8388608) → bool | None``
    Stream the remote file at *path* to *local_path* in chunks of
    *chunk_size* bytes (default 8 MiB).  Peak memory usage is bounded by
    the chunk size regardless of file size.

``temp_del(path)``
    Delete the file at *path* from temp storage.

``temp_sum(path, hash_type) → str | None``
    Return a checksum for the remote file.  *hash_type* is a string such as
    ``"md5"`` or ``"sha256"``.

ARCHIVE METHODS
===============

``schedule_unmigrate(path)``
    Request that the archived file at *path* be recalled to temp storage.
    The recall is asynchronous; call ``async_synchronize()`` to start the
    migration job and poll ``async_completed()`` for completion.

``schedule_migrate() → bool``
    No-op; migration from temp to archive is handled implicitly by the
    server.

ASYNC SYNC METHODS
==================

``async_synchronize() → str | None``
    Trigger an archive synchronisation job.  Returns a job-ID string that
    can be passed to ``async_completed()``.

``async_completed(job_id) → int | None``
    Poll for the completion of *job_id*.  Returns a status code (integer)
    when the job has finished, or ``None`` if it is still running.

SYSTEM METHODS
==============

``terminate()``
    Ask the backend to shut down its current instance.

``error() → str | None``
    Return the last error message, or ``None`` if no error has occurred.

ERROR HANDLING
==============

All methods return ``None`` on failure.  The reason is available via
``client.error()``:

.. code-block:: python

    result = client.temp_put("myfile.dat")
    if result is None:
        print(f"Upload failed: {client.error()}")

The following exceptions are raised for programming errors:

``ValueError``
    Bad constructor arguments — missing credentials, non-HTTPS URL, or
    an unsafe path traversal sequence in a path argument.

``RuntimeError``
    Calling ``stat()`` before ``login()``.

``NotImplementedError``
    Calling an unimplemented method (``system_stats``,
    ``schedule_archive_check_sums``, ``schedule_archive_del``).

SECURITY
========

- HTTPS is enforced; ``http://`` URLs are rejected at construction time.
- TLS certificate verification is enabled and cannot be overridden by
  environment variables.
- All HTTP requests carry a 10 s connect / 60 s read timeout.
- Non-streaming API responses are buffered with a hard 1 MiB limit.
- The ``X-Auth`` session token is stripped before following a redirect to
  a different origin.
- The session rejects all server-set cookies; authentication is exclusively
  via ``X-Auth``.
- HTTP redirects are limited to 5 per request.

EXAMPLE
=======

.. code-block:: python

    import os, time
    from agoo import Agoo

    client = Agoo(
        user=os.environ["agOO_USER"],
        password=os.environ["agOO_PASSWORD"],
    )

    if not client.login():
        raise SystemExit(f"Login failed: {client.error()}")

    # Upload a file; skip if it already exists.
    if not client.temp_put("backup.tar.gz"):
        raise SystemExit(f"Upload failed: {client.error()}")

    # Trigger an archive sync and wait.
    job_id = client.async_synchronize()
    while True:
        status = client.async_completed(job_id)
        if status is not None:
            print(f"Sync done, status {status}")
            break
        time.sleep(30)

    client.logout()

SEE ALSO
========

**agOO-client-TUI**\(1)

AUTHOR
======

Thomas Gruet
