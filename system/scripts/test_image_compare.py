#!/usr/bin/env python3
"""Tests for image_compare.py and the compare-page helpers in review_surface.py.

Images shared with the owner go on one compare page (external-replies Rule 14
item 2).

Wrong impls these catch: a page that loads anything from the network; a
worktree path as the link (gone after reap); a description or caption that
is not escaped or not verbatim; an --image after a --group landing outside
that group; a page auto-built for one image or when a compare link is already
there; the compare link sorting after the per-image links; a draft's image
paths counted as report images; a link to a main path that does not exist yet;
an opener that runs twice for one page version, or opens a page that is not
there; a bare `assets/...html` page that draws "⚠️ Image not linked".
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import image_compare  # noqa: E402
import review_surface  # noqa: E402

# Never open a browser from a test; the Opener tests use a recorder instead.
os.environ["IMAGE_COMPARE_OPEN_DISABLED"] = "1"
# Never add a tab to the real cmux either;
# the cmux tests below mock _cmux_bin.
os.environ["SWITCHBOARD_OPEN_VIA"] = "browser"

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


# These tests spell POSIX paths and file:// URLs, or start an extensionless fake opener.
posix_only = unittest.skipIf(os.name == "nt", "expects POSIX paths and URLs, or an extensionless fake opener")


class _Vault(unittest.TestCase):
    """A temp main checkout with a task worktree dir; env points both helpers at it."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="imgcmp-")).resolve()
        self.main = self.tmp / "vault"
        self.wt = self.main / ".amp" / "worktrees" / "rt-abc"
        (self.wt / "workstreams" / "demo" / "assets").mkdir(parents=True)
        (self.main / "workstreams" / "other" / "assets").mkdir(parents=True)
        self._env = {k: os.environ.get(k) for k in ("SWITCHBOARD_VAULT_ROOT", "SWITCHBOARD_COMPARE_ROOT", "SWITCHBOARD_WORKTREES_DIR")}
        os.environ["SWITCHBOARD_VAULT_ROOT"] = str(self.main)
        os.environ["SWITCHBOARD_COMPARE_ROOT"] = str(self.wt)
        os.environ.pop("SWITCHBOARD_WORKTREES_DIR", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def img(self, rel: str, root: pathlib.Path | None = None) -> pathlib.Path:
        path = (root or self.wt) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        return path


class BuildPage(_Vault):
    def test_groups_labels_caveats_and_verbatim_text(self):
        full = self.img("workstreams/demo/assets/c1-full.png")
        crop = self.img("workstreams/demo/assets/c1-crop.png")
        page = image_compare.page_from_spec({
            "title": "Covers <draft>", "workstream": "demo", "caveats": ["All text inside the 7:1 crop."],
            "groups": [{"key": "C1", "name": "Option A", "badge": "Recommended",
                        "images": [{"path": str(full), "label": "Full banner"}, {"path": str(crop), "label": "7:1 crop"}],
                        "caveats": ["No faces behind the text."]}],
            "texts": [{"label": "Description 1", "text": "Launch notes: <b>free</b>\n  every week & more"}],
        })
        res = image_compare.build(page)
        doc = pathlib.Path(res["path"]).read_text()
        self.assertEqual(pathlib.Path(res["path"]), self.wt / "workstreams/demo/assets/compare" / pathlib.Path(res["path"]).name)
        self.assertIn("Covers &lt;draft&gt;", doc)
        self.assertIn('<span class="key">C1</span><h2>Option A</h2><span class="badge">Recommended</span>', doc)
        self.assertIn("Full banner", doc)
        self.assertIn("7:1 crop", doc)
        self.assertIn("No faces behind the text.", doc)
        self.assertIn("All text inside the 7:1 crop.", doc)
        self.assertIn("<pre>Launch notes: &lt;b&gt;free&lt;/b&gt;\n  every week &amp; more</pre>", doc)
        self.assertIn("Each image", doc)

    @posix_only
    def test_page_is_offline_and_the_link_names_the_live_copy(self):
        a = self.img("workstreams/demo/assets/a.png")
        b = self.img("workstreams/demo/assets/b.png")
        res = image_compare.build(image_compare.page_from_spec(
            {"title": "AB", "workstream": "demo", "images": [{"path": str(a)}, {"path": str(b)}]}))
        doc = pathlib.Path(res["path"]).read_text()
        self.assertNotRegex(doc, r"(?:src|href)=\"https?://")
        self.assertNotIn("@import", doc)
        main_page = self.main / "workstreams/demo/assets/compare" / pathlib.Path(res["path"]).name
        self.assertEqual(res["main_path"], str(main_page))
        # Before the branch lands only the worktree page exists, so the link names it
        # and main_link names where it lands (an event page linked main and was missing).
        self.assertEqual(res["file_url"], "file://" + pathlib.Path(res["path"]).as_posix())
        self.assertTrue(pathlib.Path(res["path"]).exists())
        self.assertEqual(res["link"], f"[Compare 2 images]({res['file_url']})")
        self.assertEqual(res["main_link"], f"[Compare 2 images](file://{main_page.as_posix()})")
        # Once the page is on main, the link names main.
        main_page.parent.mkdir(parents=True, exist_ok=True)
        main_page.write_text("landed")
        res2 = image_compare.build(image_compare.page_from_spec(
            {"title": "AB", "workstream": "demo", "images": [{"path": str(a)}, {"path": str(b)}]}))
        self.assertEqual(res2["file_url"], "file://" + main_page.as_posix())
        self.assertEqual([r["path"] for r in res["images"]],
                         [str(self.main / "workstreams/demo/assets/a.png"), str(self.main / "workstreams/demo/assets/b.png")])
        # Main-relative source first, then the path as given as a fallback the page tries.
        srcs = json.loads(re.search(r'data-srcs="([^"]+)"', doc).group(1).replace("&quot;", '"'))
        self.assertEqual(srcs[0], "../a.png")
        self.assertIn("file://" + a.as_posix(), srcs)
        self.assertIn("A", doc)  # a single-image card still gets a header

    @posix_only
    def test_image_in_another_checkout_maps_to_main_and_embed_inlines(self):
        other = self.img("workstreams/other/assets/x.png", root=self.main / ".amp" / "worktrees" / "rt-zzz")
        res = image_compare.build(image_compare.page_from_spec(
            {"title": "X", "workstream": "demo", "images": [{"path": str(other), "label": "X"}]}), embed=False)
        doc = pathlib.Path(res["path"]).read_text()
        self.assertIn("../../../other/assets/x.png", doc)
        self.assertEqual(res["images"][0]["path"], str(self.main / "workstreams/other/assets/x.png"))
        res = image_compare.build(image_compare.page_from_spec(
            {"title": "X", "slug": "x-embed", "workstream": "demo", "images": [{"path": str(other)}]}), embed=True)
        self.assertIn("data:image/png;base64,", pathlib.Path(res["path"]).read_text())

    def test_cli_keeps_group_order(self):
        a, b, c = (self.img(f"workstreams/demo/assets/{n}.png") for n in "abc")
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "image_compare.py"), "build", "--title", "Before and after",
                            "--workstream", "demo", "--caveat", "Page caveat",
                            "--group", "Before::Today", "--image", f"{a}::Old", "--caveat", "Before caveat",
                            "--group", "After", "--image", f"{b}::New full", "--image", f"{c}::New crop",
                            "--text", "Copy::verbatim words"],
                           capture_output=True, text=True, env=os.environ)
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual([i["label"] for i in res["images"]], ["Before", "After · New full", "After · New crop"])
        doc = pathlib.Path(res["path"]).read_text()
        before = doc.index("<h2>Before</h2>")
        self.assertLess(before, doc.index("Before caveat"), "caveat after --group belongs to that group")
        self.assertLess(doc.index("Before caveat"), doc.index("<h2>After</h2>"))
        self.assertLess(doc.index("Page caveat"), before)
        link = subprocess.run([sys.executable, str(SCRIPT_DIR / "image_compare.py"), "build", "--image", str(a),
                               "--print", "link"], capture_output=True, text=True, env=os.environ)
        self.assertTrue(link.stdout.startswith("[Compare 1 image](file://"), link.stdout)

    def test_cli_without_images_fails(self):
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "image_compare.py"), "build", "--title", "x"],
                           capture_output=True, text=True, env=os.environ)
        self.assertEqual(r.returncode, 2)


