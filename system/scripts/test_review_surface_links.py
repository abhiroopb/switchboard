"""Note links on wmux (http mode) and the empty-pointer artifact."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_surface as rs  # noqa: E402

NOTE = "workstreams/pm-os-maintenance/CONTEXT.md"


class EmptyPointerTests(unittest.TestCase):
    def test_placeholder_obsidian_uri_stays_prose(self):
        # A placeholder "file=..." must not lose its dots and render a ".md" artifact.
        out = rs.report_text("vault registered, obsidian://open?vault=pm-os&file=... opened a real note.")
        self.assertNotIn(".md", out)
        self.assertNotIn("[Open in Obsidian]", out)
        self.assertIn("obsidian://open?vault=pm-os&file=...", out)

    def test_empty_file_param_is_not_a_link(self):
        out = rs.report_text("see obsidian://open?vault=pm-os&file= for it")
        self.assertNotIn("[obsidian:// link]", out)

    def test_real_pointer_still_renders(self):
        with mock.patch.dict(os.environ, {"PM_OS_NOTE_LINK": "file"}):
            out = rs.report_text(f"see {rs.obsidian_uri(NOTE)} now")
        self.assertIn(NOTE, out)
        self.assertIn("[obsidian:// link](obsidian://open?vault=", out)


class HttpModeTests(unittest.TestCase):
    def test_wmux_picks_http_only_when_server_answers(self):
        with mock.patch.dict(os.environ, {"PM_OS_MUX": "wmux"}):
            os.environ.pop("PM_OS_NOTE_LINK", None)
            with mock.patch.object(rs, "note_link_server_up", return_value=True):
                self.assertEqual(rs.note_link_mode(), "http")
            with mock.patch.object(rs, "note_link_server_up", return_value=False):
                self.assertNotEqual(rs.note_link_mode(), "http")

    def test_forced_mode_wins(self):
        with mock.patch.dict(os.environ, {"PM_OS_MUX": "wmux", "PM_OS_NOTE_LINK": "file"}):
            self.assertEqual(rs.note_link_mode(), "file")

    def test_http_url_round_trips(self):
        url = rs.http_note_url(NOTE)
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertEqual(rs.click_url_note(url), NOTE)
        self.assertIsNone(rs.http_url_note("http://example.com/open?file=" + NOTE))

    def test_http_render_is_stable(self):
        with mock.patch.dict(os.environ, {"PM_OS_NOTE_LINK": "http"}):
            once = rs.report_text("Review done", review_url=rs.obsidian_uri(NOTE))
            self.assertIn("[Open in Obsidian](http://127.0.0.1:", once)
            self.assertEqual(rs.report_text(once), once)


if __name__ == "__main__":
    unittest.main()
