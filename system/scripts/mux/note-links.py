#!/usr/bin/env python3
"""Clickable vault-note links for terminals that open only http(s) links (wmux).

wmux turns only http(s) URLs into clickable links and opens them in its
browser panel (Settings, Browser, Links), so the file:// and obsidian:// links
in CoS reports did nothing there. review_surface.note_link_mode() picks
``http`` on wmux and renders each note link as

    http://127.0.0.1:7472/n/<id>

a 10-character id of the vault-relative path (review_surface.note_link_id), short
enough that Claude Code never wraps it. The older long form
http://127.0.0.1:7472/open?file=<vault-relative path>.md still works.

This small server answers that link: it opens the note in the Obsidian app
(obsidian://open?vault=switchboard&file=...) and, on Windows, brings Obsidian's window
to the front, because a hidden server's launch otherwise leaves it behind wmux
. The panel shows a small "Opened in Obsidian" page with the
note's text one click away, or the whole note when Obsidian did not open.
/view?file= shows the note without opening Obsidian.

Safety: binds 127.0.0.1 only, GET only, serves only .md files inside the vault
(no path traversal, no worktrees), sends no CORS headers, and never writes.

Compare pages: ``/c/<id>`` serves an image_compare.py compare page (an .html
in a ``compare`` folder, review_surface.compare_rel_ok) with its local images
inlined, because a wmux pane opens only http(s) links and the page's file://
link did nothing. The id is review_surface.compare_link_id of
the page's vault-relative path; review_surface.compare_for_link_id finds main's
copy, or the live worktree copy main's .state/compare-links.json names. Only
image files are inlined, the page gets the /page CSP (no fetches, no frames),
and like a note it answers only a direct visit, never a cross-site request.
The CSP adds ``sandbox allow-scripts``, so the page runs at an opaque origin and
its script cannot read other bridge pages (notes, /view). Only images inside
main or the page's own worktree, outside the sensitive folders, are inlined
(review_surface.compare_image_ok), up to COMPARE_MAX_BYTES per page.

Local pages: ``/page/<name>`` serves one self-contained HTML file named in the
gitignored allowlist ``local/note-links-pages.json`` ({"<name>": "<vault-relative
.html path>"}), so a link in a Google Sheet can open a private local app on this
machine without uploading it. Nothing outside that list is served as HTML. A page
may be opened by a top-level click from another site (Sec-Fetch-Mode navigate,
Dest document), never framed or fetched cross-site, and COOP cuts the opener.
Its CSP blocks fetches and every subresource (connect, img, font, script src are
'none' or data:) but not navigation, and 'unsafe-inline' runs any script that
gets into the page, so an allowlisted page must never write URL text (query,
hash, name) into markup. All pages share one origin and its localStorage; link
them as http://127.0.0.1:<port>/page/<name>, because localhost is another origin.

Usage:
    python3 system/scripts/mux/note-links.py serve  [--port P]   run in the foreground
    python3 system/scripts/mux/note-links.py ensure [--port P]   start detached unless the port answers
    python3 system/scripts/mux/note-links.py status [--port P]   exit 0 when the server answers

Environment: SWITCHBOARD_NOTE_LINK_PORT (default 7472), SWITCHBOARD_VAULT_ROOT, SWITCHBOARD_VAULT_NAME.
Windows starts it at sign-in as the \\switchboard\\com.switchboard.note-links scheduled task.
"""
from __future__ import annotations

import argparse
import html
import http.client
import os
import re
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

HOST = "127.0.0.1"
DEFAULT_PORT = 7472
# What /health reports in X-Note-Links-Protocol. 2 adds /n/<id>;
# 3 brings the Obsidian window to the front on Windows;
# 4 never sends a key and cancels the raise on any key press after the click
#; 5 serves /c/<id> compare pages;
# a server that sends no header is protocol 1. ensure replaces an older server,
# and review_surface renders the long /open?file= form while one still answers.
PROTOCOL = "5"
PROTOCOL_HEADER = "X-Note-Links-Protocol"


