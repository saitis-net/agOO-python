================
agOO-client-TUI
================

----------------------------------------------
interactive TUI file browser for agOO storage
----------------------------------------------

:Manual section: 1
:Manual group: agOO user commands

SYNOPSIS
========

**agOO-client-TUI**

DESCRIPTION
===========

**agOO-client-TUI** is a keyboard-driven, split-pane terminal file browser
for transferring files between the local filesystem and an agOO remote storage
server.  The left pane shows the local filesystem; the right pane shows the
agOO remote store with its two tiers (fast online *cache* and tape-backed
*archive*).

Network operations (uploads, downloads, recalls, directory listings) run in
background threads so the interface remains responsive at all times.  A live
progress overlay appears during uploads.

ENVIRONMENT
===========

Both variables are **required**.  The program exits with an error if either is
unset.

``agOO_USER``
    Per-user namespace embedded in every API URL path.

``agOO_PASSWORD``
    Password used to authenticate the session.

KEY BINDINGS
============

These bindings are available in either pane unless noted:

``Tab``
    Switch focus between the local and remote pane.

``Up`` / ``k``
    Move cursor up.

``Down`` / ``j``
    Move cursor down.

``Enter``
    Open the context menu for the item under the cursor.

``Space``
    *(local pane)* Toggle the file under the cursor in the upload queue.

``Backspace``
    Go up one directory level.

``r`` / ``F5``
    Refresh the active pane.

``u``
    *(local)* Clear the entire upload queue.
    *(remote)* Cancel all pending archive recalls.

``s``
    *(local)* Upload all queued files.
    *(remote)* Trigger an archive synchronisation job.

``q``
    Quit.  If uploads or recalls are pending a confirmation menu is shown.
    Pressing ``q`` during an active upload also shows a confirmation menu
    before aborting.

``Ctrl+C``
    *(during an upload)* Abort after the current file finishes.  Files already
    uploaded are removed from the queue so a subsequent ``s`` retries only what
    remains.

LOCAL PANE
==========

Files marked for upload are shown in green with a ``[+]`` prefix.  Partially-
marked directories show a magenta ``[~]`` prefix.  Press ``s`` to start
uploading everything in the queue; the library batches uploads automatically
and triggers archive synchronisation cycles when the combined size exceeds
the available cache space.

REMOTE PANE
===========

File state indicators
---------------------

``✗`` (bold)
    Cached, marked for eviction — in cache, will return to archive on the
    next synchronisation.

``●`` (dim)
    Cached, available — recalled from archive and ready to download.

``↑`` (italic)
    Recall in progress — an archive recall has been requested.

``○`` (dim)
    Archived — on tape; must be retrieved before it can be downloaded.

*(no indicator)*
    A directory.

Context menus
-------------

Pressing ``Enter`` on a cached file (``✗`` or ``●``) opens a menu with:
**Download to local**, **Mark for retrieval** / **Unmark (return to archive)**,
and **Delete from remote**.

Pressing ``Enter`` on an archived file (``○``) offers:
**Retrieve from archive**.  The file shows ``↑`` while the recall is queued.
Press ``s`` to run the synchronisation job that performs the recall, then
refresh with ``r`` once complete.

Pressing ``Enter`` on a remote directory offers:
**Browse**, **Download folder**, and **Mark folder for retrieval**.

NOTES
=====

- A terminal at least 100 columns wide is recommended.
- The agOO server communicates exclusively over HTTPS; all API requests are
  made with certificate verification enabled.
- Archive recall is asynchronous.  After requesting a recall, press ``s`` to
  trigger a synchronisation job, wait for the file to change to ``●``, then
  download it.

FILES
=====

``~/.agOO-client-TUI.history``
    Not yet implemented; reserved for future use.

EXIT STATUS
===========

0
    Normal exit (user pressed ``q`` and confirmed).

1
    Startup error (missing credentials, login failure, terminal too small).

SEE ALSO
========

**agoo**\(3)

AUTHOR
======

Thomas Gruet
