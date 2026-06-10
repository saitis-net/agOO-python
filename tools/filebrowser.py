#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  Thomas Gruet <thomas.gruet@saitis.net>
"""tools/filebrowser.py — Interactive split-pane TUI for agOO storage.

Layout
------
  ┌ Local: /home/user ──────────┬ Remote: /eos ────────────────┐
  │ ..                          │ ..                           │
  │ dir/                  4 KB  │   dir/                       │
  │ [+] queued.gz     512.0 MB  │ ● online.gz        3.4 GB   │ ← bold
  │ [~] partial/              0 │ ○ archived.gz      5.6 GB   │ ← dim
  │ file.tar.gz         1.2 GB  │ ↑ recalled.gz      7.8 GB   │ ← italic
  └─────────────────────────────┴──────────────────────────────┘
   Tab:switch  ↑↓/jk:move  Space:mark  u:unmark all
   Enter:menu  r:refresh  ⌫:up  s:upload N pending  q:quit
   ✓ Ready

Local pane
----------
  [+] green    File (or fully-queued folder) — queued for upload.
  [~] magenta  Folder with some but not all files queued.
  Space        Toggle upload queue for the file under the cursor.
  Enter        On a file   → Mark / Unmark for upload.
               On a folder → Browse  or  Mark all files for upload.
  s            Upload all queued files.  Shows a progress overlay;
               press Ctrl-C to abort after the current file finishes.

Remote pane
-----------
  Bold   ✗ file is in cache, marked for eviction (unarchiveAsked=false).
  Dim    ● file is in cache, available — recalled from archive (unarchiveAsked=true).
  Dim    ○ file is in archive — available for Retrieve.
  Italic ↑ archive recall in progress.

  Enter  On a file (cache, unmarked) → Download / Mark for retrieval / Delete.
         On a file (cache, marked)   → Download / Unmark (return to archive) / Delete.
         On a file (archive) → Retrieve from archive (triggers recall).
         On a file (pending) → status info only.
         On a folder         → Browse / Download folder / Mark for retrieval.
  s      Trigger an archive sync (non-blocking, status shown in footer).

Threading model
---------------
  The main thread owns curses and drains msg_q on every loop tick.
  Worker threads (uploads, downloads, recalls, sync) communicate back
  exclusively through msg_q using the following message kinds:

    upload_progress  dict with file/done/total/bytes_done/bytes_total
    status           str — busy status-bar update (blocking op in progress)
    done             str — blocking op succeeded
    error            str — blocking op failed
    recall_done      str — background recall/download succeeded
    recall_status    str — background recall progress note
    recall_error     str — background recall/download failed
    remote_listing   (path, items|None) — result of a background remote refresh

Credentials
-----------
  Reads agOO_USER and agOO_PASSWORD from environment variables.
"""

import curses
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agoo import Agoo

_USER     = os.environ.get("agOO_USER")
_PASSWORD = os.environ.get("agOO_PASSWORD")

