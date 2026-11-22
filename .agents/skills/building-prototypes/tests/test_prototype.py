"""Tests for scripts/prototype.py. Run from the skill folder:

    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

import prototype  # noqa: E402


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            code = prototype.main(list(argv))
    except SystemExit as exc:  # CliError paths exit via sys.exit
        code = int(exc.code or 0)
    return code, out.getvalue()


FILLED = {
    "Problem": "Leads cannot see coverage. Quote: \"I keep a paper list.\"",
    "Who it is for": "A shift lead at one location on a laptop and a phone.",
    "Goals": "Read coverage in ten seconds. Uncovered shifts stand out.",
    "Non-goals": "No editing, no notifications.",
    "What we want to learn": "Leads find the uncovered row within 10 seconds unaided.",
    "Success metrics": "Primary: uncovered shifts filled within 24h, baseline unknown, target 80%. Guardrail: page load under 2s.",
}


def fill_discovery(root: Path, criteria: list[str] | None = None) -> None:
    path = root / "DISCOVERY.md"
    text = path.read_text()
    for name, body in FILLED.items():
        text = re.sub(rf"(## {re.escape(name)}\n\n(?:<!--[^\n]*-->\n)?)<[^>]*>", lambda m, b=body: m.group(1) + b, text, flags=re.S)
    rows = criteria if criteria is not None else [
        "At 1440 wide the tiles and table fit without scrolling",
        "At 390 wide the tiles stack with no horizontal scroll",
        "Tab reaches the filter, each row, and the feedback links",
    ]
    text = re.sub(
        r"(## Acceptance criteria\n\n(?:<!--[^\n]*-->\n)?)(?:- \[ \] [^\n]*\n)+",
        lambda m: m.group(1) + "".join(f"- [ ] {r}\n" for r in rows),
        text,
    )
    path.write_text(text)


class PrototypeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        code, out = run("init", "demo", "--title", "Demo Board", "--owner", "Owner",
                        "--feedback-link", "Email=mailto:o@example.com", "--dir", str(self.base / "demo"))
        self.assertEqual(code, 0, out)
        self.root = self.base / "demo"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def manifest(self) -> dict:
        return json.loads((self.root / "prototype.json").read_text())

    def test_init_scaffolds_every_document(self) -> None:
        for rel in ("prototype.json", "DISCOVERY.md", "RESEARCH.md", "CHANGELOG.md", "DEMO.md", "DECISION.md",
                    "README.md", "fixtures/data.json", "fixtures/README.md", "versions/v1/index.html"):
            self.assertTrue((self.root / rel).exists(), rel)
        html = (self.root / "versions" / "v1" / "index.html").read_text()
        self.assertIn('data-prototype-version="v1"', html)
        self.assertIn('id="feedback"', html)
        self.assertIn("mailto:o@example.com", html)
        self.assertEqual(self.manifest()["stage"], "discover")

    def test_gate_fails_on_long_placeholders_and_passes_when_filled(self) -> None:
        code, out = run("gate", "--dir", str(self.root))
        self.assertEqual(code, 1)
        # The Problem placeholder is longer than 80 characters; it must still be caught.
        self.assertIn("## Problem", out)
        self.assertIn("## Acceptance criteria", out)

        fill_discovery(self.root)
        code, out = run("gate", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertFalse(self.manifest()["gate"]["passed"], "gate passes only with --approve")

        code, out = run("gate", "--approve", "--by", "Owner", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertTrue(self.manifest()["gate"]["passed"])

    def test_gate_ignores_html_tags_and_urls(self) -> None:
        fill_discovery(self.root)
        path = self.root / "DISCOVERY.md"
        path.write_text(path.read_text().replace(
            "No editing, no notifications.",
            "No editing. See <https://example.com/spec> and the <img> rule.",
        ))
        code, out = run("gate", "--dir", str(self.root))
        self.assertEqual(code, 0, out)

    def test_version_and_fork_refuse_before_gate(self) -> None:
        code, out = run("version", "--note", "x", "--dir", str(self.root))
        self.assertNotEqual(code, 0)
        self.assertIn("gate", out)
        self.assertFalse((self.root / "versions" / "v2").exists())
        code, out = run("fork", "alt", "--hypothesis", "h", "--dir", str(self.root))
        self.assertNotEqual(code, 0)
        self.assertFalse((self.root / "variants" / "alt").exists())

    def test_version_fork_compare_and_changelog(self) -> None:
        fill_discovery(self.root)
        run("gate", "--approve", "--by", "Owner", "--dir", str(self.root))
        code, out = run("version", "--note", "Second pass", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        v2 = (self.root / "versions" / "v2" / "index.html").read_text()
        self.assertIn('data-prototype-version="v2"', v2)
        self.assertEqual(self.manifest()["current_version"], 2)

        code, out = run("fork", "dense", "--hypothesis", "Denser wins", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / "variants" / "dense" / "index.html").exists())
        self.assertEqual(self.manifest()["variants"][0]["name"], "dense")

        log = (self.root / "CHANGELOG.md").read_text()
        self.assertLess(log.index("variant dense"), log.index("| v2 |"), "newest row first")
        self.assertIn("Second pass", log)

        code, out = run("compare", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        compare = (self.root / "compare.html").read_text()
        for src in ("versions/v1/index.html", "versions/v2/index.html", "variants/dense/index.html"):
            self.assertIn(src, compare)

    def test_criteria_keeps_verification_notes(self) -> None:
        fill_discovery(self.root)
        code, out = run("criteria", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        matrix = self.root / "reviews" / "acceptance-matrix.md"
        text = matrix.read_text()
        self.assertEqual(text.count("| open |"), 3)
        text = text.replace(
            "| At 1440 wide the tiles and table fit without scrolling |  | open |  |",
            "| At 1440 wide the tiles and table fit without scrolling | screenshot | pass | qa/v1-1440.png |",
        )
        matrix.write_text(text)
        fill_discovery(self.root, criteria=[
            "At 1440 wide the tiles and table fit without scrolling",
            "A brand new criterion",
        ])
        run("criteria", "--dir", str(self.root))
        text = matrix.read_text()
        self.assertIn("| screenshot | pass | qa/v1-1440.png |", text)
        self.assertIn("| A brand new criterion |  | open |  |", text)

    def test_check_flags_contract_violations(self) -> None:
        code, out = run("check", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        index = self.root / "versions" / "v1" / "index.html"
        html = index.read_text()
        html = html.replace("data-prototype-banner", "data-x")
        html = html.replace("</head>", '<script src="https://cdn.example.com/a.js"></script></head>')
        index.write_text(html)
        code, out = run("check", "--dir", str(self.root))
        self.assertNotEqual(code, 0)
        self.assertIn("banner", out)
        self.assertIn("cdn.example.com", out)

    def test_init_inlines_design_baseline_tokens(self) -> None:
        html = (self.root / "versions" / "v1" / "index.html").read_text()
        self.assertNotIn("{{tokens}}", html)
        self.assertIn("--text-sm:", html)
        self.assertIn("--space-4:", html)
        self.assertIn(":focus-visible", html)
        self.assertIn(".btn:hover", html)
        # The scaffold itself passes the visual-quality check with no warnings.
        self.assertEqual(prototype.visual_quality(html), [])
        code, out = run("check", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertNotIn("visual:", out)

    def test_check_warns_on_a_page_off_the_design_baseline(self) -> None:
        index = self.root / "versions" / "v1" / "index.html"
        html = index.read_text()
        plain_css = (
            "body{font:15px/1.4 sans-serif;margin:0}"
            + "".join(f".s{i}{{font-size:{10 + i}px;padding:{3 + i}px {5 + i}px;margin:{7 + i}px}}" for i in range(10))
        )
        html = re.sub(r"<style>.*?</style>", f"<style>{plain_css}</style>", html, flags=re.S)
        index.write_text(html)
        warns = prototype.visual_quality(html)
        self.assertTrue(any("no type scale tokens" in w for w in warns), warns)
        self.assertTrue(any("no spacing tokens" in w for w in warns), warns)
        self.assertTrue(any("distinct px font sizes" in w for w in warns), warns)
        self.assertTrue(any("padding/margin/gap" in w for w in warns), warns)
        self.assertTrue(any(":hover" in w for w in warns), warns)
        self.assertTrue(any(":focus-visible" in w for w in warns), warns)
        # Visual drift is a warning, not a failure: check still exits 0 and prints it.
        code, out = run("check", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn("visual: no type scale tokens", out)

    def test_visual_quality_ignores_root_token_values_and_hairlines(self) -> None:
        # Token declarations in :root and 0/1/2 px hairlines are not literal drift.
        css = (
            ":root{--text-xs:12px;--text-sm:14px;--text-lg:18px;--text-xl:20px;--text-2xl:24px;"
            "--text-3xl:30px;--text-4xl:36px;--text-5xl:48px;--text-6xl:60px;--space-1:4px}"
            ".a{font-size:var(--text-sm);padding:1px 2px;margin:0;gap:var(--space-1)}"
            ".a:hover{color:red}.a:focus-visible{outline:0}"
        )
        self.assertEqual(prototype.visual_quality(f"<style>{css}</style>"), [])

    def test_export_rewrites_fixture_paths_for_both_adapters(self) -> None:
        fill_discovery(self.root)
        run("gate", "--approve", "--by", "Owner", "--dir", str(self.root))
        for adapter in ("static", "local"):
            out_dir = self.base / f"dist-{adapter}"
            code, out = run("export", "--adapter", adapter, "--out", str(out_dir), "--dir", str(self.root))
            self.assertEqual(code, 0, out)
            html = (out_dir / "index.html").read_text()
            self.assertNotIn("../../fixtures/", html)
            self.assertIn('"fixtures/data.json"', html)
            self.assertTrue((out_dir / "fixtures" / "data.json").exists())
            meta = json.loads((out_dir / "PROTOTYPE.json").read_text())
            self.assertEqual(meta["adapter"], adapter)
            self.assertEqual(meta["source"], "versions/v1")
        self.assertEqual(self.manifest()["stage"], "share")

    def test_export_rejects_unknown_adapter(self) -> None:
        code, out = run("export", "--adapter", "examplehost", "--dir", str(self.root))
        self.assertNotEqual(code, 0)
        self.assertIn("deploy-adapters.md", out)

    def test_find_browser_env_override_wins(self) -> None:
        import os

        with unittest.mock.patch.dict(os.environ, {"PROTOTYPE_BROWSER": "/nonexistent/chrome"}):
            self.assertEqual(prototype.find_browser(), "/nonexistent/chrome")

    def test_crop_png_keeps_top_left_pixels(self) -> None:
        import struct
        import zlib

        # 4x3 RGBA image, every pixel encodes its own (x, y) so the crop is checkable.
        width, height = 4, 3
        rows = bytearray()
        prev = bytes(width * 4)
        for y in range(height):
            rows.append(4)  # Paeth filter, so the unfilter path is exercised
            line = bytes(b for x in range(width) for b in (x, y, 7, 255))
            filtered = bytearray()
            for i, byte in enumerate(line):
                a = line[i - 4] if i >= 4 else 0
                b = prev[i]
                c = prev[i - 4] if i >= 4 else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                filtered.append((byte - pred) & 255)
            rows += filtered
            prev = line

        def chunk(kind: bytes, body: bytes) -> bytes:
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows)))
            + chunk(b"IEND", b"")
        )
        path = self.root / "probe.png"
        path.write_bytes(png)
        prototype.crop_png(path, 2, 2)
        data = path.read_bytes()
        self.assertEqual(struct.unpack(">II", data[16:24]), (2, 2))
        idat = data[data.index(b"IDAT") + 4:]
        raw = zlib.decompress(idat[: struct.unpack(">I", data[data.index(b"IDAT") - 4:data.index(b"IDAT")])[0]])
        # filter byte 0, then pixels (0,0) (1,0) / (0,1) (1,1)
        self.assertEqual(raw, bytes([0, 0, 0, 7, 255, 1, 0, 7, 255, 0, 0, 1, 7, 255, 1, 1, 7, 255]))

    def test_scaffold_has_metrics_and_system_notes_sections(self) -> None:
        discovery = (self.root / "DISCOVERY.md").read_text()
        research = (self.root / "RESEARCH.md").read_text()
        self.assertIn("## Success metrics", discovery)
        self.assertIn("## System notes: data, AI, latency, failure modes", research)
        # A filled doc that skips Success metrics must not pass the gate.
        text = discovery
        for name, body in FILLED.items():
            if name == "Success metrics":
                continue
            text = re.sub(rf"(## {re.escape(name)}\n\n(?:<!--[^\n]*-->\n)?)<[^>]*>", lambda m, b=body: m.group(1) + b, text, flags=re.S)
        (self.root / "DISCOVERY.md").write_text(text)
        code, out = run("gate", "--dir", str(self.root))
        self.assertNotEqual(code, 0)
        self.assertIn("Success metrics", out)

    def test_init_brief_copies_file_and_links_manifest(self) -> None:
        brief = self.base / "exercise.md"
        brief.write_text("# Exercise\n\nRedesign the cancel flow.\n")
        code, out = run("init", "brieftest", "--owner", "Owner", "--brief", str(brief),
                        "--dir", str(self.base / "brieftest"))
        self.assertEqual(code, 0, out)
        root = self.base / "brieftest"
        self.assertEqual((root / "BRIEF.md").read_text(), brief.read_text())
        manifest = json.loads((root / "prototype.json").read_text())
        self.assertEqual(manifest["links"]["brief"], "BRIEF.md")
        self.assertEqual(manifest["states"], [])
        code, out = run("init", "nobrief", "--brief", str(self.base / "missing.md"), "--dir", str(self.base / "nobrief"))
        self.assertNotEqual(code, 0)
        self.assertIn("not a file", out)

    def test_capture_states_cli_wins_over_manifest(self) -> None:
        manifest = {"states": [{"name": "offer", "query": "step=offer"}, {"name": "done", "query": "?step=done"}]}
        states = prototype.capture_states(manifest, None, False)
        self.assertEqual(states, [
            {"name": "", "query": ""},
            {"name": "offer", "query": "?step=offer"},
            {"name": "done", "query": "?step=done"},
        ])
        self.assertEqual(prototype.capture_states(manifest, None, True), [{"name": "", "query": ""}])
        cli = prototype.capture_states(manifest, ["why-open=persona=dev&why=open"], False)
        self.assertEqual(cli, [{"name": "why-open", "query": "?persona=dev&why=open"}])
        with self.assertRaises(prototype.CliError):
            prototype.capture_states({"states": [{"name": "Bad:Name", "query": "?x"}]}, None, False)
        # A bare name reshoots one declared state; an undeclared bare name fails and lists what exists.
        self.assertEqual(prototype.capture_states(manifest, ["done"], False), [{"name": "done", "query": "?step=done"}])
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(prototype.CliError):
            prototype.parse_state("noquery", manifest)
        self.assertIn("offer, done", err.getvalue())
        with self.assertRaises(prototype.CliError):
            prototype.parse_state("noquery")
        self.assertEqual(prototype.target_label(Path("/p/versions/v2")), "v2")
        self.assertEqual(prototype.target_label(Path("/p/variants/dense")), "variants-dense")

    def test_compare_state_opens_every_frame_at_the_query(self) -> None:
        fill_discovery(self.root)
        run("gate", "--approve", "--by", "Owner", "--dir", str(self.root))
        run("fork", "dense", "--hypothesis", "Denser wins", "--dir", str(self.root))
        path = self.root / "prototype.json"
        manifest = json.loads(path.read_text())
        manifest["states"] = [{"name": "offer", "query": "?persona=maya&step=offer"}]
        path.write_text(json.dumps(manifest))
        code, out = run("compare", "--state", "offer", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        compare = (self.root / "compare.html").read_text()
        self.assertIn('src="versions/v1/index.html?persona=maya&step=offer"', compare)
        self.assertIn('src="variants/dense/index.html?persona=maya&step=offer"', compare)
        code, out = run("compare", "--state", "step=done", "--out", "compare-done.html", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn('index.html?step=done"', (self.root / "compare-done.html").read_text())
        # the default page is untouched, so one page per state can coexist
        self.assertIn('index.html?persona=maya&step=offer"', compare)
        self.assertEqual(compare, (self.root / "compare.html").read_text())
        code, out = run("compare", "--out", "../escape.html", "--dir", str(self.root))
        self.assertNotEqual(code, 0)
        self.assertIn("inside the prototype directory", out)

    def test_status_reports_states_and_matrix_counts(self) -> None:
        fill_discovery(self.root)
        code, out = run("status", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn("capture states: default only", out)
        self.assertIn("acceptance matrix: not generated", out)
        run("criteria", "--dir", str(self.root))
        matrix = self.root / "reviews" / "acceptance-matrix.md"
        matrix.write_text(matrix.read_text().replace("| open |", "| pass |", 1))
        path = self.root / "prototype.json"
        manifest = json.loads(path.read_text())
        manifest["states"] = [{"name": "offer", "query": "?step=offer"}]
        path.write_text(json.dumps(manifest))
        code, out = run("status", "--dir", str(self.root))
        self.assertIn("capture states: offer", out)
        self.assertIn("acceptance matrix: 1 pass, 0 fail, 2 open", out)

    def test_decide_requires_a_filled_decision_doc_then_sets_the_stage(self) -> None:
        fill_discovery(self.root)
        decision = self.root / "DECISION.md"
        # scaffold still has the `archive | promote | iterate` line and placeholders
        code, out = run("decide", "archive", "--dir", str(self.root))
        self.assertNotEqual(code, 0)
        self.assertIn("- Decision: archive", out)
        text = decision.read_text().replace("- Decision: archive | promote | iterate", "- Decision: archive")
        decision.write_text(text)
        code, out = run("decide", "archive", "--dir", str(self.root))
        self.assertNotEqual(code, 0)
        self.assertIn("What we learned", out)
        text = text.replace(
            "<One bullet per hypothesis in DISCOVERY.md: confirmed, refuted, or unclear, with the evidence (session log, quotes, captures).>",
            "- **H1 unclear.** No session yet; captures show the offer renders.",
        ).replace("<date decided>", "2026-10-29").replace("<owner>", "Owner").replace("v<N> (variants compared: <names or none>)", "v1 (variants compared: none)")
        decision.write_text(text)
        # the outcome must match the document
        code, out = run("decide", "promote", "--dir", str(self.root))
        self.assertNotEqual(code, 0)
        self.assertIn("- Decision: promote", out)
        code, out = run("decide", "archive", "--by", "Owner", "--dir", str(self.root))
        self.assertEqual(code, 0, out)
        manifest = json.loads((self.root / "prototype.json").read_text())
        self.assertEqual(manifest["stage"], "decide")
        self.assertEqual(manifest["decision"]["outcome"], "archive")
        self.assertEqual(manifest["decision"]["by"], "Owner")
        code, out = run("status", "--dir", str(self.root))
        self.assertIn("stage: decide (archive)", out)

    def test_serve_reports_busy_port_and_port_zero_picks_a_free_one(self) -> None:
        import socket
        import threading

        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy = holder.getsockname()[1]
        try:
            code, out = run("serve", "--dir", str(self.root), "--port", str(busy))
        finally:
            holder.close()
        self.assertEqual(code, 2, out)
        self.assertIn(f"port {busy} is already in use", out)
        self.assertIn("--port 0", out)
        self.assertNotIn("Traceback", out)

        out = io.StringIO()
        started = threading.Event()
        real_server = prototype.PrototypeServer
        servers: list = []

        class Capturing(real_server):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                servers.append(self)

            def serve_forever(self, *a, **kw):  # noqa: D401
                started.set()
                raise KeyboardInterrupt

        with unittest.mock.patch.object(prototype, "PrototypeServer", Capturing), \
                redirect_stdout(out), redirect_stderr(out):
            code = prototype.main(["serve", "--dir", str(self.root), "--port", "0"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(started.is_set())
        port = servers[0].server_address[1]
        self.assertNotEqual(port, 0)
        self.assertIn(f"http://127.0.0.1:{port}/", out.getvalue())

    @contextlib.contextmanager
    def _served(self, root: Path):
        """A real server for ``root`` on a free port, running on a thread."""
        import threading

        httpd = prototype.make_server(root, 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd.server_address[1]
        finally:
            httpd.shutdown()
            httpd.server_close()

    @staticmethod
    def _get(port: int, path: str, timeout: float = 5.0):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_serve_reads_from_dir_not_cwd_and_survives_a_deleted_cwd(self) -> None:
        import os

        (self.root / "versions" / "v1").mkdir(parents=True, exist_ok=True)
        (self.root / "versions" / "v1" / "index.html").write_text("<p>v1 body</p>", encoding="utf-8")
        original = os.getcwd()
        scratch = Path(tempfile.mkdtemp())
        try:
            os.chdir(scratch)
            scratch.rmdir()  # the shell's cwd is gone, as after a dropped checkout
            with self._served(self.root) as port:
                status, body = self._get(port, "/versions/v1/index.html")
        finally:
            os.chdir(original)
        self.assertEqual(status, 200)
        self.assertIn(b"v1 body", body)

    def test_serve_answers_503_with_the_path_when_the_root_disappears(self) -> None:
        import shutil

        root = Path(tempfile.mkdtemp()) / "gone-proto"
        shutil.copytree(self.root, root)
        err = io.StringIO()
        with self._served(root) as port, redirect_stderr(err):
            status, _ = self._get(port, "/prototype.json")
            self.assertEqual(status, 200)
            shutil.rmtree(root)
            status, body = self._get(port, "/prototype.json")
            status2, _ = self._get(port, "/versions/v1/index.html")
        self.assertEqual(status, 503)
        self.assertEqual(status2, 503)
        self.assertIn(str(root).encode(), body)
        self.assertIn(b"no longer exists", body)
        self.assertEqual(err.getvalue().count("no longer exists"), 1, err.getvalue())

    def test_serve_is_not_blocked_by_a_client_that_never_sends_a_request(self) -> None:
        import socket

        with self._served(self.root) as port:
            stalled = socket.create_connection(("127.0.0.1", port))
            try:
                status, _ = self._get(port, "/prototype.json", timeout=5.0)
            finally:
                stalled.close()
        self.assertEqual(status, 200)

    def test_core_never_names_a_host(self) -> None:
        source = (SKILL / "scripts" / "prototype.py").read_text().lower()
        skill_md = (SKILL / "SKILL.md").read_text().lower()
        for word in ("examplehost", "internal-example", "intranet.example"):
            self.assertNotIn(word, source, f"{word} in prototype.py")
            self.assertNotIn(word, skill_md, f"{word} in SKILL.md")


if __name__ == "__main__":
    unittest.main()
