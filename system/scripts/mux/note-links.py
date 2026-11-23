#!/usr/bin/env python3
"""Clickable vault-note links for terminals that open only http(s) links (wmux).

wmux turns only http(s) URLs into clickable links and opens them in its
browser panel (Settings, Browser, Links), so the file:// and obsidian:// links
in CoS reports did nothing there. review_surface.note_link_mode() picks
``http`` on wmux and renders each note link as

    http://127.0.0.1:7472/open?file=<vault-relative path>.md

This small server answers that link: it opens the note in Obsidian
(obsidian://open?vault=switchboard&file=...) and shows the note's text in the panel,
so the note is readable even when Obsidian is closed.

Safety: binds 127.0.0.1 only, GET only, serves only .md files inside the vault
(no path traversal, no worktrees), sends no CORS headers, and never writes.

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
import os
import subprocess
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

HOST = "127.0.0.1"
DEFAULT_PORT = 7472


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


def open_in_obsidian(rel: str) -> bool:
    uri = f"obsidian://open?vault={quote(vault_name(), safe='')}&file={quote(rel[:-3] if rel.endswith('.md') else rel, safe='/')}"
    try:
        if os.name == "nt":
            os.startfile(uri)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-g", uri])
        else:
            subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def page(title: str, body: str, status_line: str = "") -> bytes:
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;margin:16px;max-width:860px;color:#1b1b1b;background:#fff}"
            "pre{white-space:pre-wrap;word-wrap:break-word;font:14px/1.5 ui-monospace,Consolas,monospace}"
            ".s{color:#555;font-size:13px}@media(prefers-color-scheme:dark){body{background:#1b1b1b;color:#e8e8e8}.s{color:#aaa}}</style>"
            f"<h3>{html.escape(title)}</h3><p class=s>{html.escape(status_line)}</p>{body}").encode("utf-8")


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

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        is_page = parts.path.startswith("/page/")
        if not self.allowed(top_level_from_anywhere=is_page):
            return self.send(403, b"forbidden", "text/plain; charset=utf-8")
        if is_page:
            return self.serve_page(parts.path[len("/page/"):])
        if parts.path == "/health":
            return self.send(200, b"ok", "text/plain; charset=utf-8")
        if parts.path not in ("/open", "/view"):
            return self.send(404, page("Not found", "", "switchboard note links serve /open?file=<note>.md"))
        rel = (parse_qs(parts.query).get("file") or [""])[0]
        note = resolve_note(rel)
        if note is None:
            return self.send(404, page("Note not found", "", f"No vault note at {rel or '(empty path)'}"))
        rel_path = note.relative_to(vault_root()).as_posix()
        status = ""
        if parts.path == "/open":
            status = "Opened in Obsidian." if open_in_obsidian(rel_path) else "Obsidian did not open; the note is below."
        text = note.read_text(encoding="utf-8", errors="replace")
        self.send(200, page(rel_path, f"<pre>{html.escape(text)}</pre>", status))


def answers(p: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{p}/health", timeout=timeout) as resp:
            return resp.status == 200
    except OSError:
        return False


def serve(p: int) -> int:
    vault_root()  # resolve before serving
    server = ThreadingHTTPServer((HOST, p), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def ensure(p: int) -> int:
    if answers(p):
        print(f"note links: already serving on http://{HOST}:{p}")
        return 0
    cmd = [sys.executable, str(Path(__file__).resolve()), "serve", "--port", str(p)]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        # A hidden console its children share (DETACHED_PROCESS left git without one
        # and every request stalled; see commit 6f0428e).
        kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    # The parent resolves the vault (git) so the detached child never has to.
    kwargs["env"] = {**os.environ, "SWITCHBOARD_VAULT_ROOT": str(vault_root())}
    subprocess.Popen(cmd, **kwargs)
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