class FromReport(_Vault):
    @posix_only
    def test_two_images_build_one_page(self):
        a = self.img(".amp/in/artifacts/cover-1.png")
        b = self.img(".amp/in/artifacts/cover-2.png")
        res = image_compare.from_report(f"Covers: {a} and [Cover two]({b}).", workstream="demo", task_id="rt-abc")
        self.assertTrue(res["built"], res)
        self.assertEqual([i["label"] for i in res["images"]], ["Cover 1", "Cover two"])
        self.assertTrue(res["path"].endswith("workstreams/demo/assets/compare/" + res["path"].rsplit("/", 1)[-1]))
        self.assertIn("rt-abc-images.html", res["path"])

    def test_one_image_present_link_or_missing_files_do_not_build(self):
        a = self.img(".amp/in/artifacts/cover-1.png")
        self.assertFalse(image_compare.from_report(f"Cover: {a}.", workstream="demo")["built"])
        b = self.img(".amp/in/artifacts/cover-2.png")
        linked = f"Covers {a} {b}; compare workstreams/demo/assets/compare/2026-11-02-x.html"
        self.assertEqual(image_compare.from_report(linked, workstream="demo")["reason"], "compare page present")
        gone = image_compare.from_report("Covers /nope/a.png and /nope/b.png", workstream="demo")
        self.assertFalse(gone["built"])
        self.assertTrue(gone["warning"].startswith(image_compare.COMPARE_WARNING))

    def test_bad_workstream_slug_is_refused(self):
        a = self.img(".amp/in/artifacts/a.png")
        with self.assertRaises(ValueError):
            image_compare.build(image_compare.page_from_spec(
                {"title": "x", "workstream": "../../evil", "images": [{"path": str(a)}]}))

    def test_disabled_kill_switch(self):
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "image_compare.py"), "from-report", "--text", "/a.png /b.png"],
                           capture_output=True, text=True, env={**os.environ, "IMAGE_COMPARE_DISABLED": "1"})
        self.assertEqual(json.loads(r.stdout)["reason"], "disabled")


