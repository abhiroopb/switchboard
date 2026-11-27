#!/usr/bin/env python3
"""Compare pages on Windows and under wmux.

Two bugs, both found on one real compare page:

1. image_compare.py build wrote image paths with os.path.relpath and quote(),
   so on Windows every relative src was "..%5C..%5Cthursday%5Ca.jpg", which no
   browser resolves; the worker patched the page by hand.
2. A compare page was linked as file:///.../assets/compare/<page>.html, and a
   wmux pane opens only http(s) links, so the owner's click did nothing. Under
   wmux the link is now http://127.0.0.1:7472/c/<id>, served by the note-links
   bridge with the images inlined.

Wrong impls these catch: a backslash or %5C in a src on any platform; a src
that does not resolve to the image from the page's folder; a worktree outside
main whose images keep the worktree path as their main path; a crash when a
page and an image sit on different drives; a file:// link under wmux, or an
http link on a surface that is not wmux, or one to a bridge too old to serve
it; a /c/ link that changes when the page lands; a bridge that serves any .html,
a page the registry points at outside its own path, a non-image file inlined
into the page, a cross-site request, or a page without the locked-down CSP.
"""
from __future__ import annotations

import importlib.util
import json
import ntpath
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
import sys  # noqa: E402

sys.path.insert(0, str(HERE))
import image_compare  # noqa: E402
import review_surface as rs  # noqa: E402

spec = importlib.util.spec_from_file_location("note_links_cmp", HERE / "mux" / "note-links.py")
nl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nl)

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def srcs_of(doc: str) -> list[list[str]]:
    return [json.loads(m.replace("&quot;", '"')) for m in re.findall(r'data-srcs="([^"]+)"', doc)]