# Internal agOO sentinel directory — hidden from the remote pane.
_HIDDEN_NAMES = {"_sgbdb"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    """Format *n* bytes as a human-readable string (e.g. '1.2 GB', '512.0 MB')."""
    for unit, thr in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= thr:
            return f"{n / thr:.1f} {unit}"
    return f"{n} B"


def _sanitize(s: str) -> str:
    """Replace control characters in *s* with '?'.

    Prevents server-supplied strings (filenames, error messages) that contain
    ANSI escape sequences, embedded newlines, or null bytes from corrupting
    curses rendering or injecting terminal control sequences.

    str.isprintable() returns False for all C0/C1 control characters
    (U+0000–U+001F, U+007F–U+009F) as well as Unicode separators, so it
    covers the full range of characters that curses should never receive.
    """
    return "".join(c if c.isprintable() else "?" for c in s)


# ── Local pane ─────────────────────────────────────────────────────────────────

@dataclass
class _LocalEntry:
    """Immutable snapshot of one local filesystem entry used by LocalPane.

    size is always 0 for directories; reading it with stat() would be
    misleading because directory sizes are filesystem-dependent.
    """
    path: Path
    name: str
    is_dir: bool
    size: int


class LocalPane:
    """Left pane: browse the local filesystem and manage the upload queue.

    Entries are sorted directories-first, then alphabetically.  Hidden files
    (names starting with '.') are excluded.  The queue state is passed in at
    draw time so this class holds no reference to FileBrowser.
    """

    def __init__(self, win, start: Path) -> None:
        """Initialise the pane and immediately populate entries via refresh()."""
        self.win = win
        self.path = start.resolve()
        self.entries: list[_LocalEntry] = []
        self.cursor = 0
        self.scroll = 0
        self.error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        """Rebuild entries from the current directory.

        Always prepends a synthetic ``..`` entry unless the path is the
        filesystem root, so the user can navigate upward.  Entries are
        sorted directories-first, then alphabetically by name (case-insensitive).
        Hidden files (names starting with ``.``) are excluded.
        """
        self.error = None
        items: list[_LocalEntry] = []
        if self.path != self.path.parent:
            items.append(_LocalEntry(self.path.parent, "..", True, 0))
        try:
            children = sorted(
                (p for p in self.path.iterdir() if not p.name.startswith(".")),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
            for p in children:
                try:
                    size = 0 if p.is_dir() else p.stat().st_size
                except OSError:
                    size = 0
                items.append(_LocalEntry(p, p.name, p.is_dir(), size))
        except PermissionError as exc:
            self.error = str(exc)
        self.entries = items
        self._clamp()

    def _clamp(self) -> None:
        """Keep cursor inside the valid entry range after the list shrinks."""
        if self.cursor >= len(self.entries):
            self.cursor = max(0, len(self.entries) - 1)

    def _visible(self) -> int:
        """Number of entry rows that fit inside the border (height − 2 border rows)."""
        return self.win.getmaxyx()[0] - 2

    def move_up(self) -> None:
        if self.cursor > 0:
            self.cursor -= 1
            if self.cursor < self.scroll:
                self.scroll = self.cursor

    def move_down(self) -> None:
        if self.cursor < len(self.entries) - 1:
            self.cursor += 1
            vis = self._visible()
            if self.cursor >= self.scroll + vis:
                self.scroll = self.cursor - vis + 1

    def current_entry(self) -> _LocalEntry | None:
        if 0 <= self.cursor < len(self.entries):
            return self.entries[self.cursor]
        return None

    def enter_dir(self) -> None:
        e = self.current_entry()
        if e and e.is_dir:
            self.path = e.path.resolve()
            self.cursor = self.scroll = 0
            self.refresh()

    def go_up(self) -> None:
        if self.path != self.path.parent:
            self.path = self.path.parent
            self.cursor = self.scroll = 0
            self.refresh()

    def draw(self, active: bool, active_pair: int,
             pending_up: set[str], pending_pair: int,
             pending_folders: set[str] | None = None,
             partial_pair: int = 0) -> None:
        """Render the pane into its curses window.

        Folder queue markers
        --------------------
        A folder shows ``[+]`` (pending_pair colour) only if it was
        explicitly mass-marked via _mark_local_folder() AND at least one of
        its files is still in pending_up.  It shows ``[~]`` (partial_pair)
        when some but not all files are queued (e.g. after individual files
        were removed from the queue).  The ``..`` entry is never marked.
        """
        win = self.win
        h, w = win.getmaxyx()
        win.erase()
        win.border()

        title = f" Local: {self.path} "
        title_attr = curses.A_BOLD | (curses.color_pair(active_pair) if active else curses.A_NORMAL)
        try:
            win.addstr(0, 2, title[:w - 4], title_attr)
        except curses.error:
            pass

        if self.error:
            try:
                win.addstr(1, 2, f"Error: {self.error}"[:w - 4], curses.A_DIM)
            except curses.error:
                pass
            win.noutrefresh()
            return

        vis = self._visible()
        size_w = 10

        for i in range(vis):
            idx = self.scroll + i
            if idx >= len(self.entries):
                break
            e   = self.entries[idx]
            row = i + 1

            if e.is_dir and e.name != "..":
                path_prefix = str(e.path) + os.sep
                has_any = any(p.startswith(path_prefix) for p in pending_up)
                is_full = (has_any
                           and pending_folders is not None
                           and str(e.path) in pending_folders)
                queued_state = "full" if is_full else ("partial" if has_any else None)
            elif not e.is_dir:
                queued_state = "full" if str(e.path) in pending_up else None
            else:
                queued_state = None  # ".." entry — never marked

            marker = "[+] " if queued_state == "full" else (
                     "[~] " if queued_state == "partial" else "    ")
            label  = (e.name + "/") if e.is_dir else e.name
            size_s = "" if e.is_dir else _fmt_size(e.size)
            avail  = w - 2 - len(marker) - 1
            name_w = avail - size_w - 1
            line   = marker + label[:name_w].ljust(name_w) + " " + size_s.rjust(size_w)

            attr = curses.A_BOLD if e.is_dir else curses.A_NORMAL
            if queued_state == "full":
                attr |= curses.color_pair(pending_pair)
            elif queued_state == "partial" and partial_pair:
                attr |= curses.color_pair(partial_pair)
            if idx == self.cursor:
                attr |= curses.A_REVERSE
            try:
                win.addstr(row, 1, line[:w - 2], attr)
            except curses.error:
                pass

        win.noutrefresh()


# ── Remote pane ────────────────────────────────────────────────────────────────

class RemotePane:
    """Right pane: browse agOO remote storage (two-tier: cache and archive).

    Each entry carries the server-supplied fields isDir, isOffline, size, and
    unarchiveAsked.  The pane augments entries with display_name and api_path
    (stripped of the leading '/') for consistent rendering and API calls.
    """

    def __init__(self, win, client: Agoo, pending_recalls: set[str]) -> None:
        """Initialise the pane and immediately populate entries via refresh().

        pending_recalls is stored by reference so that FileBrowser additions
        are visible to draw() without an explicit synchronisation step.
        """
        self.win = win
        self.client = client
        self.pending_recalls = pending_recalls
        self.remote_path = ""
        self.entries: list[dict] = []
        self.cursor = 0
        self.scroll = 0
        self.error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        """Fetch the current directory listing from the server.

        On failure, entries is cleared so the pane shows the error message
        rather than stale data from a previous successful listing.
        """
        self.error = None
        raw = self.client.list_resources(self.remote_path)
        if raw is None:
            self.error = _sanitize(self.client.error() or "listing failed")
            self.entries = []
            return
        self._build(raw)

    def _build(self, items: list[dict]) -> None:
        """Convert raw server items into the internal entry list.

        Each entry is augmented with:
          display_name  — the bare filename (same as ``name``)
          api_path      — the server path with the leading ``/`` stripped,
                          because all Agoo API calls use relative paths.

        The internal sentinel directory ``_sgbdb`` is filtered out here so
        it never appears in the pane.  Entries are sorted directories-first,
        then alphabetically.  A synthetic ``..`` entry is prepended when not
        at the root.
        """
        result: list[dict] = []
        for item in items:
            name = item.get("name", "")
            if name in _HIDDEN_NAMES:
                continue
            api_path = item.get("path", "").lstrip("/")  # Agoo API uses relative paths
            result.append({**item, "display_name": _sanitize(name), "api_path": api_path})

        result.sort(key=lambda e: (not e.get("isDir", False),
                                   e.get("display_name", "").lower()))
        self.entries = []
        if self.remote_path:
            self.entries.append({
                "name": "..", "display_name": "..", "api_path": "",
                "isDir": True, "isOffline": False, "size": 0,
                "unarchiveAsked": False,
            })
        self.entries += result
        self._clamp()

    def _clamp(self) -> None:
        """Keep cursor inside the valid entry range after the list shrinks."""
        if self.cursor >= len(self.entries):
            self.cursor = max(0, len(self.entries) - 1)

    def _visible(self) -> int:
        """Number of entry rows that fit inside the border (height − 2 border rows)."""
        return self.win.getmaxyx()[0] - 2

    def move_up(self) -> None:
        if self.cursor > 0:
            self.cursor -= 1
            if self.cursor < self.scroll:
                self.scroll = self.cursor

    def move_down(self) -> None:
        if self.cursor < len(self.entries) - 1:
            self.cursor += 1
            vis = self._visible()
            if self.cursor >= self.scroll + vis:
                self.scroll = self.cursor - vis + 1

    def current_entry(self) -> dict | None:
        if 0 <= self.cursor < len(self.entries):
            return self.entries[self.cursor]
        return None

    def enter_dir(self) -> None:
        """Navigate into the directory under the cursor.

        For ``..``, rsplit strips the last path component; if the path
        contains no ``/``, we are one level below root so the result is
        ``""`` (root).
        """
        e = self.current_entry()
        if not e or not e.get("isDir"):
            return
        if e["display_name"] == "..":
            self.remote_path = (self.remote_path.rsplit("/", 1)[0]
                                if "/" in self.remote_path else "")
        else:
            self.remote_path = e.get("api_path", e.get("name", ""))
        self.cursor = self.scroll = 0
        self.refresh()

    def go_up(self) -> None:
        """Navigate to the parent directory; no-op when already at root (empty path)."""
        if self.remote_path:
            self.remote_path = (self.remote_path.rsplit("/", 1)[0]
                                if "/" in self.remote_path else "")
            self.cursor = self.scroll = 0
            self.refresh()

    def draw(self, active: bool, active_pair: int, italic_attr: int) -> None:
        """Render the pane into its curses window.

        Each entry is rendered with an indicator prefix that reflects the
        server-reported file state:

          ``  ``  directory (bold)
          ``✗ ``  cached, eviction-marked — isOffline=false, unarchiveAsked=false (bold)
          ``● ``  cached, available       — isOffline=false, unarchiveAsked=true  (dim)
          ``↑ ``  recall in progress      — isOffline=true,  unarchiveAsked=true  (italic)
          ``○ ``  archived                — isOffline=true,  unarchiveAsked=false (dim)
        """
        win = self.win
        h, w = win.getmaxyx()
        win.erase()
        win.border()

        path_label = ("/" + self.remote_path) if self.remote_path else "/"
        title = f" Remote: {path_label} "
        title_attr = curses.A_BOLD | (curses.color_pair(active_pair) if active else curses.A_NORMAL)
        try:
            win.addstr(0, 2, title[:w - 4], title_attr)
        except curses.error:
            pass

        if self.error:
            try:
                win.addstr(1, 2, f"Error: {self.error}"[:w - 4], curses.A_DIM)
            except curses.error:
                pass
            win.noutrefresh()
            return

        vis = self._visible()
        size_w = 10

        for i in range(vis):
            idx = self.scroll + i
            if idx >= len(self.entries):
                break
            e   = self.entries[idx]
            row = i + 1

            is_dir     = e.get("isDir", False)
            is_offline = e.get("isOffline", False)
            api_path   = e.get("api_path", "")
            display    = e.get("display_name", e.get("name", ""))
            pending   = is_offline and (e.get("unarchiveAsked", False)
                                        or api_path in self.pending_recalls)
            recalled  = (not is_offline) and e.get("unarchiveAsked", False)

            if is_dir:
                indicator = "  "
                label     = display + "/"
                attr      = curses.A_BOLD
            elif pending:
                indicator = "↑ "
                label     = display
                attr      = italic_attr
            elif recalled:
                indicator = "● "
                label     = display
                attr      = curses.A_DIM
            elif not is_offline:
                indicator = "✗ "
                label     = display
                attr      = curses.A_BOLD
            else:
                indicator = "○ "
                label     = display
                attr      = curses.A_DIM

            if idx == self.cursor:
                attr |= curses.A_REVERSE

            size_s = "" if is_dir else _fmt_size(e.get("size", 0))
            avail  = w - 2 - 2 - len(indicator) - 1   # 2 for border, 2 for left margin
            name_w = avail - size_w - 1
            line   = "  " + indicator + label[:name_w].ljust(name_w) + " " + size_s.rjust(size_w)
            try:
                win.addstr(row, 1, line[:w - 2], attr)
            except curses.error:
                pass

        win.noutrefresh()


# ── Context menu ───────────────────────────────────────────────────────────────

def show_menu(stdscr, title: str, options: list[str]) -> str | None:
    """Centred popup menu. Returns the chosen option string, or None on Escape."""
    if not options:
        return None
    h, w = stdscr.getmaxyx()
    inner_w = max(len(title), max(len(o) for o in options))
    menu_w  = min(inner_w + 6, w - 2)
    menu_h  = len(options) + 4
    y = max(0, (h - menu_h) // 2)
    x = max(0, (w - menu_w) // 2)

    win = curses.newwin(menu_h, menu_w, y, x)
    win.keypad(True)
    cursor = 0

    while True:
        win.erase()
        win.border()
        try:
            win.addstr(1, max(1, (menu_w - len(title)) // 2),
                       title[:menu_w - 4], curses.A_BOLD)
        except curses.error:
            pass
        for i, opt in enumerate(options):
            attr = curses.A_REVERSE if i == cursor else curses.A_NORMAL
            try:
                win.addstr(i + 3, 3, opt[:menu_w - 6].ljust(menu_w - 6), attr)
            except curses.error:
                pass
        win.noutrefresh()
        curses.doupdate()

        key = win.getch()
        if key in (curses.KEY_UP, ord('k')) and cursor > 0:
            cursor -= 1
        elif key in (curses.KEY_DOWN, ord('j')) and cursor < len(options) - 1:
            cursor += 1
        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            del win
            return options[cursor]
        elif key == 27:
            del win
            return None

    return None   # unreachable


# ── Main application ───────────────────────────────────────────────────────────

class FileBrowser:
    """Main TUI application: event loop, pane management, and all operations.

    Owns the curses screen and two child panes (LocalPane, RemotePane).
    All background work runs in daemon threads; threads communicate back via
    msg_q.  The event loop drains msg_q every tick and dispatches results to
    the UI.  See the module docstring for the full msg_q protocol.

    Upload state machine
    --------------------
    Idle:      busy=False, _upload_progress=None, _upload_abort=None
    Uploading: busy=True,  _upload_progress=dict, _upload_abort=Event
    Done/err:  busy=False, _upload_progress=None, _upload_abort=None
    """

    def __init__(self, stdscr, client: Agoo) -> None:
        """Initialise colour pairs, state, and the two pane windows.

        Colour pair assignments (referenced by number throughout the class):
          1  cyan    — active pane title border
          2  yellow  — status bar
          3  green   — fully-queued local items ([+])
          4  magenta — partially-queued local folders ([~])

        A_ITALIC falls back to A_UNDERLINE on terminals that do not support it
        (used for the ↑ recall-in-progress indicator in the remote pane).
        """
        self.stdscr = stdscr
        self.client = client
        self.pending_recalls: set[str] = set()         # api_paths with active recall
        self.pending_uploads: set[str] = set()         # absolute local paths queued for upload
        self.pending_upload_folders: set[str] = set()  # folders mass-marked via Enter menu
        self.status = "Ready"
        self.busy   = False                            # True while upload/download blocks UI
        self.msg_q: queue.Queue = queue.Queue()
        self._upload_abort: threading.Event | None = None
        self._upload_progress: dict | None = None
        self._spinner_frame: int = 0

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN,    -1)  # active pane title
        curses.init_pair(2, curses.COLOR_YELLOW,  -1)  # status bar
        curses.init_pair(3, curses.COLOR_GREEN,   -1)  # fully-queued items
        curses.init_pair(4, curses.COLOR_MAGENTA, -1)  # partially-queued folders

        self._italic = getattr(curses, "A_ITALIC", curses.A_UNDERLINE)

        self._init_layout()

    def _init_layout(self) -> None:
        """Create or recreate pane windows to fit the current terminal size.

        Called once at startup and again on KEY_RESIZE.  Preserves the
        current directory and remote path across resizes so navigation state
        is not lost when the terminal is resized mid-session.
        """
        h, w = self.stdscr.getmaxyx()
        pane_h  = h - 2
        left_w  = w // 2
        right_w = w - left_w

        left_win  = curses.newwin(pane_h, left_w,  0, 0)
        right_win = curses.newwin(pane_h, right_w, 0, left_w)
        left_win.keypad(True)
        right_win.keypad(True)

        prev_local  = getattr(self, '_local_pane',  None)
        prev_remote = getattr(self, '_remote_pane', None)

        self._local_pane  = LocalPane(left_win,
                                      prev_local.path if prev_local else Path.home())
        self._remote_pane = RemotePane(right_win, self.client, self.pending_recalls)

        if prev_remote and prev_remote.remote_path:
            self._remote_pane.remote_path = prev_remote.remote_path
            self._remote_pane.refresh()

        self.active = getattr(self, 'active', 0)

    # ── Drawing ────────────────────────────────────────────────────────────────

    def _draw_hints(self) -> None:
        """Render the key-binding hint bar at the second-to-last row.

        The 's' hint changes depending on which pane is active: upload queue
        summary on the local pane, archive sync on the remote pane.
        """
        h, w = self.stdscr.getmaxyx()
        n = len(self.pending_uploads)
        if self.active == 0:
            s_hint = f"s:upload ({n} pending)" if n else "s:upload pending"
        else:
            s_hint = "s:sync archive"
        hints = (f" Tab:switch  ↑↓/jk:move  Space:mark  u:unmark all  "
                 f"Enter:menu  r:refresh  ⌫:up  {s_hint}  q:quit")
        try:
            self.stdscr.addstr(h - 2, 0, hints[:w - 1].ljust(w - 1), curses.A_REVERSE)
        except curses.error:
            pass

    def _draw_status(self) -> None:
        """Render the status bar at the last row.

        Shows ⟳ while a blocking operation is in progress (busy=True)
        and ✓ when idle.
        """
        h, w = self.stdscr.getmaxyx()
        icon = " ⟳ " if self.busy else " ✓ "
        line = (icon + self.status)[:w - 1].ljust(w - 1)
        try:
            self.stdscr.addstr(h - 1, 0, line, curses.color_pair(2))
        except curses.error:
            pass
        self.stdscr.noutrefresh()

    def _draw_all(self) -> None:
        """Full repaint: hints bar → status bar → both panes → overlay (if active).

        The upload overlay is drawn last so it paints on top of both pane
        windows in the curses virtual screen.
        """
        self.stdscr.erase()
        self._draw_hints()
        self._draw_status()
        # Snapshot both sets before passing to draw() so that the worker
        # thread calling pending_uploads.difference_update() cannot mutate
        # them mid-iteration inside LocalPane.draw().
        self._local_pane.draw(self.active == 0, 1,
                              frozenset(self.pending_uploads), 3,
                              frozenset(self.pending_upload_folders), 4)
        self._remote_pane.draw(self.active == 1, 1, self._italic)
        if self._upload_progress is not None:
            self._draw_upload_overlay()
        curses.doupdate()

    def _set_status(self, msg: str, busy: bool = False) -> None:
        """Update status text and busy flag, then immediately repaint the status bar.

        Calls doupdate() directly rather than waiting for the next _draw_all()
        so the user sees the update as soon as the operation starts.
        """
        self.status = msg
        self.busy   = busy
        self._draw_status()
        curses.doupdate()

    def _draw_upload_overlay(self) -> None:
        """Render a centred progress popup on top of the browser panes.

        Called both from _draw_all() (full redraws) and directly from the
        event loop (spinner ticks and progress updates) to avoid repainting
        the entire screen on every 50 ms tick.

        The progress bar is a weighted average: 50% file-count advancement
        plus 50% volume advancement, giving equal weight to both dimensions.
        """
        p = self._upload_progress
        if p is None:
            return
        h, w = self.stdscr.getmaxyx()
        ov_w = min(64, max(40, w - 4))
        ov_h = 8
        y = max(0, (h - ov_h) // 2)
        x = max(0, (w - ov_w) // 2)

        win = curses.newwin(ov_h, ov_w, y, x)
        win.border()

        _SPINNER = r"|/-\\"
        spin  = _SPINNER[self._spinner_frame % 4]
        done  = p["done"]
        total = p["total"]
        title = f" {spin} Uploading {done} / {total} "
        try:
            win.addstr(0, max(1, (ov_w - len(title)) // 2),
                       title[:ov_w - 2], curses.A_BOLD)
        except curses.error:
            pass

        # Current filename (row 2)
        filename = os.path.basename(p.get("file", ""))
        try:
            win.addstr(2, 2, filename[:ov_w - 4])
        except curses.error:
            pass

        # Progress bar: 50% file-count + 50% volume (row 3)
        bytes_done  = p.get("bytes_done", 0)
        bytes_total = p.get("bytes_total", 0) or 1
        files_frac  = done / total if total > 0 else 0.0
        bytes_frac  = bytes_done / bytes_total
        pct         = 0.5 * files_frac + 0.5 * bytes_frac
        bar_w       = ov_w - 10
        filled      = int(bar_w * pct)
        bar         = "█" * filled + "░" * (bar_w - filled)
        try:
            win.addstr(3, 2, f"{bar}  {pct * 100:.0f}%"[:ov_w - 4])
        except curses.error:
            pass

        # Raw metrics (row 4)
        files_line = f"{done}/{total} files  ·  {_fmt_size(bytes_done)} / {_fmt_size(bytes_total)}"
        try:
            win.addstr(4, 2, files_line[:ov_w - 4], curses.A_DIM)
        except curses.error:
            pass

        # Abort hint (row 6)
        try:
            win.addstr(6, 2, "^C to abort", curses.A_DIM)
        except curses.error:
            pass

        win.noutrefresh()

    def _prune_upload_folders(self) -> None:
        """Remove fully-uploaded folders from pending_upload_folders."""
        self.pending_upload_folders = {
            d for d in self.pending_upload_folders
            if any(p.startswith(d + os.sep) for p in self.pending_uploads)
        }

    def _spawn_remote_refresh(self) -> None:
        """Fetch the remote listing in a background thread (non-blocking).

        Captures the current remote_path at dispatch time and posts
        ("remote_listing", (path, items)) on msg_q when done.  The event
        loop applies the result only if the pane has not navigated away
        since the request was issued.
        """
        path = self._remote_pane.remote_path

        def _work() -> None:
            items = self.client.list_resources(path)
            self.msg_q.put(("remote_listing", (path, items)))

        threading.Thread(target=_work, daemon=True).start()

    # ── Event loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Enter the curses event loop.  Returns when the user quits.

        Loop structure per iteration
        ----------------------------
        1. Drain msg_q (worker-thread results).
        2. If any message changed visible state, refresh both panes.
        3. Non-blocking getch().
           - No key (-1): advance spinner if uploading, then napms(50).
           - Key while busy: only Ctrl-C (abort) or 'q' (quit) are honoured.
           - Key while idle: full key dispatch.
        """
        self.stdscr.nodelay(True)
        self._draw_all()

        while True:
            # Drain the worker-thread message queue.
            changed = False
            while not self.msg_q.empty():
                kind, data = self.msg_q.get_nowait()
                if kind == "upload_progress":
                    self._upload_progress = data
                    self._draw_upload_overlay()
                    curses.doupdate()
                elif kind == "status":
                    # Blocking operation progress update.
                    self._set_status(data, busy=True)
                elif kind == "done":
                    # Blocking operation finished successfully.
                    self._upload_progress = None
                    self._upload_abort    = None
                    self.busy = False
                    self._set_status(data)
                    self._prune_upload_folders()
                    changed = True
                elif kind == "error":
                    # Blocking operation failed.
                    self._upload_progress = None
                    self._upload_abort    = None
                    self.busy = False
                    self._set_status(f"Error: {data}")
                    changed = True
                elif kind == "recall_done":
                    # Background recall+download finished (non-blocking).
                    if not self.busy:
                        self._set_status(data)
                    changed = True
                elif kind == "recall_status":
                    # Background recall progress (non-blocking).
                    if not self.busy:
                        self._set_status(data)
                elif kind == "recall_error":
                    # Background recall failed (non-blocking).
                    if not self.busy:
                        self._set_status(f"Recall error: {data}")
                elif kind == "remote_listing":
                    # Result of a background remote pane refresh.
                    r_path, items = data
                    if r_path == self._remote_pane.remote_path:
                        if items is None:
                            self._remote_pane.error = (
                                self.client.error() or "listing failed")
                            self._remote_pane.entries = []
                            self._remote_pane._clamp()
                        else:
                            self._remote_pane.error = None
                            self._remote_pane._build(items)
                        self._draw_all()

            if changed:
                # Refresh local pane immediately (fast, no network I/O).
                # Remote pane refresh is dispatched to a background thread
                # so the UI is never blocked waiting for the server.
                self._local_pane.refresh()
                self._spawn_remote_refresh()
                self._draw_all()

            key = self.stdscr.getch()
            if key == -1:
                if self.busy and self._upload_progress is not None:
                    # Advance spinner on every idle tick while an upload is running.
                    self._spinner_frame += 1
                    self._draw_upload_overlay()
                    curses.doupdate()
                curses.napms(50)
                continue

            # While a blocking operation runs: Ctrl-C aborts upload; q quits.
            if self.busy:
                if key == 3:  # Ctrl-C
                    if self._upload_abort is not None:
                        self._upload_abort.set()
                        self._set_status(
                            "Aborting… waiting for current file to finish", busy=True)
                elif key in (ord('q'), ord('Q')):
                    p = self._upload_progress
                    if p:
                        title = f"Uploading {p['done']}/{p['total']} files — abort and quit?"
                    else:
                        title = "Operation in progress — abort and quit?"
                    choice = show_menu(self.stdscr, title,
                                       ["Abort and quit", "Cancel"])
                    self._draw_all()
                    if choice == "Abort and quit":
                        if self._upload_abort is not None:
                            self._upload_abort.set()
                        break
                continue

            if key in (ord('q'), ord('Q')):
                if self._confirm_quit():
                    break
            elif key == curses.KEY_RESIZE:
                self._init_layout()
                self._draw_all()
            elif key == ord('\t'):
                self.active = 1 - self.active
                self._draw_all()
            elif key in (curses.KEY_UP, ord('k')):
                self._active_pane().move_up()
                self._draw_all()
            elif key in (curses.KEY_DOWN, ord('j')):
                self._active_pane().move_down()
                self._draw_all()
            elif key == ord(' '):
                self._handle_space()
            elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
                self._handle_enter()
            elif key in (curses.KEY_BACKSPACE, 127):
                self._active_pane().go_up()
                self._draw_all()
            elif key in (ord('r'), curses.KEY_F5):
                self._active_pane().refresh()
                self._draw_all()
            elif key == ord('u'):
                if self.active == 0:
                    n = len(self.pending_uploads)
                    self.pending_uploads.clear()
                    self._set_status(f"Cleared {n} pending upload(s).")
                else:
                    n = len(self.pending_recalls)
                    self.pending_recalls.clear()
                    self._set_status(f"Cleared {n} pending recall(s).")
                self._draw_all()
            elif key == ord('s'):
                if self.active == 0:
                    self._trigger_upload()
                else:
                    self._trigger_sync()

    def _active_pane(self):
        """Return the pane currently in focus (active=0 → local, active=1 → remote)."""
        return self._local_pane if self.active == 0 else self._remote_pane

    # ── Space key — toggle upload queue ───────────────────────────────────────

    def _handle_space(self) -> None:
        """Toggle the upload queue for the file under the cursor.

        Only applies to the local pane.  Directories are silently ignored
        because only individual files can be queued via Space; use Enter →
        "Mark folder for upload" to queue an entire directory tree.
        """
        if self.active != 0:
            return
        e = self._local_pane.current_entry()
        if not e or e.is_dir:
            return
        p = str(e.path)
        if p in self.pending_uploads:
            self.pending_uploads.discard(p)
            self._set_status(f"Unmarked: {e.name}  ({len(self.pending_uploads)} pending)")
        else:
            self.pending_uploads.add(p)
            self._set_status(
                f"Queued: {e.name}  ({len(self.pending_uploads)} pending — press 's' to upload)"
            )
        self._draw_all()

    # ── Enter key ──────────────────────────────────────────────────────────────

    def _handle_enter(self) -> None:
        """Dispatch Enter to the active pane handler.

        The two handlers are separate because the menus and underlying
        operations differ completely between local and remote contexts.
        """
        if self.active == 0:
            self._handle_local_enter()
        else:
            self._handle_remote_enter()

    def _handle_local_enter(self) -> None:
        """Handle Enter on the local pane.

        Three cases:
          ``..``      → navigate up directly (no menu).
          directory   → Browse / Mark folder for upload / Cancel menu.
          file        → Mark for upload / Unmark / Cancel menu.
        """
        pane = self._local_pane
        e    = pane.current_entry()
        if e is None:
            return

        if e.is_dir:
            # '..' navigates directly without a menu.
            if e.name == "..":
                pane.go_up()
                self._draw_all()
                return

            choice = show_menu(self.stdscr, f"{e.name}/",
                               ["Browse", "Mark folder for upload", "Cancel"])
            self._draw_all()
            if choice == "Browse":
                pane.enter_dir()
                self._draw_all()
            elif choice == "Mark folder for upload":
                self._mark_local_folder(e.path)
            return

        # File — mark / unmark for deferred upload.
        p       = str(e.path)
        queued  = p in self.pending_uploads
        label   = "Unmark" if queued else "Mark for upload"
        choice  = show_menu(self.stdscr, e.name, [label, "Cancel"])
        self._draw_all()
        if choice == label:
            if queued:
                self.pending_uploads.discard(p)
                self._set_status(f"Unmarked: {e.name}  ({len(self.pending_uploads)} pending)")
            else:
                self.pending_uploads.add(p)
                self._set_status(
                    f"Queued: {e.name}  ({len(self.pending_uploads)} pending — press 's' to upload)"
                )
            self._draw_all()

    def _handle_remote_enter(self) -> None:
        """Handle Enter on the remote pane.

        For directories: Browse / Download folder / Mark for retrieval menu.
        For files, the menu depends on the file's state:
          ● cached, unarchiveAsked=false  → Download / Mark for retrieval / Delete
          ✗ cached, unarchiveAsked=true   → Download / Unmark (return to archive) / Delete
          ↑ offline + recall in progress  → info popup only
          ○ archived (offline)            → Retrieve from archive
        """
        pane = self._remote_pane
        e    = pane.current_entry()
        if e is None:
            return

        if e.get("isDir"):
            # '..' navigates directly without a menu.
            if e.get("display_name") == "..":
                pane.go_up()
                self._draw_all()
                return

            folder_name = e.get("display_name", "")
            folder_path = e.get("api_path", e.get("name", ""))
            choice = show_menu(self.stdscr, f"{folder_name}/",
                               ["Browse",
                                "Download folder",
                                "Mark folder for retrieval",
                                "Cancel"])
            self._draw_all()
            if choice == "Browse":
                pane.enter_dir()
                self._draw_all()
            elif choice == "Download folder":
                self._download_folder(folder_path, folder_name,
                                      self._local_pane.path)
            elif choice == "Mark folder for retrieval":
                self._mark_remote_folder_for_retrieval(folder_path, folder_name)
            return

        api_path   = e.get("api_path", e.get("path", "").lstrip("/"))
        display    = e.get("display_name", e.get("name", ""))
        is_offline = e.get("isOffline", False)
        # pending is only meaningful for offline files; an online file with
        # unarchiveAsked=True has already been recalled and is downloadable.
        pending    = is_offline and (e.get("unarchiveAsked", False)
                                     or api_path in self.pending_recalls)
        local_dest = self._safe_local_dest(display)
        if local_dest is None:
            self._set_status(f"Blocked: '{display}' path escapes local directory.")
            self._draw_all()
            return

        if not is_offline:
            # File is in cache.  If unarchiveAsked is True the file was
            # explicitly recalled; offer to cancel that mark.  Otherwise
            # offer to archive it ("Mark for retrieval") so it can be
            # recalled later via schedule_unmigrate + async_synchronize.
            unarchive_asked = e.get("unarchiveAsked", False)
            archive_label = ("Unmark (return to archive)" if unarchive_asked
                             else "Mark for retrieval")
            choice = show_menu(self.stdscr, f"● {display}",
                               ["Download to local",
                                archive_label,
                                "Delete from remote",
                                "Cancel"])
            self._draw_all()
            if choice == "Download to local":
                self._do_download(api_path, display, local_dest)
            elif choice == "Unmark (return to archive)":
                self._do_unmark(api_path, display)
            elif choice == "Mark for retrieval":
                self._do_mark(api_path, display)
            elif choice == "Delete from remote":
                self._do_delete(api_path, display)
        elif pending:
            # File is offline and a recall is already in progress — info only.
            show_menu(self.stdscr, f"↑ {display}  (recall in progress)", ["OK"])
            self._draw_all()
        else:
            # File is in archive — Retrieve only.
            choice = show_menu(self.stdscr, f"○ {display}  (archived)",
                               ["Retrieve from archive", "Cancel"])
            self._draw_all()
            if choice == "Retrieve from archive":
                self._do_recall(api_path, display)

    # ── Local folder marking ───────────────────────────────────────────────────

    def _collect_local_files(self, path: Path,
                              _visited: set[str] | None = None) -> list[str]:
        """Recursively collect all non-hidden files under path.

        Symlinks to files are included normally.  Symlinks to directories are
        followed unless doing so would create a traversal loop, detected by
        tracking the resolved real path of every directory entered.
        """
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

    def _mark_local_folder(self, path: Path) -> None:
        """Queue every non-hidden file under *path* and record it as fully-marked.

        Recording in pending_upload_folders lets LocalPane.draw() distinguish
        fully-marked folders ([+]) from folders where only some files are
        queued ([~]) after individual files have been unqueued.
        """
        files = self._collect_local_files(path)
        self.pending_uploads.update(files)
        self.pending_upload_folders.add(str(path))
        self._set_status(
            f"Queued {len(files)} file(s) from {path.name}/  "
            f"({len(self.pending_uploads)} total pending — press 's' to upload)"
        )
        self._draw_all()

    # ── Remote folder retrieval ────────────────────────────────────────────────

    def _mark_remote_folder_for_retrieval(self, folder_api_path: str,
                                          folder_display: str) -> None:
        """Trigger archive recall for every offline file in a remote folder.

        Does NOT download — files are marked ↑ (italic) until they return
        online.  Use 'Download folder' to download cached files.
        """
        def _work() -> None:
            items = self.client.list_resources(folder_api_path)
            if items is None:
                self.msg_q.put(("recall_error",
                                f"Cannot list {folder_display}/: {self.client.error()}"))
                return

            offline = [e for e in items
                       if not e.get("isDir")
                       and e.get("isOffline")
                       and not e.get("unarchiveAsked")]

            if not offline:
                self.msg_q.put(("recall_status",
                                f"No offline files to retrieve in {folder_display}/."))
                return

            triggered = 0
            for item in offline:
                api_path = item.get("path", "").lstrip("/")
                self.pending_recalls.add(api_path)
                if self.client.schedule_unmigrate(api_path) is not None:
                    triggered += 1
                else:
                    self.pending_recalls.discard(api_path)

            self.msg_q.put(("recall_status",
                            f"Recall triggered for {triggered}/{len(offline)} "
                            f"file(s) in {folder_display}/  (↑ italic when pending)"))

        threading.Thread(target=_work, daemon=True).start()
        self._set_status(f"Requesting recall for contents of {folder_display}/…")

    def _download_folder(self, folder_api_path: str, folder_display: str,
                         local_base: Path) -> None:
        """Spawn a non-blocking download thread for every cached file in a folder."""
        def _work() -> None:
            items = self.client.list_resources(folder_api_path)
            if items is None:
                self.msg_q.put(("recall_error",
                                f"Cannot list {folder_display}/: {self.client.error()}"))
                return

            online = [e for e in items
                      if not e.get("isDir") and not e.get("isOffline")]

            if not online:
                self.msg_q.put(("recall_status",
                                f"No cached files to download in {folder_display}/."))
                return

            self.msg_q.put(("recall_status",
                            f"Spawning {len(online)} download(s) from {folder_display}/…"))
            for item in online:
                api_path   = item.get("path", "").lstrip("/")
                name       = item.get("name", api_path)
                local_dest = (local_base / name).resolve()
                # Guard against server-supplied names that contain ../
                try:
                    local_dest.relative_to(local_base.resolve())
                except ValueError:
                    self.msg_q.put(("recall_error",
                                    f"Blocked: '{name}' path escapes local directory."))
                    continue
                threading.Thread(
                    target=self._download_single,
                    args=(api_path, name, str(local_dest)),
                    daemon=True,
                ).start()

        threading.Thread(target=_work, daemon=True).start()
        self._set_status(f"Listing {folder_display}/ for download…")

    def _download_single(self, api_path: str, display: str,
                         local_dest: str) -> None:
        """Download one cached file without blocking the UI.  Runs in a thread."""
        ok = self.client.temp_get_file(api_path, local_dest)
        if ok:
            self.msg_q.put(("recall_done", f"Downloaded → {local_dest}"))
        else:
            self.msg_q.put(("recall_error",
                            f"Download failed for {display}: {self.client.error()}"))

    # ── Path helpers ───────────────────────────────────────────────────────────

    def _safe_local_dest(self, filename: str) -> str | None:
        """Return the resolved local path for *filename* only if it stays inside
        the current local-pane directory.

        A remote server could theoretically return a display name containing
        ``../`` components.  Resolving against the current directory and then
        checking the prefix prevents writes outside the intended location.
        Returns None (and does not download) if the path would escape.
        """
        base = self._local_pane.path.resolve()
        dest = (base / filename).resolve()
        try:
            dest.relative_to(base)
            return str(dest)
        except ValueError:
            return None

    # ── Operations ─────────────────────────────────────────────────────────────

    def _do_unmark(self, api_path: str, display: str) -> None:
        """Return a cached file to archive via schedule_migrate."""
        ok = self.client.schedule_migrate(api_path)
        if ok:
            self._set_status(f"Unmarked: {display}  (returned to archive).")
            self._remote_pane.refresh()
            self._draw_all()
        else:
            self._set_status(f"Unmark failed: {self.client.error()}")

    def _do_mark(self, api_path: str, display: str) -> None:
        """Mark a cached file for retrieval via schedule_unmigrate (sets unarchiveAsked)."""
        ok = self.client.schedule_unmigrate(api_path)
        if ok:
            self._set_status(f"Marked: {display}  (marked for retrieval).")
            self._remote_pane.refresh()
            self._draw_all()
        else:
            self._set_status(f"Mark failed: {self.client.error()}")

    def _do_delete(self, api_path: str, display: str) -> None:
        """Permanently delete a file from remote storage (cache + archive)."""
        result = self.client.temp_del(api_path)
        if result is not None:
            self._set_status(f"Deleted {display}.")
            self._remote_pane.refresh()
            self._draw_all()
        else:
            self._set_status(f"Delete failed: {self.client.error()}")

    def _confirm_quit(self) -> bool:
        """Return True if the application should exit now.

        Quits immediately when nothing is pending.  When there are unsent
        uploads or unfinished recalls the user is shown a confirmation menu
        so they can cancel, trigger a sync, and then quit cleanly.

        The busy-path emergency quit (q during an active upload) bypasses
        this check intentionally — the user already has Ctrl-C for abort.
        """
        parts = []
        n_up = len(self.pending_uploads)
        n_rc = len(self.pending_recalls)
        if n_up:
            parts.append(f"{n_up} upload(s) pending")
        if n_rc:
            parts.append(f"{n_rc} recall(s) pending")
        if not parts:
            return True
        choice = show_menu(self.stdscr, "  ·  ".join(parts),
                           ["Quit anyway", "Cancel"])
        self._draw_all()
        return choice == "Quit anyway"

    def _trigger_sync(self) -> None:
        """Trigger an agOO archive sync from the remote pane ('s' key).

        Runs in a background daemon thread so the UI stays responsive.
        The job is polled every 30 seconds (hardcoded; sync jobs are slow
        by nature — archiving to tape may take minutes).  Progress is
        reported via recall_status/recall_done/recall_error messages on
        msg_q, which the event loop displays in the status bar.
        """
        def _work() -> None:
            self.msg_q.put(("recall_status", "Triggering archive sync…"))
            job_id = self.client.async_synchronize()
            if job_id is None:
                self.msg_q.put(("recall_error",
                                f"Sync failed: {self.client.error()}"))
                return
            short = job_id[:12]
            self.msg_q.put(("recall_status", f"Sync job {short}… started"))
            while True:
                status = self.client.async_completed(job_id)
                if status is None:
                    self.msg_q.put(("recall_status",
                                    f"Sync job {short}… running"))
                    time.sleep(30)
                    continue
                if status == 0:
                    self.msg_q.put(("recall_done",
                                    f"Archive sync completed (job {short})."))
                else:
                    self.msg_q.put(("recall_error",
                                    f"Sync job {short} finished with status {status}"))
                break

        threading.Thread(target=_work, daemon=True).start()
        self._set_status("Archive sync triggered — running in background…")

    def _trigger_upload(self) -> None:
        """Upload all queued files when 's' is pressed."""
        if not self.pending_uploads:
            self._set_status("No files queued for upload.  "
                             "Use Space or Enter on a file to queue it.")
            return
        self._do_upload(sorted(self.pending_uploads))

    def _do_upload(self, paths: list[str]) -> None:
        """Start a batched upload of *paths* and show the progress overlay.

        Sets busy=True for the duration; the overlay is dismissed automatically
        when the worker thread sends "done" or "error".  Ctrl-C sets the abort
        event and stops the loop after the current file finishes.
        """
        label = f"{len(paths)} file(s)"
        abort = threading.Event()
        self._upload_abort    = abort
        self._upload_progress = {
            "file": paths[0] if paths else "",
            "done": 0, "total": len(paths),
            "bytes_done": 0, "bytes_total": 0,
        }
        self._set_status(f"Uploading {label}…", busy=True)
        self._draw_all()   # show overlay immediately before first file starts

        # Tracks files confirmed uploaded by the progress callback so that
        # an aborted upload can remove only the already-uploaded files from
        # pending_uploads rather than leaving them in the queue.
        uploaded: set[str] = set()

        def _on_progress(f: str, done: int, total: int,
                         bytes_done: int, bytes_total: int) -> None:
            uploaded.add(f)
            self.msg_q.put(("upload_progress", {
                "file": f, "done": done, "total": total,
                "bytes_done": bytes_done, "bytes_total": bytes_total,
            }))

        def _work() -> None:
            ok = self.client.batch_put(paths, progress=_on_progress,
                                       abort_event=abort)
            if ok:
                self.pending_uploads.difference_update(paths)
                self.msg_q.put(("done", f"Uploaded {label} successfully."))
            elif abort.is_set():
                # Remove only the files that were confirmed uploaded before
                # the abort so re-pressing 's' retries the remainder only.
                self.pending_uploads.difference_update(uploaded)
                self.msg_q.put(("error", "Upload aborted."))
            else:
                self.msg_q.put(("error", self.client.error() or "upload failed"))

        threading.Thread(target=_work, daemon=True).start()

    def _do_download(self, api_path: str, display: str, local_dest: str) -> None:
        """Blocking download from cache — locks the UI until complete."""
        self._set_status(f"Downloading {display}…", busy=True)

        def _work() -> None:
            self.msg_q.put(("status", f"Downloading {display}…"))
            ok = self.client.temp_get_file(api_path, local_dest)
            if ok:
                self.msg_q.put(("done", f"Downloaded → {local_dest}"))
            else:
                self.msg_q.put(("error", self.client.error() or "download failed"))

        threading.Thread(target=_work, daemon=True).start()

    def _do_recall(self, api_path: str, display: str) -> None:
        """Request an archive recall.  Marks the file ↑ pending; does NOT download."""
        self.pending_recalls.add(api_path)
        self._draw_all()

        def _work() -> None:
            if self.client.schedule_unmigrate(api_path) is not None:
                self.msg_q.put(("recall_status",
                                f"Recall requested: {display}  "
                                f"(refresh pane when ↑ clears, then download)"))
            else:
                self.pending_recalls.discard(api_path)
                self.msg_q.put(("recall_error",
                                f"Recall failed for {display}: {self.client.error()}"))

        threading.Thread(target=_work, daemon=True).start()
        self._set_status(f"Recall requested: {display}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    """Entry point: authenticate and launch the curses TUI.

    curses.wrapper() is used so the terminal is restored to a sane state
    even if FileBrowser.run() raises an exception.  The ``finally`` block
    calls logout() to discard the session token regardless of how the
    application exits.
    """
    missing = [n for n, v in (("agOO_USER", _USER), ("agOO_PASSWORD", _PASSWORD)) if not v]
    if missing:
        for n in missing:
            print(f"Error: ${n} is not set.", file=sys.stderr)
        print("Export agOO_USER and agOO_PASSWORD before running.", file=sys.stderr)
        sys.exit(1)

    client = Agoo(user=_USER, login="admin", password=_PASSWORD)
    print("Authenticating…", end="", flush=True)
    if not client.login():
        print(f"\nLogin failed: {client.error()}", file=sys.stderr)
        sys.exit(1)
    print(" OK")

    try:
        def _curses_main(stdscr):
            curses.curs_set(0)
            stdscr.keypad(True)
            FileBrowser(stdscr, client).run()

        curses.wrapper(_curses_main)
    finally:
        client.logout()


if __name__ == "__main__":
    main()