class Opener(_Vault):
    """open_page: macOS `open` once per page version, on the copy that exists now."""

    def setUp(self):
        super().setUp()
        self.log = self.tmp / "opened.log"
        rec = self.tmp / "rec.sh"
        rec.write_text(f'#!/bin/sh\necho "$1" >> "{self.log}"\n')
        rec.chmod(0o755)
        self._open_env = {k: os.environ.get(k) for k in ("SWITCHBOARD_OPEN_CMD", "IMAGE_COMPARE_OPEN_DISABLED")}
        os.environ["SWITCHBOARD_OPEN_CMD"] = str(rec)
        os.environ.pop("IMAGE_COMPARE_OPEN_DISABLED", None)

    def tearDown(self):
        for k, v in self._open_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def opened(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def page(self, root: pathlib.Path, rel: str = "workstreams/demo/assets/compare/p.html") -> pathlib.Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<html></html>")
        return path

    @posix_only
    def test_missing_main_path_opens_the_live_worktree_copy(self):
        live = self.page(self.wt)
        res = image_compare.open_page("file://" + str(self.main / "workstreams/demo/assets/compare/p.html"))
        self.assertTrue(res["opened"], res)
        self.assertEqual(res["path"], str(live))
        self.assertEqual(res["line"], f"Opened {live}")
        self.assertEqual(self.opened(), [str(live)])

    @posix_only
    def test_one_open_per_version_and_force_reopens(self):
        page = self.page(self.main)
        self.assertTrue(image_compare.open_page(str(page))["opened"])
        again = image_compare.open_page(str(page))
        self.assertFalse(again["opened"])
        self.assertEqual(again["reason"], "already opened")
        self.assertTrue(image_compare.open_page(str(page), force=True)["opened"])
        # A rebuilt page (new content) opens again; the version is a content hash,
        # so a worktree page and its landed copy count as one.
        page.write_text(page.read_text() + "<!-- rebuilt -->")
        self.assertTrue(image_compare.open_page(str(page))["opened"], "a rebuilt page opens again")
        self.assertEqual(len(self.opened()), 3)
        self.assertTrue((self.main / ".state" / "compare-opened.json").exists())

    def test_missing_page_and_kill_switch_never_run_open(self):
        self.assertEqual(image_compare.open_page(str(self.main / "nope.html"))["reason"], "not found")
        page = self.page(self.main)
        os.environ["IMAGE_COMPARE_OPEN_DISABLED"] = "1"
        res = image_compare.open_page(str(page))
        self.assertEqual((res["opened"], res["reason"], res["line"]), (False, "disabled", f"Page: {page}"))
        self.assertEqual(self.opened(), [])

    @posix_only
    def test_build_open_flag_opens_the_worktree_page(self):
        a = self.img("workstreams/demo/assets/a.png")
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "image_compare.py"), "build", "--title", "One",
                            "--workstream", "demo", "--image", f"{a}::A", "--open"],
                           capture_output=True, text=True, env=os.environ, cwd=self.wt)
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertTrue(res["opened"])
        self.assertEqual(self.opened(), [res["path"]])
        self.assertIn(f"Opened {res['path']}", r.stderr)
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "image_compare.py"), "build", "--title", "Two",
                            "--workstream", "demo", "--image", f"{a}::A"],
                           capture_output=True, text=True, env=os.environ, cwd=self.wt)
        self.assertFalse(json.loads(r.stdout)["opened"], "no TTY and no --open: nothing opens")

    @posix_only
    def test_open_report_opens_each_linked_page_once(self):
        live = self.page(self.wt, "workstreams/demo/assets/event/event-applied-verification.html")
        text = ("Screenshots on one page: workstreams/demo/assets/event/event-applied-verification.html ⏎ "
                f"[Compare](file://{self.main}/workstreams/demo/assets/event/event-applied-verification.html) ⏎ "
                "```\nnot this one: workstreams/demo/assets/compare/draft.html\n```")
        res = image_compare.open_report_pages(text)
        self.assertTrue(res["opened"])
        self.assertEqual(self.opened(), [str(live)])