class _Vault(unittest.TestCase):
    """Main checkout at tmp/vault and, like Windows, a task worktree outside it at tmp/wt."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cmplinks-")).resolve()
        self.main = self.tmp / "vault"
        self.wt = self.tmp / "worktrees" / "rt-abc"
        (self.main / "workstreams" / "boots" / "assets").mkdir(parents=True)
        (self.wt / "workstreams" / "boots" / "assets").mkdir(parents=True)
        # A linked git worktree of main, as dispatch.py makes it.
        (self.main / ".git" / "worktrees" / "rt-abc").mkdir(parents=True)
        (self.wt / ".git").write_text(f"gitdir: {(self.main / '.git' / 'worktrees' / 'rt-abc').as_posix()}\n")
        env = {"SWITCHBOARD_VAULT_ROOT": str(self.main), "SWITCHBOARD_COMPARE_ROOT": str(self.wt),
               "SWITCHBOARD_WORKTREES_DIR": str(self.tmp / "worktrees"), "SWITCHBOARD_NOTE_LINK": "file",
               "IMAGE_COMPARE_OPEN_DISABLED": "1", "SWITCHBOARD_OPEN_VIA": "browser"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("SWITCHBOARD_NOTE_LINK_PORT", None)
        # A probe of the live bridge earlier in this process must not leak in.
        proto = mock.patch.object(rs, "_NOTE_LINK_PROTOCOL", None)
        proto.start()
        self.addCleanup(proto.stop)
        rs._COMPARE_IDS.clear()
        rs._COMPARE_IDS_SCANNED.clear()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def img(self, rel: str, root: Path | None = None, data: bytes = PNG) -> Path:
        path = (root or self.wt) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def build(self, *paths: Path, slug: str = "duke") -> dict:
        return image_compare.build(image_compare.page_from_spec(
            {"title": "Duke", "slug": slug, "workstream": "boots", "images": [{"path": str(p)} for p in paths]}))


class RelativeSrcs(_Vault):
    def test_srcs_are_forward_slash_and_resolve_from_the_page(self):
        a = self.img("workstreams/boots/assets/thursday/cav rich #1.jpg")
        res = self.build(a)
        doc = Path(res["path"]).read_text(encoding="utf-8")
        self.assertNotIn("%5C", doc)
        for attr in re.findall(r'(?:src|data-srcs|href)="([^"]*)"', doc):
            self.assertNotIn("\\", attr)
        srcs = srcs_of(doc)[0]
        self.assertEqual(srcs[0], "../thursday/cav%20rich%20%231.jpg")
        self.assertIn(f'src="{srcs[0]}"', doc)
        page_dir = Path(res["path"]).parent
        from urllib.parse import unquote
        self.assertTrue((page_dir / unquote(srcs[0])).resolve().samefile(a))

    def test_worktree_outside_main_maps_images_to_main(self):
        a = self.img("workstreams/boots/assets/a.png")
        res = self.build(a)
        self.assertEqual(res["images"][0]["path"], str(self.main / "workstreams/boots/assets/a.png"))
        self.assertEqual(res["main_path"], str(self.main / "workstreams/boots/assets/compare" / Path(res["path"]).name))
        # The main page and the worktree page reach the image by the same short path.
        self.assertEqual(srcs_of(Path(res["path"]).read_text(encoding="utf-8"))[0][0], "../a.png")

    def test_windows_relpath_becomes_url_path(self):
        with mock.patch.object(image_compare.os.path, "relpath", ntpath.relpath), \
                mock.patch.object(image_compare.os, "sep", "\\"):
            got = image_compare.rel_src(Path(r"C:\v\workstreams\b\assets\thursday\a b.jpg"),
                                        Path(r"C:\v\workstreams\b\assets\compare"))
        self.assertEqual(got, "../thursday/a%20b.jpg")

    def test_another_drive_is_skipped_not_a_crash(self):
        with mock.patch.object(image_compare.os.path, "relpath", side_effect=ValueError("path is on mount 'D:'")):
            self.assertIsNone(image_compare.rel_src(Path("/x/a.png"), Path("/y")))
            a = self.img("workstreams/boots/assets/a.png")
            res = self.build(a)
        srcs = srcs_of(Path(res["path"]).read_text(encoding="utf-8"))[0]
        self.assertTrue(all(s.startswith("file://") for s in srcs), srcs)


class CompareRel(unittest.TestCase):
    def test_only_html_in_a_compare_folder(self):
        ok = ["workstreams/b/assets/compare/p.html", ".amp/in/artifacts/compare/p.html", "projects/x/compare/p.htm"]
        bad = ["workstreams/b/assets/p.html", "workstreams/b/assets/compare/p.md", "notes/sensitive/compare/p.html",
               ".amp/worktrees/rt-1/workstreams/b/assets/compare/p.html", ".git/compare/p.html",
               "workstreams/../compare/p.html", "compare/p.html/../x.html", "workstreams/.hidden/compare/p.html", ""]
        for rel in ok:
            self.assertTrue(rs.compare_rel_ok(rel), rel)
        for rel in bad:
            self.assertFalse(rs.compare_rel_ok(rel), rel)

    def test_id_ignores_case_and_separators(self):
        self.assertEqual(rs.compare_link_id("Workstreams\\b\\assets\\compare\\P.html"),
                         rs.compare_link_id("workstreams/b/assets/compare/p.html"))
        self.assertRegex(rs.http_compare_url("workstreams/b/assets/compare/p.html"),
                         r"^http://127\.0\.0\.1:7472/c/[0-9a-f]{10}$")


class BuildLink(_Vault):
    def test_file_mode_keeps_the_file_link(self):
        res = self.build(self.img("workstreams/boots/assets/a.png"))
        self.assertTrue(res["link"].startswith("[Compare 1 image](file://"), res["link"])
        self.assertEqual(res["url"], res["file_url"])
        self.assertFalse(self.main.joinpath(*rs.COMPARE_REGISTRY).exists())

    def test_each_page_gets_its_own_registry_file(self):
        os.environ["SWITCHBOARD_NOTE_LINK"] = "http"
        a = self.build(self.img("workstreams/boots/assets/a.png"), slug="one")
        b = self.build(self.img("workstreams/boots/assets/b.png"), slug="two")
        reg = self.main.joinpath(*rs.COMPARE_REGISTRY)
        self.assertEqual(sorted(f.stem for f in reg.glob("*.json")),
                         sorted(r["url"].rsplit("/", 1)[-1] for r in (a, b)))

    def test_wmux_gets_the_bridge_link_before_and_after_landing(self):
        os.environ["SWITCHBOARD_NOTE_LINK"] = "http"
        res = self.build(self.img("workstreams/boots/assets/a.png"))
        url = res["url"]
        self.assertRegex(url, r"^http://127\.0\.0\.1:7472/c/[0-9a-f]{10}$")
        self.assertEqual(res["link"], f"[Compare 1 image]({url})")
        self.assertEqual(res["main_link"], res["link"])
        self.assertEqual(res["file_url"][:8], "file:///"[:8])
        link_id = url.rsplit("/", 1)[-1]
        # Before landing the registry names the worktree copy...
        self.assertEqual(rs.compare_for_link_id(link_id), Path(res["path"]))
        # ...and once the page is on main, main's copy wins under the same link.
        landed = Path(res["main_path"])
        landed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(res["path"], landed)
        self.assertEqual(rs.compare_for_link_id(link_id), landed)
        # A landed page with no registry entry is found by walking the vault.
        shutil.rmtree(self.main.joinpath(*rs.COMPARE_REGISTRY))
        self.assertEqual(rs.compare_for_link_id(link_id), landed)

    def test_an_old_bridge_keeps_the_file_link(self):
        os.environ["SWITCHBOARD_NOTE_LINK"] = "http"
        with mock.patch.object(rs, "_NOTE_LINK_PROTOCOL", "4"):
            res = self.build(self.img("workstreams/boots/assets/a.png"))
        self.assertTrue(res["url"].startswith("file://"))

    def test_registry_cannot_point_outside_the_page_path(self):
        rel = "workstreams/boots/assets/compare/p.html"
        other = self.tmp / "elsewhere" / "secret.html"
        other.parent.mkdir()
        other.write_text("x")
        rs.register_compare_page(rel, other, self.main)
        self.assertIsNone(rs.compare_for_link_id(rs.compare_link_id(rel)))
        bad_rel = "workstreams/boots/assets/p.html"
        rs.register_compare_page(bad_rel, self.img(bad_rel, self.main), self.main)
        self.assertIsNone(rs.compare_for_link_id(rs.compare_link_id(bad_rel)))
        self.assertIsNone(rs.compare_for_link_id("../../etc"))

    def test_registry_needs_a_linked_worktree_of_main(self):
        rel = "workstreams/boots/assets/compare/p.html"
        # Same tail, but in a folder that is not a worktree of main (a Downloads copy, another repo).
        stray = self.img(rel, self.tmp / "Downloads" / "x", b"<p>x</p>")
        rs.register_compare_page(rel, stray, self.main)
        self.assertIsNone(rs.compare_for_link_id(rs.compare_link_id(rel)))
        other = self.tmp / "other-repo"
        (other / ".git").mkdir(parents=True)
        rs.register_compare_page(rel, self.img(rel, other, b"<p>x</p>"), self.main)
        self.assertIsNone(rs.compare_for_link_id(rs.compare_link_id(rel)))
        rs.register_compare_page(rel, self.img(rel, self.wt, b"<p>x</p>"), self.main)
        self.assertEqual(rs.compare_for_link_id(rs.compare_link_id(rel)), self.wt / rel)

    def test_blocked_folders_ignore_case(self):
        for rel in ("Notes/Sensitive/compare/p.html", "NOTES/chrome-tabs/compare/p.html",
                    ".AMP/worktrees/x/compare/p.html"):
            self.assertFalse(rs.compare_rel_ok(rel), rel)
        self.assertTrue(rs.compare_rel_ok(".AMP/In/Artifacts/compare/p.html"))
        roots = [self.main]
        self.assertFalse(rs.compare_image_ok(self.img("Notes/Sensitive/id.png", self.main), roots))
        self.assertFalse(rs.compare_image_ok(self.img(".git/x.png", self.main), roots))
        self.assertFalse(rs.compare_image_ok(self.img("x.png", self.tmp / "Downloads"), roots))
        self.assertTrue(rs.compare_image_ok(self.img("workstreams/boots/assets/a.png", self.main), roots))
        self.assertTrue(rs.compare_image_ok(self.img(".amp/in/artifacts/a.png", self.main), roots))


class ReportLinks(_Vault):
    def page(self) -> tuple[Path, str]:
        os.environ["SWITCHBOARD_NOTE_LINK"] = "http"
        res = self.build(self.img("workstreams/boots/assets/a.png"), self.img("workstreams/boots/assets/b.png"))
        landed = Path(res["main_path"])
        landed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(res["path"], landed)
        return landed, res["url"]

    def test_report_text_turns_the_file_link_into_the_bridge_link(self):
        landed, url = self.page()
        text = f"Two boots. [Compare 2 images]({image_compare.file_url(landed)}) then /x/a.png"
        out = rs.report_text(text, link_mode="landing")
        self.assertIn(f"[Compare 2 images]({url})", out.splitlines())
        self.assertNotIn("compare/", out.replace(url, ""))
        self.assertEqual(rs.report_text(out, link_mode="landing"), out)
        self.assertTrue(rs.has_compare_link(out))
        self.assertTrue(rs.is_compare_url(url))
        self.assertEqual(rs.missing_compare_page(out + " /x/b.png"), [])

    def test_bare_compare_path_becomes_the_bridge_link(self):
        landed, url = self.page()
        out = rs.report_text(f"Page: {landed}", link_mode="landing")
        self.assertIn(f"({url})", out)

    def test_not_wmux_keeps_file_links(self):
        landed, _ = self.page()
        os.environ["SWITCHBOARD_NOTE_LINK"] = "file"
        out = rs.report_text(f"[Compare 2 images]({image_compare.file_url(landed)})", link_mode="landing")
        self.assertIn("file://", out)
        self.assertNotIn("/c/", out)


class OpenUnderWmux(_Vault):
    def test_wmux_panel_gets_the_bridge_link(self):
        os.environ.update(SWITCHBOARD_NOTE_LINK="http", SWITCHBOARD_OPEN_VIA="wmux")
        os.environ.pop("IMAGE_COMPARE_OPEN_DISABLED")
        res = self.build(self.img("workstreams/boots/assets/a.png"))
        seen: list = []
        with mock.patch.object(image_compare, "_cmux_bin", return_value=None), \
                mock.patch.object(image_compare.sys, "platform", "win32"), \
                mock.patch.object(image_compare, "_open_in_wmux", side_effect=lambda u: (seen.append(u), (True, ""))[1]):
            out = image_compare.open_page(res["path"], cwd=self.wt, force=True)
        self.assertTrue(out["opened"], out)
        self.assertEqual(seen, [res["url"]])
        self.assertEqual(out["url"], res["url"])
        self.assertEqual(out["line"], f"Opened {res['url']} in the wmux browser panel")

    def test_unopened_page_line_names_the_clickable_link(self):
        os.environ["SWITCHBOARD_NOTE_LINK"] = "http"
        res = self.build(self.img("workstreams/boots/assets/a.png"))
        out = image_compare.open_page(res["path"], cwd=self.wt)
        self.assertEqual(out["line"], f"Page: {res['url']}")
        self.assertEqual(out["path"], res["path"])

    def test_open_report_finds_the_bridge_link(self):
        os.environ.update(SWITCHBOARD_NOTE_LINK="http")
        res = self.build(self.img("workstreams/boots/assets/a.png"))
        with mock.patch.object(image_compare, "open_page", side_effect=lambda t, **k: {"opened": True, "t": t}) as op:
            image_compare.open_report_pages(f"[Compare 1 image]({res['url']})")
        self.assertEqual([c.args[0] for c in op.call_args_list], [res["path"]])


class BridgeServesComparePages(_Vault):
    def setUp(self):
        super().setUp()
        nl._VAULT_ROOT = self.main
        self.addCleanup(setattr, nl, "_VAULT_ROOT", None)
        for name, value in (("open_in_obsidian", True), ("after_open", "opened")):
            guard = mock.patch.object(nl, name, return_value=value)
            guard.start()
            self.addCleanup(guard.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), nl.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        os.environ["SWITCHBOARD_NOTE_LINK"] = "http"

    def get(self, path: str, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, "", dict(exc.headers)

    def test_serves_the_page_with_images_inlined_and_locked_down(self):
        res = self.build(self.img("workstreams/boots/assets/a.png"))
        link_id = res["url"].rsplit("/", 1)[-1]
        code, body, headers = self.get(f"/c/{link_id}")
        self.assertEqual(code, 200)
        self.assertIn('src="data:image/png;base64,', body)
        self.assertNotIn('loading="lazy"', body.split("<script>")[0])
        self.assertEqual(headers.get("Content-Security-Policy"), nl.COMPARE_CSP)
        # An opaque origin: the page's script cannot read notes or /view from the bridge.
        self.assertIn("sandbox allow-scripts", headers.get("Content-Security-Policy"))
        self.assertNotIn("allow-same-origin", headers.get("Content-Security-Policy"))
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(self.get("/health")[2].get(nl.PROTOCOL_HEADER), "5")

    def test_never_inlines_a_file_that_is_not_an_image(self):
        secret = self.img("workstreams/boots/secret.txt", self.main, b"top secret")
        page = self.main / "workstreams/boots/assets/compare/p.html"
        page.parent.mkdir(parents=True)
        page.write_text(f'<img src="{image_compare.file_url(secret)}">', encoding="utf-8")
        code, body, _ = self.get("/c/" + rs.compare_link_id("workstreams/boots/assets/compare/p.html"))
        self.assertEqual(code, 200)
        self.assertNotIn("data:", body)
        self.assertNotIn("dG9wIHNlY3JldA", body)

    def test_inlines_only_vault_images_outside_sensitive_folders(self):
        inside = self.img("workstreams/boots/assets/ok.png", self.main)
        sensitive = self.img("notes/sensitive/passport.png", self.main)
        outside = self.img("scan.png", self.tmp / "Downloads")
        page = self.main / "workstreams/boots/assets/compare/p.html"
        page.parent.mkdir(parents=True)
        page.write_text("".join(f'<img src="{image_compare.file_url(p)}">' for p in (inside, sensitive, outside)),
                        encoding="utf-8")
        code, body, _ = self.get("/c/" + rs.compare_link_id("workstreams/boots/assets/compare/p.html"))
        self.assertEqual(code, 200)
        self.assertEqual(body.count("data:image/png;base64,"), 1)
        self.assertIn(image_compare.file_url(sensitive), body)
        self.assertIn(image_compare.file_url(outside), body)

    def test_inlining_stops_at_the_page_cap(self):
        a = self.img("workstreams/boots/assets/a.png", self.main)
        b = self.img("workstreams/boots/assets/b.png", self.main)
        page = self.main / "workstreams/boots/assets/compare/p.html"
        page.parent.mkdir(parents=True)
        page.write_text(f'<img src="{image_compare.file_url(a)}"><img src="{image_compare.file_url(b)}">', encoding="utf-8")
        with mock.patch.object(nl, "COMPARE_MAX_BYTES", len(PNG) + 1):
            body = self.get("/c/" + rs.compare_link_id("workstreams/boots/assets/compare/p.html"))[1]
        self.assertEqual(body.count("data:image/png;base64,"), 1)

    def test_unknown_and_non_compare_pages_are_404(self):
        other = self.main / "workstreams/boots/assets/app.html"
        other.write_text("<p>app</p>", encoding="utf-8")
        self.assertEqual(self.get("/c/" + rs.compare_link_id("workstreams/boots/assets/app.html"))[0], 404)
        self.assertEqual(self.get("/c/0000000000")[0], 404)
        self.assertEqual(self.get("/c/../../etc/passwd")[0], 404)

    def test_cross_site_and_rebinding_are_refused(self):
        res = self.build(self.img("workstreams/boots/assets/a.png"))
        path = "/c/" + res["url"].rsplit("/", 1)[-1]
        self.assertEqual(self.get(path, {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                                         "Sec-Fetch-Dest": "document"})[0], 403)
        self.assertEqual(self.get(path, {"Host": "evil.example:80"})[0], 403)


if __name__ == "__main__":
    unittest.main()