def port() -> int:
    try:
        return int(os.environ.get("SWITCHBOARD_NOTE_LINK_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


_VAULT_ROOT: Path | None = None


def vault_root() -> Path:
    """Resolved once per process: a detached server must not spawn git per request."""
    global _VAULT_ROOT
    if _VAULT_ROOT is None:
        import review_surface

        _VAULT_ROOT = review_surface.vault_root()
    return _VAULT_ROOT


def vault_name() -> str:
    import review_surface

    return review_surface.vault_name()


def resolve_note(rel: str) -> Path | None:
    """The vault note for a vault-relative path, or None when it is not one."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or rel.startswith(".") or "/." in rel:
        return None
    if not rel.endswith((".md", ".markdown")):
        rel += ".md"
    root = vault_root()
    path = (root / rel).resolve()
    try:
        inside = path.relative_to(root)
    except ValueError:
        return None
    if inside.parts[:2] in _BLOCKED_PREFIXES or not path.is_file():
        return None
    return path


def note_for_id(link_id: str) -> str | None:
    """Vault-relative note path for a /n/<id> link (review_surface.note_for_link_id)."""
    import review_surface

    return review_surface.note_for_link_id(link_id, vault_root())


PAGES_FILE = ("local", "note-links-pages.json")
_BLOCKED_PREFIXES = ((".amp", "worktrees"), ("notes", "sensitive"), ("notes", "chrome-tabs"))
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data: blob:; font-src data:; connect-src 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'")


def resolve_page(name: str) -> Path | None:
    """The allowlisted local HTML page for a /page/<name> link, or None.
    The list is read per request, so adding a page needs no restart."""
    import json

    name = (name or "").strip("/")
    if not name or not all(ch.isalnum() or ch in "-_" for ch in name):
        return None
    root = vault_root()
    try:
        pages = json.loads((root.joinpath(*PAGES_FILE)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rel = pages.get(name) if isinstance(pages, dict) else None
    if not isinstance(rel, str):
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel.lower().endswith((".html", ".htm")) or rel.startswith(".") or "/." in rel:
        return None
    path = (root / rel).resolve()
    try:
        inside = path.relative_to(root)
    except ValueError:
        return None
    if inside.parts[:2] in _BLOCKED_PREFIXES or not path.is_file():
        return None
    return path


COMPARE_CSP = PAGE_CSP + "; sandbox allow-scripts"
COMPARE_MAX_BYTES = 150 * 1024 * 1024


def compare_for_id(link_id: str) -> tuple[Path, Path] | None:
    """(page file, checkout holding it) for a /c/<id> link (review_surface.compare_page_for_link_id)."""
    import review_surface

    return review_surface.compare_page_for_link_id(link_id, vault_root())


def compare_body(path: Path, checkout: Path) -> bytes:
    """The compare page with its local images inlined (image_compare.inline_images),
    so the page shows them over http, where file:// and relative image paths do not load."""
    import image_compare
    import review_surface

    roots = list(dict.fromkeys([vault_root().resolve(), checkout.resolve()]))
    text = path.read_text(encoding="utf-8", errors="replace")
    return image_compare.inline_images(
        text, path.parent, only_images=True, max_total=COMPARE_MAX_BYTES,
        allow=lambda p: review_surface.compare_image_ok(p, roots)).encode("utf-8")


def obsidian_uri(rel: str) -> str:
    return (f"obsidian://open?vault={quote(vault_name(), safe='')}"
            f"&file={quote(rel[:-3] if rel.endswith('.md') else rel, safe='/')}")


def open_in_obsidian(rel: str) -> bool:
    """Ask the Obsidian app to open the note. True when the launch went out.
    On Windows the caller then runs after_open, which brings Obsidian forward."""
    uri = obsidian_uri(rel)
    try:
        if IS_WINDOWS:
            os.startfile(uri)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-g", uri])
        else:
            subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


RAISE_WAIT_SECONDS = 8.0
IS_WINDOWS = os.name == "nt"
# Virtual keys the raise guard watches: the mouse buttons (1, 2, 4, 5, 6) and every
# keyboard key (8 to 254). Plain mouse movement is not a key, so it never cancels.
MOUSE_BUTTONS = frozenset((1, 2, 4, 5, 6))
WATCHED_KEYS = (*sorted(MOUSE_BUTTONS), *range(8, 255))


class KeyWatch:
    """Notices a key or mouse-button press after the click.

    A raised Obsidian takes every keystroke that follows, so any press after the
    click cancels the raise, at any point, with no grace period. On 2026-11-26 a
    stray "[]()" (Obsidian's Ctrl+K, Insert Markdown link) landed in the Windows
    issue log while Obsidian held focus after a raise test. Keys already down at
    the click (the click's own button, the Ctrl of a Ctrl+click) count only once
    they are released and pressed again. Mouse buttons count only after the
    double-click time, so the second click of a double-click never cancels.
    ``state`` is GetAsyncKeyState: bit 0x8000 means down now, bit 0x0001 pressed
    since the last call."""

    def __init__(self, state, double_click_s: float = 0.5, clock=None) -> None:
        import time

        self.state = state
        self.clock = clock or time.monotonic
        self.mouse_from = self.clock() + double_click_s
        self.pressed = False
        # Reading every key clears its "pressed since the last call" bit.
        self.held = {vk for vk in WATCHED_KEYS if self.state(vk) & 0x8000}

    def poll(self) -> bool:
        """True once the owner has pressed a key or mouse button since the click."""
        if self.pressed:
            return True
        mouse_counts = self.clock() >= self.mouse_from
        for vk in WATCHED_KEYS:
            bits = self.state(vk) & 0xFFFF
            if vk in self.held:
                if bits & 0x8000:
                    continue  # still held since the click (auto-repeat sets bit 0x0001 too)
                self.held.discard(vk)  # released: its next press counts
                bits &= 0x0001  # released and pressed again within one poll
            if vk in MOUSE_BUTTONS and not mouse_counts:
                continue
            if bits & 0x8001:
                self.pressed = True
        return self.pressed


def key_watch() -> "KeyWatch | None":
    """A KeyWatch started now (Windows), else None. The handler starts it before
    the launch so a press while Obsidian starts is seen too."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32")
        user32.GetAsyncKeyState.argtypes, user32.GetAsyncKeyState.restype = (ctypes.c_int,), ctypes.c_short
        user32.GetDoubleClickTime.argtypes, user32.GetDoubleClickTime.restype = (), ctypes.c_uint
        return KeyWatch(user32.GetAsyncKeyState, double_click_s=min(user32.GetDoubleClickTime(), 2000) / 1000)
    except Exception:
        return None


def input_tick() -> int:
    """GetTickCount on Windows (milliseconds, wraps every 49 days), else 0."""
    if not IS_WINDOWS:
        return 0
    import ctypes

    return int(ctypes.WinDLL("kernel32").GetTickCount())


def after_open(rel: str, watch: "KeyWatch | None") -> str:
    """What happened after a launch: "front" when Obsidian came to the front
    showing the note, "opened" when the front is unconfirmed, "opened (owner
    moved on)" when the owner pressed a key or mouse button after the click, so
    Obsidian was left where it was.

    Windows: the owner clicks the link in wmux, but this server
    runs hidden, so Windows let no window come forward and Obsidian switched to
    the note behind wmux. The server waits for Obsidian's window to show the note
    and raises it. This is the one focus change the owner asked for: a click on
    an Open in Obsidian link brings Obsidian up. It never sends a key
   ."""
    if not IS_WINDOWS or watch is None:
        return "opened"
    try:
        return raise_obsidian(Path(rel).stem, vault_name(), watch)
    except Exception:  # best effort: the note is already open in Obsidian
        return "opened"


def match_obsidian_title(title: str, stem: str, vault: str) -> str:
    """Obsidian titles its window "<note> - <vault> - Obsidian <version>".
    "note" when the title shows that note in that vault, "vault" when it is
    that vault's window on another note (or none), else ""."""
    suffix = re.search(r" - Obsidian(?: v?[\w.+-]+)?$", title or "")
    if not suffix:
        return ""
    head = title[:suffix.start()]
    if head == vault:
        return "vault"
    if not head.endswith(f" - {vault}"):
        return ""
    return "note" if head[:-len(f" - {vault}")] == stem else "vault"


def wait_for_window(find, wait: float, clock=None, sleep=None, cancelled=None):
    """Poll ``find`` (returning (note_window, vault_window)) until the note's
    window shows or ``wait`` seconds pass; then the note's window, else the
    vault's, else None. None at once when ``cancelled()`` turns true (the owner
    pressed a key). Polls every 50 ms so a short key tap is still seen."""
    import time

    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    cancelled = cancelled or (lambda: False)
    deadline = clock() + wait
    while True:
        if cancelled():
            return None
        hwnd, fallback = find()
        if hwnd or clock() >= deadline:
            return hwnd or fallback
        sleep(0.05)


def raise_obsidian(stem: str, vault: str, watch: KeyWatch, wait: float | None = None) -> str:
    """Bring the Obsidian window for ``vault`` to the front once it shows ``stem``
    (see after_open). Waits up to ``wait`` seconds for the note (a cold start
    takes a few); when the title never names it, the vault's window is raised
    anyway. Any key or mouse-button press after the click (``watch``) cancels
    the raise, before or after the window shows. Windows only."""
    import ctypes
    from ctypes import wintypes

    HWND, DWORD, BOOL = wintypes.HWND, wintypes.DWORD, wintypes.BOOL
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)
    for fn, args, res in (
            (user32.GetForegroundWindow, (), HWND),
            (user32.GetWindowThreadProcessId, (HWND, ctypes.POINTER(DWORD)), DWORD),
            (user32.GetWindowTextW, (HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int),
            (user32.IsWindowVisible, (HWND,), BOOL),
            (user32.IsIconic, (HWND,), BOOL),
            (user32.ShowWindow, (HWND, ctypes.c_int), BOOL),
            (user32.SetForegroundWindow, (HWND,), BOOL),
            (user32.BringWindowToTop, (HWND,), BOOL),
            (user32.AttachThreadInput, (DWORD, DWORD, BOOL), BOOL),
            (user32.EnumWindows, (enum_proc, wintypes.LPARAM), BOOL),
            (kernel32.GetCurrentThreadId, (), DWORD),
            (kernel32.OpenProcess, (DWORD, BOOL, DWORD), wintypes.HANDLE),
            (kernel32.QueryFullProcessImageNameW, (wintypes.HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)), BOOL),
            (kernel32.CloseHandle, (wintypes.HANDLE,), BOOL)):
        fn.argtypes, fn.restype = args, res

    def is_obsidian(hwnd) -> bool:
        pid = DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return False
            return Path(buf.value).name.lower() == "obsidian.exe"
        finally:
            kernel32.CloseHandle(handle)

    def find() -> tuple:
        found: dict = {}

        def visit(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                kind = match_obsidian_title(buf.value, stem, vault)
                if kind and kind not in found and is_obsidian(hwnd):
                    found[kind] = hwnd
            return True

        user32.EnumWindows(enum_proc(visit), 0)
        return found.get("note"), found.get("vault")

    hwnd = wait_for_window(find, RAISE_WAIT_SECONDS if wait is None else wait, cancelled=watch.poll)
    # Checked again just before the raise: a press after the window showed counts too.
    if watch.poll():
        return "opened (owner moved on)"
    if not hwnd:
        return "opened"
    return "front" if bring_to_front(user32, kernel32, hwnd) else "opened"


def bring_to_front(user32, kernel32, hwnd) -> bool:
    """Windows refuses SetForegroundWindow from a background process unless it
    shares input with the foreground thread or sent the last input event. Try
    the plain call, then shared input. Never a synthetic key:
    the Alt press this used to send went through the owner's keyboard state
    while they typed. When both are refused, Obsidian stays behind with the note
    open and its taskbar button flashes."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.SetForegroundWindow(hwnd) and user32.GetForegroundWindow() == hwnd:
        return True
    fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)
    return user32.GetForegroundWindow() == hwnd


def page(title: str, body: str, status_line: str = "") -> bytes:
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;margin:16px;max-width:860px;color:#1b1b1b;background:#fff}"
            "pre{white-space:pre-wrap;word-wrap:break-word;font:14px/1.5 ui-monospace,Consolas,monospace}"
            ".s{color:#555;font-size:13px}@media(prefers-color-scheme:dark){body{background:#1b1b1b;color:#e8e8e8}.s{color:#aaa}}</style>"
            f"<h3>{html.escape(title)}</h3><p class=s>{html.escape(status_line)}</p>{body}").encode("utf-8")


_REFUSALS_LOGGED: dict[str, float] = {}
_REFUSALS_LOCK = threading.Lock()


def log_refusal_once(path: str, every_s: float = 60.0, clock=None) -> bool:
    """True at most once a minute per path, so a page that fires requests in a
    loop cannot fill the log. At most 256 paths are remembered."""
    import time

    now = (clock or time.monotonic)()
    key = path[:80]
    with _REFUSALS_LOCK:
        last = _REFUSALS_LOGGED.get(key)
        if last is not None and now - last < every_s:
            return False
        if len(_REFUSALS_LOGGED) >= 256:
            _REFUSALS_LOGGED.clear()
        _REFUSALS_LOGGED[key] = now
        return True


def log(line: str) -> None:
    """One line per open on stderr. ensure points a detached server's stderr at
    system/state/logs/note-links.server.log, so a click can be checked later."""
    import time

    try:
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}", file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "switchboard-note-links/1"

    def log_message(self, fmt: str, *args) -> None:  # quiet: no request log
        return

    def send(self, code: int, payload: bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def allowed(self, top_level_from_anywhere: bool = False) -> bool:
        """Only a direct visit to this server. A Host other than 127.0.0.1 or
        localhost on our port is a DNS-rebinding page that wants to read notes;
        a cross-site Sec-Fetch-Site is another page firing /open (an <img> tag).
        A click in the wmux panel is a top-level visit (Sec-Fetch-Site none).
        A /page link may also be a click from another site (a Google Sheet), but
        only as a top-level document navigation, never a fetch, image, or frame."""
        port_ = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in (f"127.0.0.1:{port_}", f"localhost:{port_}"):
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "none").lower()
        if site in ("none", "same-origin"):
            return True
        if not top_level_from_anywhere:
            return False
        return ((self.headers.get("Sec-Fetch-Mode") or "").lower() == "navigate"
                and (self.headers.get("Sec-Fetch-Dest") or "").lower() == "document")

    def serve_page(self, name: str) -> None:
        page_path = resolve_page(name)
        if page_path is None:
            return self.send(404, page("Page not found", "", f"No local page named {name or '(empty)'}"))
        self.send(200, page_path.read_bytes(), extra={
            "Content-Security-Policy": PAGE_CSP,
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
        })

    def serve_compare(self, link_id: str) -> None:
        found = compare_for_id(link_id)
        if found is None:
            return self.send(404, page("Compare page not found", "", f"No compare page with id {link_id or '(empty)'}"))
        try:
            body = compare_body(*found)
        except OSError:
            return self.send(404, page("Compare page not found", "", "The compare page could not be read"))
        log(f"compare {found[0].name}: served")
        self.send(200, body, extra={
            "Content-Security-Policy": COMPARE_CSP,
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
        })

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        is_page = parts.path.startswith("/page/")
        if not self.allowed(top_level_from_anywhere=is_page):
            # Logged: a click the owner made that never opened
            # anything shows here instead of leaving no trace at all.
            if log_refusal_once(parts.path):
                log(f"refused {parts.path[:80]}: Host {(self.headers.get('Host') or '(none)')[:80]!r}, "
                    f"Sec-Fetch-Site {(self.headers.get('Sec-Fetch-Site') or '(none)')[:40]!r}")
            return self.send(403, b"forbidden", "text/plain; charset=utf-8")
        if is_page:
            return self.serve_page(parts.path[len("/page/"):])
        if parts.path.startswith("/c/"):
            return self.serve_compare(parts.path[len("/c/"):])
        if parts.path == "/health":
            return self.send(200, b"ok", "text/plain; charset=utf-8", extra={PROTOCOL_HEADER: PROTOCOL})
        short = parts.path.startswith("/n/")
        if parts.path not in ("/open", "/view") and not short:
            return self.send(404, page("Not found", "", "switchboard note links serve /n/<id>, /c/<id> and /open?file=<note>.md"))
        if short:
            # The short link review_surface.http_note_url renders: it
            # opens like /open, and an id that names no note is a 404 like a bad path.
            rel = note_for_id(parts.path[len("/n/"):]) or ""
        else:
            rel = (parse_qs(parts.query).get("file") or [""])[0]
        note = resolve_note(rel)
        if note is None:
            if parts.path != "/view":
                log(f"not found {parts.path[:80]}")
            return self.send(404, page("Note not found", "", f"No vault note at {rel or '(empty path)'}"))
        rel_path = note.relative_to(vault_root()).as_posix()
        text = f"<pre>{html.escape(note.read_text(encoding='utf-8', errors='replace'))}</pre>"
        if parts.path == "/view":
            if (parse_qs(parts.query).get("opened") or [""])[0] == "1":
                # A small page: the note is in Obsidian now, and its text stays one click away.
                return self.send(200, page(rel_path, f"<details><summary>Show the note here</summary>{text}</details>",
                                           "Opened in Obsidian."))
            return self.send(200, page(rel_path, text))
        started = input_tick()
        watch = key_watch()  # before the launch, so a press while Obsidian starts counts
        if not open_in_obsidian(rel_path):
            log(f"open {rel_path}: failed")
            return self.send(200, page(rel_path, text, "Obsidian did not open; the note is below."))
        # Answer first, then raise: the panel never waits on Obsidian, and the
        # redirect leaves a /view URL behind, so a reload or a restored panel tab
        # never opens Obsidian again.
        self.send(303, b"", extra={"Location": f"/view?file={quote(rel_path, safe='/')}&opened=1"})
        outcome = after_open(rel_path, watch)
        took = (input_tick() - started) & 0xFFFFFFFF
        log(f"open {rel_path}: {outcome}" + (f" after {took} ms" if IS_WINDOWS else ""))


def answers(p: int, timeout: float = 1.5) -> bool:
    return protocol(p, timeout) is not None


def protocol(p: int, timeout: float = 1.5) -> str | None:
    """The protocol the server on port ``p`` speaks ("1" when it sends none), or None when nothing answers."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{p}/health", timeout=timeout) as resp:
            return (resp.headers.get(PROTOCOL_HEADER) or "1").strip() if resp.status == 200 else None
    except (OSError, http.client.HTTPException):
        return None