class ReviewSurfaceCompare(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="imgcmp-rs-")).resolve()
        self._saved = os.environ.get("SWITCHBOARD_VAULT_ROOT")
        os.environ["SWITCHBOARD_VAULT_ROOT"] = str(self.tmp)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SWITCHBOARD_VAULT_ROOT", None)
        else:
            os.environ["SWITCHBOARD_VAULT_ROOT"] = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_image_refs_keep_order_labels_and_skip_drafts_and_hosted(self):
        text = ("A /x/a.png, then [Cover B](workstreams/d/b.jpg), file:///x/c%20d.png, https://h.example/e.png "
                "⏎ ```\nsee /x/draft.png\n``` and [a.png](/x/a.png) again")
        self.assertEqual(review_surface.report_image_refs(text),
                         [("/x/a.png", ""), ("workstreams/d/b.jpg", "Cover B"), ("/x/c d.png", "")])

    def test_missing_compare_page_needs_two_images_and_no_compare_link(self):
        self.assertEqual(review_surface.missing_compare_page("One /x/a.png"), [])
        self.assertEqual(review_surface.missing_compare_page("Two /x/a.png [B](/x/b.png)"), ["a.png", "B"])
        for linked in ("/x/a.png /x/b.png [Compare 2 images](file:///v/workstreams/d/assets/compare/p.html)",
                       "/x/a.png /x/b.png workstreams/d/assets/compare/2026-11-02-p.html"):
            with self.subTest(text=linked):
                self.assertEqual(review_surface.missing_compare_page(linked), [])
        hosted = "/x/a.png /x/b.png see https://pages.example.com/x/price-compare.html"
        self.assertEqual(review_surface.missing_compare_page(hosted), ["a.png", "b.png"])
        self.assertTrue(review_surface.compare_warning_text(["a"]).startswith(review_surface.COMPARE_WARNING))

    def test_a_compare_link_counts_as_an_image_link(self):
        self.assertEqual(review_surface.missing_image_links(
            "Cover options on workstreams/d/assets/compare/2026-11-02-covers.html"), [])

    @posix_only
    def test_report_text_leads_with_the_compare_link_and_is_stable(self):
        text = ("Note obsidian://open?vault=switchboard&file=workstreams/d/n. Covers /x/a.png and /x/b.png ⏎ "
                "[Compare 2 images](file:///v/workstreams/d/assets/compare/p.html)")
        out = review_surface.report_text(text)
        lines = out.splitlines()
        cmp_at = lines.index("[Compare 2 images](file:///v/workstreams/d/assets/compare/p.html)")
        self.assertLess(cmp_at, lines.index("[a.png](file:///x/a.png)"))
        self.assertLess(cmp_at, lines.index("[b.png](file:///x/b.png)"))
        self.assertNotIn("Compare 2 images", lines[0], "a link alone on its line leaves no inline label")
        self.assertEqual(review_surface.report_text(out), out)

    @posix_only
    def test_bare_compare_path_becomes_a_main_link(self):
        out = review_surface.report_text(
            f"Page: {self.tmp}/.amp/worktrees/rt-abc/workstreams/d/assets/compare/2026-11-02-p.html")
        self.assertIn(f"[2026-11-02-p.html](file://{self.tmp}/workstreams/d/assets/compare/2026-11-02-p.html)", out)

    @posix_only
    def test_link_modes_resolve_the_copy_that_exists(self):
        wt_page = self.tmp / ".amp/worktrees/rt-abc/workstreams/d/assets/compare/p.html"
        wt_page.parent.mkdir(parents=True)
        wt_page.write_text("x")
        main_url = f"file://{self.tmp}/workstreams/d/assets/compare/p.html"
        wt_url = f"file://{wt_page}"
        # A parked ask (resolve): a main link that is not there yet names the live copy.
        self.assertIn(f"[Compare]({wt_url})", review_surface.report_text(f"Pick one: [Compare]({main_url})"))
        self.assertIn(f"[Compare]({wt_url})", review_surface.report_text(f"Pick one: [Compare]({wt_url})"))
        # A done report (landing): the branch lands before delivery, so a worktree link names main.
        self.assertIn(f"[Compare]({main_url})",
                      review_surface.report_text(f"Done: [Compare]({wt_url})", link_mode="landing"))
        # Landed: main exists, so resolve names main.
        main_page = self.tmp / "workstreams/d/assets/compare/p.html"
        main_page.parent.mkdir(parents=True)
        main_page.write_text("x")
        self.assertIn(f"[Compare]({main_url})", review_surface.report_text(f"Pick: [Compare]({wt_url})"))
        # Outside the vault and the rule's example stay as written.
        self.assertIn("file:///elsewhere/x.html", review_surface.localize_file_urls("see file:///elsewhere/x.html"))
        self.assertEqual(review_surface.localize_file_urls("file:///abs/x.png"), "file:///abs/x.png")

    @posix_only
    def test_event_done_report_bare_assets_page_is_linked_not_warned(self):
        # A done report sentence that once drew a false warning.
        text = ("Launch notes event calendar updated and verified live: cover P1, avatar "
                "and link preview, description 2. Review note: 2026-11-02-event-calendar-image-and-description.md; "
                "screenshots on one page: workstreams/launch-notes/assets/event/event-applied-verification.html. "
                "P1 photo rights: Harper Example can confirm.")
        self.assertEqual(review_surface.missing_image_links(text), [])
        self.assertTrue(review_surface.has_compare_link(text))
        out = review_surface.report_text(text, link_mode="landing")
        self.assertIn(f"[event-applied-verification.html](file://{self.tmp}/workstreams/launch-notes/assets/event/"
                      "event-applied-verification.html)", out)
        self.assertEqual(review_surface.report_text(out, link_mode="landing"), out)
        self.assertFalse(review_surface.evaluate([text], status="done").to_json().count("image_unlinked\": [\""))

    def test_an_open_already_done_counts_as_linked(self):
        for text in ("I opened the compare page on your Mac; the screenshots are side by side.",
                     "Opened event-check.html on the owner's Mac with the two screenshots."):
            with self.subTest(text=text):
                self.assertEqual(review_surface.missing_image_links(text), [])
        self.assertEqual(review_surface.missing_image_links("The screenshots are attached below."), ["screenshots"])
        # A plain .html outside assets/, artifacts/, reviews/ is not a review page.
        self.assertFalse(review_surface.has_compare_link("Edited system/skill-overrides/web-lizard/starter-template.html"))

    def test_an_image_word_naming_a_program_is_not_an_image(self):
        # "poster" naming the next-up digest poster script is not an image.
        report = ("**Digest fix.** An empty lock folder left on main overnight made every digest tick "
                  "today stop at locked. The folder is removed. The poster and report-to-cos.sh now clear a lock "
                  "folder that has no pid file once it is older than 2 minutes. The new test fails on the old "
                  "script, and the next-up test class passes 6 of 6. This is issue 32 in "
                  "2026-11-12-windows-cos-issues.md.")
        self.assertEqual(review_surface.missing_image_links(report), [])
        self.assertEqual(review_surface.evaluate([report], status="done").image_unlinked, [])
        for text in ("The digest poster stopped at locked.",
                     "The poster now clears stale lock folders.",
                     "The poster ticks every 30 minutes.",
                     "The original poster asked again in the thread.",
                     "The screenshot script runs headless.",
                     "The thumbnail job and render.py both moved.",
                     "The poster, post-next-up-digest.sh, now retries once."):
            with self.subTest(text=text):
                self.assertEqual(review_surface.missing_image_links(text), [])

    def test_real_poster_and_image_mentions_stay_flagged(self):
        for text, found in (("The poster shows the launch date.", ["poster"]),
                            ("Two posters are attached below.", ["posters"]),
                            ("The new poster uses the brand palette.", ["poster"]),
                            ("The poster and the flyer are ready.", ["poster"]),
                            ("The screenshots of the job page are ready.", ["screenshots"]),
                            # Picture verbs, modifiers, and labels.
                            ("The final poster now reads Grand opening on Nov 20.", ["poster"]),
                            ("The poster calls out the launch date in red.", ["poster"]),
                            ("The new poster holds the brand palette and logo.", ["poster"]),
                            ("Two posters ran in the newsletter.", ["posters"]),
                            ("The screenshot step 3 shows the error.", ["screenshot"]),
                            ("The headshot and config.json are updated.", ["headshot"]),
                            ("Here are the three screenshots.", ["screenshots"])):
            with self.subTest(text=text):
                self.assertEqual(review_surface.missing_image_links(text), found)

    def test_evaluate_carries_compare_missing_and_never_blocks(self):
        v = review_surface.evaluate(["Done: /x/a.png and /x/b.png"], status="done")
        self.assertTrue(v.ok)
        self.assertEqual(json.loads(v.to_json())["compare_missing"], ["a.png", "b.png"])
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "review_surface.py"), "check", "--status", "done",
                            "--text", "Done: /x/a.png and /x/b.png"], capture_output=True, text=True, env=os.environ)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[review-surface] WARNING (compare_missing)", r.stderr)


