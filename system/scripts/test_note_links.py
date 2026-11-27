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
        nl._REFUSALS_LOGGED.clear()
        # No test reaches the real Obsidian: an unpatched open launched it and,
        # since the raise fix, raised it over the owner's window. Tests that
        # check the call patch open_in_obsidian again on top of this.
        for name, value in (("open_in_obsidian", True), ("after_open", "opened"), ("key_watch", None)):
            guard = mock.patch.object(nl, name, return_value=value)
            guard.start()
            self.addCleanup(guard.stop)
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

    def test_opened_page_is_small_and_failure_shows_the_note(self):
        with mock.patch.object(nl, "after_open", return_value="front") as after, mock.patch.object(nl, "log") as log:
            body = self.get("/open?file=workstreams/demo/note.md")[1]
        self.assertIn("Opened in Obsidian.", body)
        self.assertIn("<details><summary>Show the note here</summary><pre># Hello &lt;b&gt;", body)
        log.assert_called_once()
        self.assertRegex(log.call_args[0][0], r"^open workstreams/demo/note.md: front( after \d+ ms)?$")
        self.assertEqual(after.call_args[0][0], "workstreams/demo/note.md")
        with mock.patch.object(nl, "open_in_obsidian", return_value=False),                 mock.patch.object(nl, "after_open") as after, mock.patch.object(nl, "log") as log:
            body = self.get("/open?file=workstreams/demo/note.md")[1]
        self.assertIn("Obsidian did not open; the note is below.", body)
        self.assertNotIn("<details>", body)
        self.assertIn("# Hello &lt;b&gt;", body)
        log.assert_called_once_with("open workstreams/demo/note.md: failed")
        after.assert_not_called()

    def test_open_redirects_to_view_so_a_reload_never_reopens(self):
        req = urllib.request.Request(self.base + "/n/" + __import__("review_surface").note_link_id("workstreams/demo/note.md"))

        class NoFollow(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        with mock.patch.object(nl, "open_in_obsidian", return_value=True) as opened:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.build_opener(NoFollow).open(req, timeout=10)
            self.assertEqual(caught.exception.code, 303)
            self.assertEqual(caught.exception.headers["Location"], "/view?file=workstreams/demo/note.md&opened=1")
            self.assertEqual(self.get("/view?file=workstreams/demo/note.md&opened=1")[0], 200)
        opened.assert_called_once()

    def test_view_never_opens_obsidian(self):
        with mock.patch.object(nl, "open_in_obsidian") as opened:
            code, body = self.get("/view?file=workstreams/demo/note.md")
        self.assertEqual(code, 200)
        opened.assert_not_called()
        self.assertNotIn("Opened in Obsidian", body)

    def test_short_link_opens_note(self):
        # The /n/<id> link review_surface renders on wmux.
        import review_surface as rs

        link_id = rs.note_link_id("workstreams/demo/note.md")
        with mock.patch.object(nl, "open_in_obsidian", return_value=True) as opened:
            code, body = self.get(f"/n/{link_id}")
        self.assertEqual(code, 200)
        opened.assert_called_once_with("workstreams/demo/note.md")
        self.assertIn("Opened in Obsidian", body)

    def test_short_link_finds_a_note_written_after_the_first_lookup(self):
        import review_surface as rs

        self.assertEqual(self.get(f"/n/{rs.note_link_id('workstreams/demo/note.md')}")[0], 200)
        (nl._VAULT_ROOT / "workstreams" / "demo" / "later.md").write_text("later", encoding="utf-8")
        with mock.patch.object(nl, "open_in_obsidian", return_value=True), mock.patch.object(rs, "NOTE_RESCAN_SECONDS", 0):
            self.assertEqual(self.get(f"/n/{rs.note_link_id('workstreams/demo/later.md')}")[0], 200)

    def test_short_link_for_a_deleted_note_is_404(self):
        import review_surface as rs

        gone = nl._VAULT_ROOT / "workstreams" / "demo" / "gone.md"
        gone.write_text("x", encoding="utf-8")
        with mock.patch.object(nl, "open_in_obsidian", return_value=True) as opened:
            self.assertEqual(self.get(f"/n/{rs.note_link_id('workstreams/demo/gone.md')}")[0], 200)
            gone.unlink()
            self.assertEqual(self.get(f"/n/{rs.note_link_id('workstreams/demo/gone.md')}")[0], 404)
        self.assertEqual(opened.call_count, 1)

    def test_short_link_refuses_unknown_hidden_and_sensitive(self):
        import review_surface as rs

        root = nl._VAULT_ROOT
        (root / "notes" / "sensitive").mkdir(parents=True)
        (root / "notes" / "sensitive" / "s.md").write_text("x", encoding="utf-8")
        with mock.patch.object(nl, "open_in_obsidian", return_value=True) as opened:
            for rel in ("notes/sensitive/s.md", ".amp/worktrees/t/x.md", ".git/config.md", "nope/missing.md"):
                self.assertEqual(self.get(f"/n/{rs.note_link_id(rel)}")[0], 404, rel)
            self.assertEqual(self.get("/n/../../etc")[0], 404)
            self.assertEqual(self.get("/n/")[0], 404)
        opened.assert_not_called()
        port = self.server.server_address[1]
        link = f"/n/{rs.note_link_id('workstreams/demo/note.md')}"
        self.assertEqual(self.get_with(link, {"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(self.get_with(link, {"Host": f"attacker.example:{port}"}), 403)

    def test_health_reports_protocol(self):
        port = self.server.server_address[1]
        self.assertEqual(nl.protocol(port), nl.PROTOCOL)
        self.assertTrue(nl.answers(port))

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
        nl.open_in_obsidian.assert_not_called()  # the gate runs before any open

    def test_refused_and_missing_links_are_logged(self):
        """A click that opened nothing left no trace in the log."""
        with mock.patch.object(nl, "log") as log:
            self.assertEqual(self.get_with("/n/a474a9b2b3", {"Sec-Fetch-Site": "cross-site"}), 403)
            self.assertEqual(self.get("/n/0000000000")[0], 404)
            self.assertEqual(self.get("/view?file=workstreams/demo/none.md")[0], 404)
        lines = [c.args[0] for c in log.call_args_list]
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^refused /n/a474a9b2b3: Host '127\.0\.0\.1:\d+', Sec-Fetch-Site 'cross-site'$")
        self.assertEqual(lines[1], "not found /n/0000000000")

    def test_refusals_are_logged_once_a_minute_per_path(self):
        now = [100.0]
        clock = lambda: now[0]
        once = nl.log_refusal_once
        self.assertTrue(once("/open", clock=clock))
        self.assertFalse(once("/open", clock=clock))   # a page firing requests in a loop
        self.assertTrue(once("/view", clock=clock))    # another path still logs
        now[0] += 61
        self.assertTrue(once("/open", clock=clock))

    def test_handler_starts_the_key_watch_before_the_launch(self):
        order = []
        with mock.patch.object(nl, "key_watch", side_effect=lambda: order.append("watch") or "W"), \
                mock.patch.object(nl, "open_in_obsidian", side_effect=lambda rel: order.append("open") or True), \
                mock.patch.object(nl, "after_open", return_value="opened") as after:
            self.assertEqual(self.get("/open?file=workstreams/demo/note.md")[0], 200)
        self.assertEqual(order, ["watch", "open"])
        after.assert_called_once_with("workstreams/demo/note.md", "W")

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



class EnsureUpgradeTests(unittest.TestCase):
    """ensure replaces a server that predates /n/<id> links."""

    def test_serves_port_matches_only_note_links_serve(self):
        self.assertTrue(nl._serves_port(r"python3 C:\switchboard\system\scripts\mux\note-links.py serve --port 7472", 7472))
        self.assertTrue(nl._serves_port("python3 /x/mux/note-links.py serve", nl.DEFAULT_PORT))
        self.assertFalse(nl._serves_port("python3 /x/mux/note-links.py serve --port 7473", 7472))
        self.assertFalse(nl._serves_port("python3 /x/mux/note-links.py ensure", 7472))
        self.assertFalse(nl._serves_port("python3 -m http.server 7472", 7472))
        self.assertTrue(nl._serves_port(r'"C:\Program Files\Python312\python.exe" -I "C:\switchboard\mux\note-links.py" serve', 7472))
        self.assertTrue(nl._serves_port(r"C:\Python312\python3.exe C:\Users\x\switchboard\system\scripts\mux\note-links.py serve --port 7472", 7472))
        self.assertFalse(nl._serves_port("python3 -c \"import x; print('mux/note-links.py serve --port 7472')\"", 7472))
        self.assertFalse(nl._serves_port("bash -c 'python3 /x/mux/note-links.py serve --port 7472'", 7472))
        self.assertTrue(nl._serves_port("python3 -X utf8 /x/mux/note-links.py serve", 7472))
        self.assertTrue(nl._serves_port("python3 -W ignore -u /x/mux/note-links.py serve", 7472))
        self.assertTrue(nl._serves_port(r"py -3 C:\x\mux\note-links.py serve --port 7472", 7472))
        self.assertTrue(nl._serves_port(r'"C:\Windows\py.exe" -3.12 "C:\x\mux\note-links.py" serve', 7472))
        self.assertFalse(nl._serves_port("python3 -m note-links.py serve", 7472))
        self.assertFalse(nl._serves_port("happy /x/mux/note-links.py serve", 7472))

    def test_newer_server_is_left_alone(self):
        with mock.patch.object(nl, "protocol", return_value=str(int(nl.PROTOCOL) + 1)), \
                mock.patch.object(nl, "stop_stale") as stop, mock.patch.object(nl.subprocess, "Popen") as popen:
            self.assertEqual(nl.ensure(7472), 0)
        stop.assert_not_called()
        popen.assert_not_called()

    def test_stale_server_that_will_not_stop_fails(self):
        with mock.patch.object(nl, "protocol", return_value="1"), \
                mock.patch.object(nl, "stop_stale", return_value=["11: gone or access denied"]), \
                mock.patch("time.sleep"), mock.patch.object(nl.subprocess, "Popen") as popen:
            self.assertEqual(nl.ensure(7472), 1)
        popen.assert_not_called()

    def test_probe_survives_a_broken_response(self):
        import http.client

        with mock.patch.object(nl.urllib.request, "urlopen", side_effect=http.client.BadStatusLine("x")):
            self.assertIsNone(nl.protocol(7472))

    def test_current_server_is_left_alone(self):
        with mock.patch.object(nl, "protocol", return_value=nl.PROTOCOL), \
                mock.patch.object(nl, "stop_stale") as stop, mock.patch.object(nl.subprocess, "Popen") as popen:
            self.assertEqual(nl.ensure(7472), 0)
        stop.assert_not_called()
        popen.assert_not_called()

    def test_older_server_is_replaced(self):
        answers = iter(["1", None, nl.PROTOCOL])
        with mock.patch.object(nl, "protocol", side_effect=lambda *a, **k: next(answers)), \
                mock.patch.object(nl, "answers", return_value=True), \
                mock.patch.object(nl, "stop_stale", return_value=["123: stopped"]) as stop, \
                mock.patch.object(nl, "vault_root", return_value=Path(".")), \
                mock.patch.object(nl, "server_log", return_value="LOG") as server_log, \
                mock.patch.object(nl.subprocess, "Popen") as popen:
            self.assertEqual(nl.ensure(7472), 0)
        stop.assert_called_once_with(7472)
        popen.assert_called_once()
        server_log.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["stderr"], "LOG")

    def test_server_log_is_the_logs_folder_file_or_nowhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(nl, "vault_root", return_value=root):
                self.assertIs(nl.server_log(), nl.subprocess.DEVNULL)
                (root / "system" / "state" / "logs").mkdir(parents=True)
                handle = nl.server_log()
                try:
                    self.assertEqual(Path(handle.name).name, "note-links.server.log")
                finally:
                    handle.close()


class ObsidianFrontTests(unittest.TestCase):
    """Windows: the hidden server raises Obsidian after the launch."""

    def test_title_match(self):
        m = nl.match_obsidian_title
        self.assertEqual(m("2026-11-12-windows-cos-issues - switchboard - Obsidian 1.15.1", "2026-11-12-windows-cos-issues", "switchboard"), "note")
        self.assertEqual(m("CONTEXT - switchboard - Obsidian 1.15.1", "2026-11-12-windows-cos-issues", "switchboard"), "vault")
        self.assertEqual(m("switchboard - Obsidian v1.4.16", "CONTEXT", "switchboard"), "vault")
        self.assertEqual(m("a - b - c - switchboard - Obsidian 1.15.1", "a - b - c", "switchboard"), "note")
        self.assertEqual(m("CONTEXT - other-vault - Obsidian 1.15.1", "CONTEXT", "switchboard"), "")
        self.assertEqual(m("CONTEXT - switchboard - Brave", "CONTEXT", "switchboard"), "")
        self.assertEqual(m("", "CONTEXT", "switchboard"), "")

    def test_windows_launch(self):
        startfile = mock.Mock()
        with mock.patch.object(nl, "IS_WINDOWS", True), mock.patch.object(nl.os, "startfile", startfile, create=True),                 mock.patch.object(nl, "vault_name", return_value="switchboard"):
            self.assertTrue(nl.open_in_obsidian("workstreams/demo/my note.md"))
            startfile.assert_called_once_with("obsidian://open?vault=switchboard&file=workstreams/demo/my%20note")
            startfile.side_effect = OSError("no handler")
            self.assertFalse(nl.open_in_obsidian("workstreams/demo/my note.md"))

    def test_after_open_raises_and_survives_any_error(self):
        watch = object()
        with mock.patch.object(nl, "IS_WINDOWS", True), mock.patch.object(nl, "vault_name", return_value="switchboard"),                 mock.patch.object(nl, "raise_obsidian", return_value="front") as raiser:
            self.assertEqual(nl.after_open("workstreams/demo/my note.md", watch), "front")
            raiser.assert_called_once_with("my note", "switchboard", watch)
            raiser.side_effect = RuntimeError("ctypes ArgumentError and friends")
            self.assertEqual(nl.after_open("workstreams/demo/my note.md", watch), "opened")
            raiser.reset_mock()
            self.assertEqual(nl.after_open("workstreams/demo/my note.md", None), "opened")  # no watch, no raise
            raiser.assert_not_called()
        with mock.patch.object(nl, "IS_WINDOWS", False), mock.patch.object(nl, "raise_obsidian") as raiser:
            self.assertEqual(nl.after_open("workstreams/demo/n.md", watch), "opened")
            raiser.assert_not_called()

    def test_wait_for_window(self):
        now = [0.0]
        clock = lambda: now[0]
        def sleep(sec):
            now[0] += sec
        seen = iter([(None, None), (None, 5), (9, 5)])
        self.assertEqual(nl.wait_for_window(lambda: next(seen), 8, clock, sleep), 9)
        self.assertEqual(nl.wait_for_window(lambda: (None, 5), 1, clock, sleep), 5)  # the vault's window at the deadline
        self.assertIsNone(nl.wait_for_window(lambda: (None, None), 1, clock, sleep))

    def test_wait_for_window_stops_when_the_owner_presses_a_key(self):
        now = [0.0]
        clock = lambda: now[0]
        def sleep(sec):
            now[0] += sec
        presses = iter([False, False, True])
        find = mock.Mock(return_value=(None, 5))
        self.assertIsNone(nl.wait_for_window(find, 8, clock, sleep, cancelled=lambda: next(presses)))
        self.assertEqual(find.call_count, 2)  # never polled again after the press

    def watch(self, read, clock=None):
        return nl.KeyWatch(read, double_click_s=0.0, clock=clock)

    def key_state(self, down=(), tapped=()):
        """A GetAsyncKeyState stand-in. ``down`` keys read 0x8000 (held now),
        ``tapped`` keys read 0x0001 once (pressed since the last call)."""
        state = {"down": set(down), "tapped": set(tapped)}

        def read(vk):
            bits = 0x8000 if vk in state["down"] else 0
            if vk in state["tapped"]:
                state["tapped"].discard(vk)
                bits |= 0x0001
            return bits
        return read, state

    def test_key_watch_sees_a_press_after_the_click(self):
        read, state = self.key_state()
        watch = self.watch(read)
        self.assertFalse(watch.poll())
        state["down"].add(0x4B)  # K (with or without Ctrl): Obsidian's Insert Markdown link
        self.assertTrue(watch.poll())
        state["down"].clear()
        self.assertTrue(watch.poll())  # stays cancelled

    def test_key_watch_sees_a_tap_between_polls(self):
        read, state = self.key_state()
        watch = self.watch(read)
        state["tapped"].add(0x20)  # space went down and up between two polls
        self.assertTrue(watch.poll())

    def test_key_watch_ignores_what_was_down_at_the_click(self):
        # The click's own button and the Ctrl of a Ctrl+click are down when the watch starts;
        # a tap from before the click is cleared by the first read.
        read, state = self.key_state(down={0x01, 0x11}, tapped={0x41})
        watch = self.watch(read)
        self.assertFalse(watch.poll())
        state["tapped"].add(0x11)  # auto-repeat while Ctrl stays held is not a new press
        self.assertFalse(watch.poll())
        state["down"].clear()  # released
        self.assertFalse(watch.poll())
        state["down"].add(0x11)  # Ctrl pressed again: now it counts
        self.assertTrue(watch.poll())

    def test_key_watch_sees_a_held_key_released_and_pressed_within_one_poll(self):
        read, state = self.key_state(down={0x11})
        watch = self.watch(read)
        state["down"].clear()
        state["tapped"].add(0x11)  # up, down and up again between two polls
        self.assertTrue(watch.poll())

    def test_key_watch_ignores_the_second_click_of_a_double_click(self):
        now = [0.0]
        read, state = self.key_state()
        watch = nl.KeyWatch(read, double_click_s=0.5, clock=lambda: now[0])
        state["down"].add(0x01)  # second click, inside the double-click time
        self.assertFalse(watch.poll())
        state["down"].clear()
        now[0] = 0.6
        self.assertFalse(watch.poll())
        state["down"].add(0x01)  # a click elsewhere after it counts
        self.assertTrue(watch.poll())
        # A key counts at once, inside the double-click time too.
        read2, state2 = self.key_state()
        watch2 = nl.KeyWatch(read2, double_click_s=0.5, clock=lambda: 0.0)
        state2["down"].add(0x4B)
        self.assertTrue(watch2.poll())

    def test_key_watch_counts_a_mouse_click_elsewhere(self):
        read, state = self.key_state()
        watch = self.watch(read)
        state["down"].add(0x01)
        self.assertTrue(watch.poll())

    def test_no_synthetic_key_anywhere(self):
        """The raise never sends a key (it once pressed Alt)."""
        source = (HERE / "mux" / "note-links.py").read_text(encoding="utf-8")
        for call in ("keybd_event", "SendInput", "SendKeys", "PostMessage", "SendMessage"):
            self.assertNotIn(call, source)

    def fake_user32(self, plain_ok=False, attach_ok=False):
        """A user32 stand-in: the foreground moves to hwnd 7 only by the allowed route."""
        state = {"fg": 99, "attached": False}
        calls = []
        u = mock.Mock()
        u.IsIconic.return_value = False
        u.GetForegroundWindow.side_effect = lambda: state["fg"]
        u.GetWindowThreadProcessId.return_value = 5

        def set_fg(hwnd):
            calls.append(("set", state["attached"]))
            if plain_ok or (attach_ok and state["attached"]):
                state["fg"] = hwnd
                return True
            return False

        def attach(_me, _them, on):
            state["attached"] = on
            calls.append(("attach", on))
            return True

        u.SetForegroundWindow.side_effect = set_fg
        u.AttachThreadInput.side_effect = attach
        k = mock.Mock()
        k.GetCurrentThreadId.return_value = 1
        return u, k, calls

    def test_bring_to_front_plain(self):
        u, k, calls = self.fake_user32(plain_ok=True)
        self.assertTrue(nl.bring_to_front(u, k, 7))
        self.assertEqual(calls, [("set", False)])

    def test_bring_to_front_shared_input_then_detaches(self):
        u, k, calls = self.fake_user32(attach_ok=True)
        self.assertTrue(nl.bring_to_front(u, k, 7))
        self.assertEqual(calls[-1], ("attach", False))
        u.keybd_event.assert_not_called()

    def test_bring_to_front_reports_refusal_and_sends_no_key(self):
        u, k, calls = self.fake_user32()
        self.assertFalse(nl.bring_to_front(u, k, 7))
        self.assertEqual(calls, [("set", False), ("attach", True), ("set", True), ("attach", False)])
        u.keybd_event.assert_not_called()
        u.SendInput.assert_not_called()

    def test_restores_a_minimized_window(self):
        u, k, _ = self.fake_user32(plain_ok=True)
        u.IsIconic.return_value = True
        nl.bring_to_front(u, k, 7)
        u.ShowWindow.assert_called_once_with(7, 9)

    def test_stop_stale_stops_only_the_matching_process(self):
        import task_leftovers

        procs = [task_leftovers.Proc(pid=11, ppid=1, name="python3.exe", cmdline="python3 /x/mux/note-links.py serve --port 7472"),
                 task_leftovers.Proc(pid=12, ppid=1, name="python3.exe", cmdline="python3 /x/other.py serve --port 7472"),
                 task_leftovers.Proc(pid=13, ppid=1, name="bash", cmdline="bash -c 'python3 /x/mux/note-links.py serve --port 7472'")]
        with mock.patch.object(task_leftovers, "snapshot", return_value=procs), \
                mock.patch.object(task_leftovers, "kill", return_value="") as kill:
            self.assertEqual(nl.stop_stale(7472), ["11: stopped"])
        kill.assert_called_once()
        self.assertEqual(kill.call_args[0][0]["pid"], 11)

    def test_stop_stale_skips_itself_and_reports_a_failed_kill(self):
        import task_leftovers

        me = task_leftovers.Proc(pid=os.getpid(), ppid=1, name="python3.exe", cmdline="python3 /x/mux/note-links.py serve")
        other = task_leftovers.Proc(pid=11, ppid=1, name="python3.exe", cmdline="python3 /x/mux/note-links.py serve")
        with mock.patch.object(task_leftovers, "snapshot", return_value=[me, other]), \
                mock.patch.object(task_leftovers, "kill", return_value="pid reused") as kill:
            self.assertEqual(nl.stop_stale(7472), ["11: pid reused"])
        kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