def _protocol_number(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _serves_port(cmdline: str, p: int) -> bool:
    import re

    # The interpreter, optional flags, then note-links.py as the script and serve as its
    # command: a `python -c` whose code only mentions the script never matches.
    if not re.match(r'\s*(?:"[^"]*(?:python[\w.]*|\bpy(?:\.exe)?)"|[^"\s]*python[\w.]*|[^"\s]*\bpy(?:\.exe)?)(?:\s+-[XW]\s*\S+|\s+-\d[\d.]*|\s+-(?![cm]\b)[A-Za-z]+)*'
                    r'\s+(?:"[^"]*note-links\.py"|[^"\s]*note-links\.py)\s+serve\b',
                    cmdline, re.IGNORECASE):
        return False
    m = re.search(r"--port[= ]+(\d+)", cmdline)
    return int(m.group(1)) == p if m else p == DEFAULT_PORT


def stop_stale(p: int) -> list[str]:
    """Stop an older note-links server on port ``p`` so ensure can start this one.
    Only a process whose command line is note-links.py serve on that port is stopped."""
    import dataclasses

    import task_leftovers

    stopped = []
    for proc in task_leftovers.snapshot():
        if proc.pid != os.getpid() and proc.name.lower().startswith("python") and _serves_port(proc.cmdline, p):
            why = task_leftovers.kill(dataclasses.asdict(proc))
            stopped.append(f"{proc.pid}: {why or 'stopped'}")
    return stopped


def serve(p: int) -> int:
    vault_root()  # resolve before serving
    server = ThreadingHTTPServer((HOST, p), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def server_log():
    """The detached server's stderr: system/state/logs/note-links.server.log when
    that folder exists, else nowhere."""
    logs = vault_root() / "system" / "state" / "logs"
    try:
        return open(logs / "note-links.server.log", "a", encoding="utf-8") if logs.is_dir() else subprocess.DEVNULL
    except OSError:
        return subprocess.DEVNULL


def ensure(p: int) -> int:
    running = protocol(p)
    if running is not None and _protocol_number(running) >= _protocol_number(PROTOCOL):
        # The same or a newer server: an older checkout's ensure never replaces it.
        print(f"note links: already serving on http://{HOST}:{p}")
        return 0
    if running is not None:
        # An older server (it predates /n/<id> links) keeps answering until stopped.
        stopped = stop_stale(p)
        print(f"note links: replacing protocol {running} server ({', '.join(stopped) or 'no process found'})")
        import time

        for _ in range(40):
            if protocol(p, 0.5) is None:
                break
            time.sleep(0.25)
        else:
            print(f"note links: protocol {running} server still answers on http://{HOST}:{p}", file=sys.stderr)
            return 1
    cmd = [sys.executable, str(Path(__file__).resolve()), "serve", "--port", str(p)]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": server_log(), "close_fds": True}
    if os.name == "nt":
        # A hidden console its children share (DETACHED_PROCESS left git without one
        # and every request stalled; see commit 6f0428e).
        kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    # The parent resolves the vault (git) so the detached child never has to.
    kwargs["env"] = {**os.environ, "SWITCHBOARD_VAULT_ROOT": str(vault_root())}
    subprocess.Popen(cmd, **kwargs)
    if hasattr(kwargs["stderr"], "close"):
        kwargs["stderr"].close()  # the child holds its own copy
    import time

    for _ in range(40):
        if answers(p, 0.5):
            print(f"note links: started on http://{HOST}:{p}")
            return 0
        time.sleep(0.25)  # a refused connection returns at once on macOS and Linux
    print(f"note links: did not answer on http://{HOST}:{p}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clickable vault-note links for wmux.")
    parser.add_argument("command", choices=("serve", "ensure", "status"))
    parser.add_argument("--port", type=int, default=port())
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args.port)
    if args.command == "ensure":
        return ensure(args.port)
    ok = answers(args.port)
    print(f"note links: {'serving' if ok else 'not running'} on http://{HOST}:{args.port}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:  # a console-less start must not open a window per child process (no_window.py)
        import no_window
        no_window.install()
    except ImportError:
        pass
    sys.exit(main())
