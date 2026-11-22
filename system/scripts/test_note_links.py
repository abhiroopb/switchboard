"""mux/note-links.py serves only vault notes."""
import importlib.util
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("note_links", HERE / "mux" / "note-links.py")
nl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nl)


class NoteLinksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        (root / "workstreams" / "demo").mkdir(parents=True)
        (root / "workstreams" / "demo" / "note.md").write_text("# Hello <b>\n", encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "config.md").write_text("secret", encoding="utf-8")
        (root / ".amp" / "worktrees" / "t").mkdir(parents=True)
        (root / ".amp" / "worktrees" / "t" / "x.md").write_text("wt", encoding="utf-8")
        (root / "data.json").write_text("{}", encoding="utf-8")
        nl._VAULT_ROOT = root
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), nl.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        nl._VAULT_ROOT = None
        self.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, ""

    def test_view_serves_escaped_note(self):
        code, body = self.get("/view?file=workstreams/demo/note.md")
        self.assertEqual(code, 200)
        self.assertIn("# Hello &lt;b&gt;", body)

    def test_open_launches_obsidian_uri(self):
        with mock.patch.object(nl, "open_in_obsidian", return_value=True) as opened:
            code, body = self.get("/open?file=workstreams/demo/note")
        self.assertEqual(code, 200)
        opened.assert_called_once_with("workstreams/demo/note.md")
        self.assertIn("Opened in Obsidian", body)

    def get_with(self, path, headers):
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_refuses_rebinding_host_and_cross_site(self):
        port = self.server.server_address[1]
        self.assertEqual(self.get_with("/view?file=workstreams/demo/note.md", {"Host": f"attacker.example:{port}"}), 403)
        self.assertEqual(self.get_with("/open?file=workstreams/demo/note.md", {"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(self.get_with("/view?file=workstreams/demo/note.md", {"Host": f"localhost:{port}", "Sec-Fetch-Site": "none"}), 200)

    def test_refuses_sensitive_notes(self):
        root = nl._VAULT_ROOT
        (root / "notes" / "sensitive").mkdir(parents=True)
        (root / "notes" / "sensitive" / "s.md").write_text("x", encoding="utf-8")
        self.assertEqual(self.get("/view?file=notes/sensitive/s.md")[0], 404)

    def write_pages(self, mapping):
        import json

        root = nl._VAULT_ROOT
        (root / "local").mkdir(exist_ok=True)
        (root / "local" / "note-links-pages.json").write_text(json.dumps(mapping), encoding="utf-8")
        (root / "workstreams" / "demo" / "app").mkdir(exist_ok=True)
        (root / "workstreams" / "demo" / "app" / "Plan.html").write_text("<!doctype html><p>plan</p>", encoding="utf-8")

    def get_page(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, "", {}

    def test_page_serves_allowlisted_html_with_locked_down_headers(self):
        self.write_pages({"plan": "workstreams/demo/app/Plan.html"})
        code, body, headers = self.get_page("/page/plan")
        self.assertEqual(code, 200)
        self.assertIn("<p>plan</p>", body)
        self.assertIn("connect-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_page_header_defaults(self):
        self.write_pages({"plan": "workstreams/demo/app/Plan.html"})
        # No Sec-Fetch headers: a local non-browser client, which can read the file anyway.
        self.assertEqual(self.get_page("/page/plan")[0], 200)
        self.assertEqual(self.get_page("/page/plan", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        same_site = {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        self.assertEqual(self.get_page("/page/plan", same_site)[0], 200)

    def test_page_allows_cross_site_click_but_not_fetch_or_frame(self):
        self.write_pages({"plan": "workstreams/demo/app/Plan.html"})
        click = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        self.assertEqual(self.get_page("/page/plan", click)[0], 200)
        for dest, mode in (("iframe", "navigate"), ("empty", "cors"), ("image", "no-cors"), ("script", "no-cors")):
            hdrs = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": mode, "Sec-Fetch-Dest": dest}
            self.assertEqual(self.get_page("/page/plan", hdrs)[0], 403, dest)
        port = self.server.server_address[1]
        self.assertEqual(self.get_page("/page/plan", {**click, "Host": f"attacker.example:{port}"})[0], 403)
        # Notes keep the old rule: a cross-site click never opens or reads a note.
        self.assertEqual(self.get_with("/view?file=workstreams/demo/note.md", click), 403)

    def test_page_refuses_unlisted_and_unsafe_targets(self):
        self.assertEqual(self.get_page("/page/plan")[0], 404)  # no allowlist file
        root = nl._VAULT_ROOT
        self.write_pages({
            "plan": "workstreams/demo/app/Plan.html",
            "up": "../outside.html",
            "md": "workstreams/demo/note.md",
            "git": ".git/x.html",
            "wt": ".amp/worktrees/t/x.html",
            "gone": "workstreams/demo/app/missing.html",
        })
        (root / ".amp" / "worktrees" / "t" / "x.html").write_text("wt", encoding="utf-8")
        for name in ("up", "md", "git", "wt", "gone", "other", "..%2Fplan", ""):
            self.assertEqual(self.get_page(f"/page/{name}")[0], 404, name)
        self.assertEqual(self.get_page("/page/plan")[0], 200)
        # Traversal and blocked targets are refused by the path check, not only the name filter.
        outside = Path(tempfile.mkdtemp()) / "x.html"
        outside.write_text("out", encoding="utf-8")
        (root / "notes" / "sensitive").mkdir(parents=True)
        (root / "notes" / "sensitive" / "s.html").write_text("s", encoding="utf-8")
        self.write_pages({
            "abs": str(outside),
            "dots": "workstreams/demo/../../" + "x.html",
            "sens": "notes/sensitive/s.html",
            "plan": "workstreams/demo/app/Plan.html",
        })
        for name in ("abs", "dots", "sens"):
            self.assertEqual(self.get_page(f"/page/{name}")[0], 404, name)
        (root / "local" / "note-links-pages.json").write_text('["workstreams/demo/app/Plan.html"]', encoding="utf-8")
        self.assertEqual(self.get_page("/page/plan")[0], 404)

    def test_refuses_everything_else(self):
        for path in ("../../etc/passwd", ".git/config.md", ".amp/worktrees/t/x.md", "data.json", "", "missing.md"):
            self.assertEqual(self.get(f"/view?file={path}")[0], 404, path)
        self.assertEqual(self.get("/other")[0], 404)


if __name__ == "__main__":
    unittest.main()
