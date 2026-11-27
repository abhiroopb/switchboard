"""Note links on wmux (http mode) and the empty-pointer artifact."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_surface as rs  # noqa: E402

NOTE = "workstreams/switchboard-maintenance/CONTEXT.md"


class EmptyPointerTests(unittest.TestCase):
    def test_placeholder_obsidian_uri_stays_prose(self):
        # A placeholder "file=..." must not lose its dots and render a ".md" artifact.
        out = rs.report_text("vault registered, obsidian://open?vault=switchboard&file=... opened a real note.")
        self.assertNotIn(".md", out)
        self.assertNotIn("[Open in Obsidian]", out)
        self.assertIn("obsidian://open?vault=switchboard&file=...", out)

    def test_empty_file_param_is_not_a_link(self):
        out = rs.report_text("see obsidian://open?vault=switchboard&file= for it")
        self.assertNotIn("[obsidian:// link]", out)

    def test_real_pointer_still_renders(self):
        with mock.patch.dict(os.environ, {"SWITCHBOARD_NOTE_LINK": "file"}):
            out = rs.report_text(f"see {rs.obsidian_uri(NOTE)} now")
        self.assertIn(NOTE, out)
        self.assertIn("[obsidian:// link](obsidian://open?vault=", out)


class HttpModeTests(unittest.TestCase):
    def test_wmux_picks_http_only_when_server_answers(self):
        with mock.patch.dict(os.environ, {"SWITCHBOARD_MUX": "wmux"}):
            os.environ.pop("SWITCHBOARD_NOTE_LINK", None)
            with mock.patch.object(rs, "note_link_server_up", return_value=True):
                self.assertEqual(rs.note_link_mode(), "http")
            with mock.patch.object(rs, "note_link_server_up", return_value=False):
                self.assertNotEqual(rs.note_link_mode(), "http")

    def test_forced_mode_wins(self):
        with mock.patch.dict(os.environ, {"SWITCHBOARD_MUX": "wmux", "SWITCHBOARD_NOTE_LINK": "file"}):
            self.assertEqual(rs.note_link_mode(), "file")

    def test_http_url_round_trips(self):
        # A vault of its own: the short /n/<id> link resolves only to a note that exists.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / NOTE).parent.mkdir(parents=True)
            (root / NOTE).write_text("# note", encoding="utf-8")
            with mock.patch.object(rs, "vault_root", return_value=root):
                url = rs.http_note_url(NOTE)
                self.assertTrue(url.startswith("http://127.0.0.1:"))
                self.assertEqual(rs.click_url_note(url), NOTE)
        self.assertIsNone(rs.http_url_note("http://example.com/open?file=" + NOTE))

    def test_http_render_is_stable(self):
        with mock.patch.dict(os.environ, {"SWITCHBOARD_NOTE_LINK": "http"}):
            once = rs.report_text("Review done", review_url=rs.obsidian_uri(NOTE))
            self.assertIn("[Open in Obsidian](http://127.0.0.1:", once)
            self.assertEqual(rs.report_text(once), once)



class ShortHttpLinkTests(unittest.TestCase):
    """wmux opens only an http(s) URL it sees whole on one line, and Claude Code
    there prints [label](url) as "label (url)" and wraps long lines, so the
    note link must be short."""

    LONG = "workstreams/example-boots/2026-11-26-example-lineup-comparison.md"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "workstreams" / "example-boots").mkdir(parents=True)
        (self.root / self.LONG).write_text("# note", encoding="utf-8")
        env = {"SWITCHBOARD_VAULT_ROOT": str(self.root), "SWITCHBOARD_NOTE_LINK": "http"}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("SWITCHBOARD_NOTE_LINK_PORT", None)
        rs._NOTE_IDS.clear()
        rs._NOTE_IDS_SCANNED.clear()

    def tearDown(self):
        self.env.stop()
        rs._NOTE_IDS.clear()
        self.tmp.cleanup()

    def test_link_is_short_and_stable(self):
        url = rs.http_note_url(self.LONG)
        self.assertEqual(url, f"http://127.0.0.1:7472/n/{rs.note_link_id(self.LONG)}")
        self.assertLessEqual(len(url), 40)
        self.assertEqual(rs.note_link_id(self.LONG), rs.note_link_id(self.LONG[:-3]))
        self.assertEqual(rs.note_link_id(self.LONG), rs.note_link_id(self.LONG.replace("/", "\\")))

    def test_short_and_legacy_links_name_the_note(self):
        self.assertEqual(rs.click_url_note(rs.http_note_url(self.LONG)), self.LONG)
        self.assertEqual(rs.click_url_note(rs.http_open_url(self.LONG)), self.LONG)
        self.assertIsNone(rs.http_url_note("http://127.0.0.1:7472/n/0000000000"))
        self.assertIsNone(rs.http_url_note("http://127.0.0.1:7472/n/not-an-id"))
        self.assertIsNone(rs.http_url_note("http://example.com/n/" + rs.note_link_id(self.LONG)))

    def test_review_link_puts_the_short_link_first(self):
        out = rs.review_link(rs.obsidian_uri(self.LONG))
        path, line = out.splitlines()
        self.assertEqual(path, self.LONG)
        self.assertTrue(line.startswith(f"[Open in Obsidian]({rs.http_note_url(self.LONG)}) · [obsidian:// link](obsidian://"))

    def test_render_is_stable_for_short_and_legacy_links(self):
        uri = rs.obsidian_uri(self.LONG)
        once = rs.report_text("Lineup done", review_url=uri)
        self.assertIn(rs.http_note_url(self.LONG), once)
        self.assertEqual(rs.report_text(once), once)
        legacy = once.replace(rs.http_note_url(self.LONG), rs.http_open_url(self.LONG))
        self.assertEqual(rs.report_text(legacy), once)

    def test_link_command_prints_the_pointer_lines(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(rs, "note_roots", return_value=[self.root]):
            self.assertEqual(rs.main(["link", self.LONG]), 0)
        self.assertEqual(out.getvalue().strip(), rs.review_link(rs.obsidian_uri(self.LONG)))
        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(rs, "note_roots", return_value=[self.root]):
            self.assertEqual(rs.main(["link", "workstreams/none/missing.md"]), 2)

    def test_older_server_gets_the_long_link(self):
        with mock.patch.object(rs, "_NOTE_LINK_PROTOCOL", "1"):
            self.assertEqual(rs.http_note_url(self.LONG), rs.http_open_url(self.LONG))
        with mock.patch.object(rs, "_NOTE_LINK_PROTOCOL", "2"):
            self.assertIn("/n/", rs.http_note_url(self.LONG))

    def test_id_ignores_case(self):
        self.assertEqual(rs.note_link_id(self.LONG), rs.note_link_id(self.LONG.upper()))
        self.assertEqual(rs.note_for_link_id(rs.note_link_id("Workstreams/" + self.LONG.split("/", 1)[1]), self.root), self.LONG)

    def test_misses_rescan_at_most_once_per_window(self):
        later = "workstreams/example-boots/later.md"
        with mock.patch.object(rs, "_scan_note_ids", wraps=rs._scan_note_ids) as scan, \
                mock.patch("time.monotonic", side_effect=[100.0, 101.0, 102.0, 200.0]):
            self.assertEqual(rs.note_for_link_id(rs.note_link_id(self.LONG), self.root), self.LONG)
            (self.root / later).write_text("x", encoding="utf-8")
            self.assertIsNone(rs.note_for_link_id(rs.note_link_id(later), self.root))
            self.assertIsNone(rs.note_for_link_id("0000000000", self.root))
            self.assertEqual(rs.note_for_link_id(rs.note_link_id(later), self.root), later)
        self.assertEqual(scan.call_count, 2)

    def test_hidden_and_sensitive_notes_have_no_id(self):
        (self.root / "notes" / "sensitive").mkdir(parents=True)
        (self.root / "notes" / "sensitive" / "s.md").write_text("x", encoding="utf-8")
        (self.root / ".amp" / "in").mkdir(parents=True)
        (self.root / ".amp" / "in" / "x.md").write_text("x", encoding="utf-8")
        for rel in ("notes/sensitive/s.md", ".amp/in/x.md"):
            self.assertIsNone(rs.note_for_link_id(rs.note_link_id(rel), self.root), rel)

if __name__ == "__main__":
    unittest.main()