class DefaultOpener(unittest.TestCase):
    """switchboard Kit: the page opens with each OS's own opener, and a machine with no
    way to open it (a headless Linux box) prints the path instead of failing."""

    def test_each_platform(self) -> None:
        from unittest import mock
        page = pathlib.Path("/tmp/page.html")
        with mock.patch.object(image_compare.sys, "platform", "darwin"):
            # Background open: never steal focus.
            self.assertEqual(image_compare._default_opener(page), ["open", "-g", str(page)])
        with mock.patch.object(image_compare.sys, "platform", "linux"), \
             mock.patch.object(image_compare.os, "name", "posix"), \
             mock.patch.object(image_compare.shutil, "which", return_value="/usr/bin/xdg-open"):
            with mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
                self.assertEqual(image_compare._default_opener(page), ["/usr/bin/xdg-open", str(page)])
            env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertIsNone(image_compare._default_opener(page))
        with mock.patch.object(image_compare.sys, "platform", "win32"), \
             mock.patch.object(image_compare.os, "name", "nt"), \
             mock.patch.object(image_compare.os, "startfile", create=True):
            self.assertEqual(image_compare._default_opener(page), [])

    def test_windows_opens_without_activating(self) -> None:
        """Windows hands the page to its default app with SW_SHOWNOACTIVATE, and an
        older Python without show_cmd falls back to the plain default-app open."""
        from unittest import mock
        page = pathlib.Path("/tmp/page.html")
        calls: list[tuple[tuple, dict]] = []
        with mock.patch.object(image_compare.os, "startfile", create=True,
                               side_effect=lambda *a, **k: calls.append((a, k))):
            image_compare._startfile_background(page)
        self.assertEqual(calls, [((str(page),), {"show_cmd": image_compare.SW_SHOWNOACTIVATE})])

        calls.clear()

        def old_python(*a, **k):
            if k:
                raise TypeError("startfile() got an unexpected keyword argument 'show_cmd'")
            calls.append((a, k))
        with mock.patch.object(image_compare.os, "startfile", create=True, side_effect=old_python):
            image_compare._startfile_background(page)
        self.assertEqual(calls, [((str(page),), {})])

    def test_open_page_runs_background_open_on_macos(self) -> None:
        """The real open path (no SWITCHBOARD_OPEN_CMD) runs `open -g <page>`."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            page = root / "compare.html"
            page.write_text("<html></html>")
            env = {k: v for k, v in os.environ.items() if k not in ("SWITCHBOARD_OPEN_CMD", "IMAGE_COMPARE_OPEN_DISABLED")}
            ran: list[list[str]] = []
            real_run = subprocess.run

            def fake_run(cmd, **k):
                if cmd and cmd[0] == "open":
                    ran.append(cmd)
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return real_run(cmd, **k)
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(image_compare.sys, "platform", "darwin"), \
                 mock.patch.object(review_surface, "resolve_local_path", side_effect=lambda p, *a: p), \
                 mock.patch.object(image_compare, "_browser_cold", return_value=None), \
                 mock.patch.object(image_compare, "_macos_html_handler", return_value="com.apple.Safari"), \
                 mock.patch.object(image_compare, "_frontmost_bundle", return_value=None), \
                 mock.patch.object(image_compare.subprocess, "run", side_effect=fake_run):
                out = image_compare.open_page(str(page), cwd=root)
        self.assertTrue(out["opened"], out)
        self.assertEqual(ran, [["open", "-g", str(page)]])

    def _mac_env(self) -> dict:
        return {k: v for k, v in os.environ.items()
                if k not in ("SWITCHBOARD_OPEN_CMD", "IMAGE_COMPARE_OPEN_DISABLED", "SWITCHBOARD_OPEN_VIA",
                             "SWITCHBOARD_OPEN_CHROME", "SWITCHBOARD_OPEN_WORKSPACE")}

    def test_chrome_default_browser_is_not_handed_the_page(self) -> None:
        """Chrome raises itself on every external open, -g or not. With no cmux, a Chrome-family default
        browser gets no page; the link stays. SWITCHBOARD_OPEN_CHROME=1 opens it."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            page = root / "compare.html"
            page.write_text("<html></html>")
            for handler in ("com.google.chrome", "com.brave.Browser", "company.thebrowser.Browser"):
                with mock.patch.dict(os.environ, self._mac_env(), clear=True), \
                     mock.patch.object(image_compare.sys, "platform", "darwin"), \
                     mock.patch.object(review_surface, "resolve_local_path", side_effect=lambda p, *a: p), \
                     mock.patch.object(image_compare, "_cmux_bin", return_value=None), \
                     mock.patch.object(image_compare, "_macos_html_handler", return_value=handler), \
                     mock.patch.object(image_compare, "_frontmost_bundle", return_value="com.tinyspeck.slackmacgap"), \
                     mock.patch.object(image_compare, "_log_open"), \
                     mock.patch.object(image_compare.subprocess, "run", side_effect=self._no_open_run()):
                    out = image_compare.open_page(str(page), cwd=root, force=True)
                self.assertFalse(out["opened"], handler)
                self.assertIn("raises itself", out["reason"])
                self.assertEqual(out["line"], f"Page: {page}")
            ran: list[list[str]] = []
            real_run = subprocess.run

            def run(cmd, **k):
                if cmd and cmd[0] == "open":
                    ran.append(cmd)
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return real_run(cmd, **k)
            with mock.patch.dict(os.environ, {**self._mac_env(), "SWITCHBOARD_OPEN_CHROME": "1"}, clear=True), \
                 mock.patch.object(image_compare.sys, "platform", "darwin"), \
                 mock.patch.object(review_surface, "resolve_local_path", side_effect=lambda p, *a: p), \
                 mock.patch.object(image_compare, "_cmux_bin", return_value=None), \
                 mock.patch.object(image_compare, "_macos_html_handler", return_value="com.google.chrome"), \
                 mock.patch.object(image_compare, "_browser_cold", return_value=None), \
                 mock.patch.object(image_compare, "_frontmost_bundle", return_value=None), \
                 mock.patch.object(image_compare, "_log_open"), \
                 mock.patch.object(image_compare, "_opened_state", return_value=root / "s.json"), \
                 mock.patch.object(image_compare.subprocess, "run", side_effect=run):
                self.assertTrue(image_compare.open_page(str(page), cwd=root, force=True)["opened"])
            self.assertEqual(ran, [["open", "-g", str(page)]])

    @posix_only
    def test_cmux_tab_opens_without_focus_and_inlines_images(self) -> None:
        """On macOS with cmux the page lands in an unselected cmux browser tab
        (no `open`, so no app is activated), with images inlined because
        cmux's webview reads only the page folder. A new version of the page
        reuses that tab; a gone tab gets a new one."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "shots").mkdir()
            (root / "shots" / "a.png").write_bytes(PNG)
            (root / "compare").mkdir()
            page = root / "compare" / "page.html"
            page.write_text('<html><img src="../shots/a.png" data-srcs="[&quot;../shots/a.png&quot;]" loading="lazy">'
                            '<img src="https://example.com/x.png"></html>')
            ran: list[list[str]] = []
            navigate_ok = {"v": True}

            def run(cmd, **k):
                ran.append(list(cmd))
                if cmd[0] == "open":
                    raise AssertionError("must not run macOS open")
                if cmd[1:3] == ["browser", "surface:7"]:
                    return subprocess.CompletedProcess(cmd, 0 if navigate_ok["v"] else 1, "", "")
                if cmd[1] == "new-surface":
                    return subprocess.CompletedProcess(cmd, 0, "OK surface:7 pane:1 workspace:9\n", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with mock.patch.dict(os.environ, {**self._mac_env(), "SWITCHBOARD_OPEN_WORKSPACE": "workspace:9"}, clear=True), \
                 mock.patch.object(image_compare.sys, "platform", "darwin"), \
                 mock.patch.object(review_surface, "resolve_local_path", side_effect=lambda p, *a: p), \
                 mock.patch.object(image_compare, "main_root", return_value=root), \
                 mock.patch.object(image_compare, "_cmux_bin", return_value="cmux"), \
                 mock.patch.object(image_compare, "_frontmost_bundle", return_value="com.tinyspeck.slackmacgap"), \
                 mock.patch.object(image_compare.subprocess, "run", side_effect=run):
                out = image_compare.open_page(str(page), cwd=root)
                self.assertTrue(out["opened"], out)
                self.assertEqual((out["via"], out["surface"], out["workspace"]), ("cmux", "surface:7", "workspace:9"))
                self.assertIn("background cmux tab", out["line"])
                new = [c for c in ran if c[1] == "new-surface"]
                self.assertEqual(len(new), 1)
                self.assertEqual(new[0][new[0].index("--focus") + 1], "false")
                self.assertEqual(new[0][new[0].index("--workspace") + 1], "workspace:9")
                view = pathlib.Path(new[0][new[0].index("--url") + 1][len("file://"):])
                self.assertEqual(view.parent, root / ".state" / "compare-view")
                text = view.read_text()
                self.assertIn('src="data:image/png;base64,', text)
                self.assertNotIn("../shots/a.png", text)
                self.assertNotIn('loading="lazy"', text)
                self.assertIn('src="https://example.com/x.png"', text)  # remote stays as is
                log = [json.loads(l) for l in (root / ".state" / "page-open.jsonl").read_text().splitlines()]
                self.assertEqual(log[-1]["via"], "cmux")
                self.assertEqual(log[-1]["front_before"], log[-1]["front_after"])
                # A new version reuses the tab.
                page.write_text(page.read_text() + "<p>v2</p>")
                ran.clear()
                out = image_compare.open_page(str(page), cwd=root)
                self.assertTrue(out["opened"])
                self.assertEqual([c[1:4] for c in ran if c[0] == "cmux"][0], ["browser", "surface:7", "navigate"])
                self.assertFalse([c for c in ran if c[1] == "new-surface"])
                # The tab was closed: a new one.
                navigate_ok["v"] = False
                ran.clear()
                self.assertTrue(image_compare.open_page(str(page), cwd=root, force=True)["opened"])
                self.assertEqual(len([c for c in ran if c[1] == "new-surface"]), 1)
                # An http(s) page goes to cmux too, never to `open`.
                ran.clear()
                out = image_compare.open_page("https://sites.example.com/owner-demo/", cwd=root, force=True)
                self.assertTrue(out["opened"])
                self.assertIn("https://sites.example.com/owner-demo/", [c for c in ran if c[0] == "cmux"][0])

    @staticmethod
    def _no_open_run():
        real_run = subprocess.run

        def run(cmd, **k):
            if cmd and cmd[0] == "open":
                raise AssertionError("must not open")
            return real_run(cmd, **k)
        return run

    def test_cold_browser_is_not_launched(self) -> None:
        """A page whose default browser is not running is not opened (launching
        can still steal focus); the line names the page instead."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            page = root / "compare.html"
            page.write_text("<html></html>")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SWITCHBOARD_OPEN_CMD", "IMAGE_COMPARE_OPEN_DISABLED", "SWITCHBOARD_OPEN_COLD_BROWSER")}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(image_compare.sys, "platform", "darwin"), \
                 mock.patch.object(review_surface, "resolve_local_path", side_effect=lambda p, *a: p), \
                 mock.patch.object(image_compare, "_macos_html_handler", return_value="com.apple.Safari"), \
                 mock.patch.object(image_compare, "_macos_app_running", return_value=False), \
                 mock.patch.object(image_compare.subprocess, "run", side_effect=self._no_open_run()):
                out = image_compare.open_page(str(page), cwd=root)
                self.assertFalse(out["opened"])
                self.assertIn("browser not running", out["reason"])
                self.assertEqual(out["line"], f"Page: {page}")
                # Running browser, or the explicit override, opens as usual.
                with mock.patch.object(image_compare, "_macos_app_running", return_value=True):
                    self.assertIsNone(image_compare._browser_cold(page))
                with mock.patch.dict(os.environ, {"SWITCHBOARD_OPEN_COLD_BROWSER": "1"}):
                    self.assertIsNone(image_compare._browser_cold(page))
                self.assertIsNone(image_compare._browser_cold(root / "image.png"))

    def test_focus_guard_hands_focus_back(self) -> None:
        """Chrome raises itself after `open -g`: the
        guard re-activates the app the owner was in, and leaves a deliberate switch."""
        from unittest import mock
        ran: list[list[str]] = []
        run = lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, b"", b"")
        with mock.patch.object(image_compare, "_frontmost_bundle",
                               side_effect=["com.tinyspeck.slackmacgap", "com.google.Chrome"]), \
             mock.patch.object(image_compare.subprocess, "run", side_effect=run), \
             mock.patch.object(image_compare.time, "sleep"):
            self.assertEqual(image_compare._guard_focus("com.tinyspeck.slackmacgap", "com.google.chrome"),
                             "com.tinyspeck.slackmacgap")
        self.assertEqual(ran, [["open", "-b", "com.tinyspeck.slackmacgap"]])
        ran.clear()
        # the owner moved to another app on purpose: no restore.
        with mock.patch.object(image_compare, "_frontmost_bundle", return_value="com.apple.finder"), \
             mock.patch.object(image_compare.subprocess, "run", side_effect=run):
            self.assertIsNone(image_compare._guard_focus("com.tinyspeck.slackmacgap", "com.google.chrome"))
        # the owner was already in the browser, or the front app is unknown: nothing to guard.
        self.assertIsNone(image_compare._guard_focus("com.google.Chrome", "com.google.chrome"))
        self.assertIsNone(image_compare._guard_focus(None, "com.google.chrome"))
        self.assertEqual(ran, [])
        # The browser never came forward: the guard times out quietly.
        with mock.patch.object(image_compare, "FOCUS_GUARD_SECONDS", 0.05), \
             mock.patch.object(image_compare, "_frontmost_bundle", return_value="com.tinyspeck.slackmacgap"), \
             mock.patch.object(image_compare.subprocess, "run", side_effect=run):
            self.assertIsNone(image_compare._guard_focus("com.tinyspeck.slackmacgap", "com.google.chrome"))
        self.assertEqual(ran, [])

    @posix_only
    def test_worktree_and_landed_copy_open_once(self) -> None:
        """A worker's worktree page and the landed main copy that report delivery
        opens are one page, so one open."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            main = pathlib.Path(tmp).resolve()
            subprocess.run(["git", "init", "-q", str(main)], check=True)
            rel = pathlib.Path("workstreams/ws/assets/compare/p.html")
            wt_page = main / ".amp/worktrees/rt-x" / rel
            main_page = main / rel
            for f in (wt_page, main_page):
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("<html>same</html>")
            rec = main / "rec.sh"
            rec.write_text("#!/bin/sh\necho \"$@\" >> \"$(dirname \"$0\")/opened.log\"\n")
            rec.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k != "IMAGE_COMPARE_OPEN_DISABLED"}
            env.update({"SWITCHBOARD_OPEN_CMD": str(rec), "SWITCHBOARD_VAULT_ROOT": str(main)})
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(review_surface, "resolve_local_path", side_effect=lambda p, *a: p):
                first = image_compare.open_page(str(wt_page), cwd=main)
                second = image_compare.open_page(str(main_page), cwd=main)
                main_page.write_text("<html>changed</html>")
                third = image_compare.open_page(str(main_page), cwd=main)
        self.assertTrue(first["opened"], first)
        self.assertEqual(second["reason"], "already opened")
        self.assertTrue(third["opened"], third)

    def test_no_opener_prints_the_path(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            page = pathlib.Path(tmp) / "compare.html"
            page.write_text("<html></html>")
            env = {k: v for k, v in os.environ.items() if k not in ("SWITCHBOARD_OPEN_CMD", "IMAGE_COMPARE_OPEN_DISABLED")}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(image_compare, "_default_opener", return_value=None), \
                 mock.patch.object(review_surface, "resolve_local_path", side_effect=lambda p, *a: p):
                out = image_compare.open_page(str(page))
        self.assertFalse(out["opened"])
        self.assertEqual(out["line"], f"Page: {page}")


def _no_xdg_open(cmd, *a, **k):
    """subprocess.run stand-in for a skip: git still runs, any xdg-open is a failure."""
    if cmd and str(cmd[0]).endswith(("xdg-open", "xdg-settings")):
        raise AssertionError(f"must not run {cmd[0]}")
    return subprocess.CompletedProcess(cmd, 0, "", "")


class OffMacOpener(unittest.TestCase):
    """Windows and Linux have no cmux tab, so the
    macOS-without-cmux rule holds there too. A Chromium-family default browser
    (Chrome, Edge, Brave) raises its window when handed a page, so it gets no
    page and the link stays; Linux under Wayland keeps xdg-open because the
    compositor refuses focus; SWITCHBOARD_OPEN_VIA=wmux opts into the wmux browser
    panel. Wrong impls: startfile to Edge (steals focus from the wmux composer);
    a URL folded by Path into `https:/…`; wmux used without the opt-in."""

    def _env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("SWITCHBOARD_OPEN_CMD", "IMAGE_COMPARE_OPEN_DISABLED", "SWITCHBOARD_OPEN_VIA",
                            "SWITCHBOARD_OPEN_CHROME", "WAYLAND_DISPLAY", "DISPLAY", "WSL_DISTRO_NAME")}
        env.update(extra)
        return env

    def _root(self, tmp: str) -> tuple[pathlib.Path, pathlib.Path]:
        root = pathlib.Path(tmp)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        page = root / "compare.html"
        page.write_text("<html></html>")
        return root, page

    def _patches(self, root: pathlib.Path, platform: str, osname: str):
        from unittest import mock
        return [mock.patch.object(image_compare.sys, "platform", platform),
                mock.patch.object(image_compare, "_is_windows", return_value=osname == "nt"),
                mock.patch.object(review_surface, "resolve_local_path", side_effect=lambda p, *a: p),
                mock.patch.object(image_compare, "_cmux_bin", return_value=None),
                mock.patch.object(image_compare, "_log_open"),
                mock.patch.object(image_compare, "_opened_state", return_value=root / "s.json")]

    def _run(self, patches, env, fn):
        from contextlib import ExitStack
        from unittest import mock
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            for p in patches:
                stack.enter_context(p)
            return fn()

    def test_windows_chromium_default_gets_no_page(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root, page = self._root(tmp)
            for progid in ("ChromeHTML", "MSEdgeHTM", "BraveHTML"):
                started: list = []
                patches = self._patches(root, "win32", "nt") + [
                    mock.patch.object(image_compare, "_windows_default_browser", return_value=progid),
                    mock.patch.object(image_compare.os, "startfile", create=True,
                                      side_effect=lambda *a, **k: started.append(a))]
                out = self._run(patches, self._env(), lambda: image_compare.open_page(str(page), cwd=root, force=True))
                self.assertFalse(out["opened"], progid)
                self.assertIn("raises itself", out["reason"])
                self.assertEqual(started, [])

    def test_windows_other_browser_and_chrome_optin_use_startfile(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root, page = self._root(tmp)
            for progid, env in (("FirefoxHTML-308046B0AF4A39CB", self._env()),
                                ("MSEdgeHTM", self._env(SWITCHBOARD_OPEN_CHROME="1"))):
                started: list = []
                patches = self._patches(root, "win32", "nt") + [
                    mock.patch.object(image_compare, "_windows_default_browser", return_value=progid),
                    mock.patch.object(image_compare.os, "startfile", create=True,
                                      side_effect=lambda *a, **k: started.append((a, k)))]
                out = self._run(patches, env, lambda: image_compare.open_page(str(page), cwd=root, force=True))
                self.assertTrue(out["opened"], (progid, out))
                self.assertEqual(started, [((str(page),), {"show_cmd": image_compare.SW_SHOWNOACTIVATE})])

    def test_windows_url_is_not_folded_by_path(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = self._root(tmp)
            started: list = []
            patches = self._patches(root, "win32", "nt") + [
                mock.patch.object(image_compare, "_windows_default_browser", return_value="FirefoxURL"),
                mock.patch.object(image_compare.os, "startfile", create=True,
                                  side_effect=lambda *a, **k: started.append(a[0]))]
            url = "https://example.com/report.html"
            out = self._run(patches, self._env(), lambda: image_compare.open_page(url, cwd=root, force=True))
            self.assertTrue(out["opened"], out)
            self.assertEqual(started, [url])

    def test_linux_x11_chromium_skipped_wayland_opens(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root, page = self._root(tmp)
            ran: list = []

            def run(cmd, **k):
                if cmd and cmd[0] == "/usr/bin/xdg-open":
                    ran.append(cmd)
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if cmd and cmd[0] == "/usr/bin/xdg-settings":
                    return subprocess.CompletedProcess(cmd, 0, "google-chrome.desktop\n", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            which = mock.patch.object(image_compare.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}")
            sp = mock.patch.object(image_compare.subprocess, "run", side_effect=run)
            x11 = self._run(self._patches(root, "linux", "posix") + [which, sp], self._env(DISPLAY=":0"),
                            lambda: image_compare.open_page(str(page), cwd=root, force=True))
            self.assertFalse(x11["opened"], x11)
            self.assertIn("google-chrome.desktop", x11["reason"])
            self.assertEqual(ran, [])
            which = mock.patch.object(image_compare.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}")
            sp = mock.patch.object(image_compare.subprocess, "run", side_effect=run)
            wl = self._run(self._patches(root, "linux", "posix") + [which, sp], self._env(WAYLAND_DISPLAY="wayland-0"),
                           lambda: image_compare.open_page(str(page), cwd=root, force=True))
            self.assertTrue(wl["opened"], wl)
            self.assertEqual(ran, [["/usr/bin/xdg-open", str(page)]])

    def test_file_url_keeps_a_windows_drive(self) -> None:
        # "file://C%3A/..." makes C: the host, so wmux loads nothing.
        self.assertEqual(image_compare.file_url(pathlib.Path("D:/kit check/x.html")), "file:///D:/kit%20check/x.html")
        self.assertEqual(image_compare.file_url(pathlib.Path("/tmp/a b.html")), "file:///tmp/a%20b.html")

    def test_unknown_default_browser_gets_no_page(self) -> None:
        # An unreadable default (fresh Windows profile, no xdg-settings) fails closed.
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root, page = self._root(tmp)
            patches = self._patches(root, "win32", "nt") + [
                mock.patch.object(image_compare, "_windows_default_browser", return_value=None),
                mock.patch.object(image_compare.os, "startfile", create=True,
                                  side_effect=AssertionError("must not startfile"))]
            out = self._run(patches, self._env(), lambda: image_compare.open_page(str(page), cwd=root, force=True))
            self.assertFalse(out["opened"], out)
            self.assertIn("default browser unknown", out["reason"])
            which = mock.patch.object(image_compare.shutil, "which",
                                      side_effect=lambda n: None if n == "xdg-settings" else f"/usr/bin/{n}")
            sp = mock.patch.object(image_compare.subprocess, "run", wraps=_no_xdg_open)
            out = self._run(self._patches(root, "linux", "posix") + [which, sp], self._env(DISPLAY=":0"),
                            lambda: image_compare.open_page(str(page), cwd=root, force=True))
            self.assertFalse(out["opened"], out)
            self.assertIn("default browser unknown", out["reason"])

    def test_linux_flatpak_ids_and_wsl(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root, page = self._root(tmp)
            for desktop, opened in (("com.brave.Browser.desktop", False), ("org.chromium.Chromium.desktop", False),
                                    ("org.mozilla.firefox.desktop", True)):
                def run(cmd, **k):
                    out = desktop + "\n" if cmd and cmd[0].endswith("xdg-settings") else ""
                    return subprocess.CompletedProcess(cmd, 0, out, "")
                which = mock.patch.object(image_compare.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}")
                sp = mock.patch.object(image_compare.subprocess, "run", side_effect=run)
                out = self._run(self._patches(root, "linux", "posix") + [which, sp], self._env(DISPLAY=":0"),
                                lambda: image_compare.open_page(str(page), cwd=root, force=True))
                self.assertEqual(out["opened"], opened, (desktop, out))
            # WSLg sets WAYLAND_DISPLAY, but xdg-open there hands the page to the Windows browser.
            which = mock.patch.object(image_compare.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}")
            sp = mock.patch.object(image_compare.subprocess, "run", wraps=_no_xdg_open)
            out = self._run(self._patches(root, "linux", "posix") + [which, sp],
                            self._env(WAYLAND_DISPLAY="wayland-0", WSL_DISTRO_NAME="Ubuntu"),
                            lambda: image_compare.open_page(str(page), cwd=root, force=True))
            self.assertFalse(out["opened"], out)
            self.assertIn("WSL", out["reason"])

    def test_wmux_browser_panel_is_opt_in(self) -> None:
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root, page = self._root(tmp)
            calls: list = []

            def run(cmd, **k):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "ok", "")
            from lib import mux
            patches = self._patches(root, "win32", "nt") + [
                mock.patch.object(mux, "wmux_cli", return_value=(["node", "wmux.js"], {})),
                mock.patch.object(image_compare.subprocess, "run", side_effect=run),
                mock.patch.object(image_compare.os, "startfile", create=True,
                                  side_effect=AssertionError("must not startfile"))]
            out = self._run(patches, self._env(SWITCHBOARD_OPEN_VIA="wmux"),
                            lambda: image_compare.open_page(str(page), cwd=root, force=True))
            self.assertTrue(out["opened"], out)
            self.assertEqual(out["via"], "wmux")
            self.assertIn("wmux browser panel", out["line"])
            self.assertEqual(calls[-1][:4], ["node", "wmux.js", "browser", "open"])
            self.assertTrue(calls[-1][4].startswith("file://"))


if __name__ == "__main__":
    unittest.main()
