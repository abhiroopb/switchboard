#!/usr/bin/env python3
"""Prototype lifecycle CLI for the building-prototypes skill.

Stdlib only. Works on any machine with Python 3.9+ and a browser. The
prototype directory is self-contained: a manifest, the discovery doc, a
changelog, fixtures, numbered versions, and named variants.

Commands
  init <slug>            scaffold a prototype directory
  gate [--approve]       check the discovery doc; --approve records the pass
  criteria               refresh reviews/acceptance-matrix.md from DISCOVERY.md
  version --note TEXT    snapshot the current version as the next vN
  fork <name> [--from]   copy a version into variants/<name>
  compare [targets...]   write compare.html with the chosen versions/variants
  check [targets...]     static checks on each index.html
  serve [--port N]       serve the prototype directory locally
  capture [targets...]   screenshot each target at 1440 and 390 into qa/
  export --adapter NAME  write a flat dist/ folder for a static host
  status                 print the manifest summary

Run inside the prototype directory, or pass --dir.
"""
from __future__ import annotations

import argparse
import errno
import datetime as dt
import http.server
import json
import os
import re
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_ROOT / "reference" / "scaffold"
MANIFEST = "prototype.json"
DISCOVERY = "DISCOVERY.md"
CHANGELOG = "CHANGELOG.md"
CORE_ADAPTERS = ("local", "static")

# Discovery sections the gate requires. Each must have content and no
# unfilled `<placeholder>` tokens.
GATE_SECTIONS = (
    "Problem",
    "Who it is for",
    "Goals",
    "Non-goals",
    "What we want to learn",
    "Success metrics",
    "Acceptance criteria",
)

# A capture state is a name plus the query string that opens the prototype in
# that state (`?persona=maya&step=offer`). Names label the PNG, so they are
# restricted to characters that are safe in a file name.
STATE_NAME = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
BRIEF = "BRIEF.md"

# A placeholder is `<text>` whose first character is a capital letter or a
# space, so HTML tags (`<img>`, `<!-- -->`) and URLs (`<https://…>`) do not match.
PLACEHOLDER = re.compile(r"<[A-Z ][^<>\n]*>")


def now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def today() -> str:
    return dt.date.today().isoformat()


class CliError(SystemExit):
    def __init__(self, message: str, code: int = 2):
        print(f"error: {message}", file=sys.stderr)
        super().__init__(code)


# --- manifest ---------------------------------------------------------------

def load_manifest(root: Path) -> dict:
    path = root / MANIFEST
    if not path.exists():
        raise CliError(f"{path} not found; run `prototype.py init <slug>` first")
    return json.loads(path.read_text())


def save_manifest(root: Path, manifest: dict) -> None:
    manifest["updated"] = now_iso()
    (root / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")


def version_dir(root: Path, n: int) -> Path:
    return root / "versions" / f"v{n}"


def resolve_target(root: Path, name: str) -> Path:
    """Map `v2`, `variants/dark`, or `dark` to a directory holding index.html."""
    candidates = [root / name, root / "versions" / name, root / "variants" / name]
    for cand in candidates:
        if (cand / "index.html").exists():
            return cand
    raise CliError(f"no index.html under {name} (looked in versions/ and variants/)")


def all_targets(root: Path) -> list[Path]:
    out = []
    for sub in ("versions", "variants"):
        base = root / sub
        if base.exists():
            out.extend(sorted(p for p in base.iterdir() if (p / "index.html").exists()))
    return out


def target_label(target: Path) -> str:
    """`v2` for a version, `variants-<name>` for a variant; used in qa/ file names."""
    return target.name if target.parent.name == "versions" else f"{target.parent.name}-{target.name}"


def normalise_query(query: str) -> str:
    query = query.strip()
    if not query:
        return ""
    return query if query.startswith("?") else "?" + query


def manifest_states(manifest: dict) -> list[dict]:
    """The named states declared in prototype.json `states`, validated."""
    states: list[dict] = []
    for entry in manifest.get("states", []) or []:
        name = str(entry.get("name", "")).strip()
        if not STATE_NAME.fullmatch(name):
            raise CliError(f"prototype.json states entry has a bad name {name!r}; use kebab-case")
        states.append({"name": name, "query": normalise_query(str(entry.get("query", "")))})
    return states


def parse_state(token: str, manifest: dict | None = None) -> dict:
    """`NAME=QUERY` from the command line into {"name", "query"}.

    A bare `NAME` picks that state from prototype.json `states`, so a reshoot
    after a fix is `capture <target> --state maya-done` with no query retyped.
    """
    name, sep, query = token.partition("=")
    name = name.strip()
    if not STATE_NAME.fullmatch(name):
        raise CliError(f"--state needs NAME=QUERY with a kebab-case name, got {token!r}")
    if sep:
        return {"name": name, "query": normalise_query(query)}
    declared = {s["name"]: s for s in manifest_states(manifest or {})}
    if name in declared:
        return declared[name]
    known = ", ".join(declared) or "none declared"
    raise CliError(f"--state {name!r} is not in prototype.json `states` ({known}); pass NAME=QUERY to shoot an ad hoc state")


def capture_states(manifest: dict, cli_states: list[str] | None, default_only: bool) -> list[dict]:
    """The states a capture run shoots.

    `--state NAME=QUERY` (or a bare `--state NAME` from the manifest) on the
    command line wins. Otherwise the manifest's `states` list is used, always
    preceded by the default (no query) shot. An entry with an empty name is
    the default state.
    """
    if cli_states:
        return [parse_state(s, manifest) for s in cli_states]
    states: list[dict] = [{"name": "", "query": ""}]
    if default_only:
        return states
    states.extend(manifest_states(manifest))
    return states


# --- templates --------------------------------------------------------------

TOKENS_FILE = "tokens.css"


def design_tokens() -> str:
    """The kit's design baseline, inlined into every scaffolded index.html."""
    return (TEMPLATE_DIR / TOKENS_FILE).read_text().rstrip("\n")


def render(template_name: str, **vars: str) -> str:
    text = (TEMPLATE_DIR / template_name).read_text()
    if "{{tokens}}" in text and "tokens" not in vars:
        vars = {**vars, "tokens": design_tokens()}
    for key, value in vars.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def feedback_links_html(links: list[dict]) -> str:
    items = []
    for link in links:
        label = link.get("label", "").strip()
        url = link.get("url", "").strip()
        if label and url:
            items.append(f'      <li><a href="{url}">{label}</a></li>')
    if not items:
        items.append('      <li><a href="mailto:owner@example.com">Email the prototype owner</a></li>')
    return "\n".join(items)


# --- commands ---------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    slug = args.slug
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise CliError("slug must be lowercase kebab-case, for example `shift-dashboard`")
    root = Path(args.dir).resolve() if args.dir else Path.cwd() / slug
    if (root / MANIFEST).exists():
        raise CliError(f"{root} already holds a prototype")
    title = args.title or slug.replace("-", " ").title()
    links = []
    for raw in args.feedback_link or []:
        if "=" not in raw:
            raise CliError(f"--feedback-link needs Label=URL, got {raw!r}")
        label, url = raw.split("=", 1)
        links.append({"label": label.strip(), "url": url.strip()})

    brief = Path(args.brief).resolve() if args.brief else None
    if brief and not brief.is_file():
        raise CliError(f"--brief {brief} is not a file")

    for sub in ("versions/v1", "variants", "fixtures", "reviews", "qa"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    manifest = {
        "slug": slug,
        "title": title,
        "created": now_iso(),
        "updated": now_iso(),
        "owner": args.owner or "",
        "stage": "discover",
        "gate": {"passed": False, "passed_at": None},
        "current_version": 1,
        "versions": [{"n": 1, "created": now_iso(), "note": "scaffold"}],
        "variants": [],
        "adapter": "local",
        "feedback_links": links,
        # Named states `capture` and `compare` open: [{"name": "offer", "query": "?step=offer"}].
        "states": [],
        "links": {"discovery": DISCOVERY, "changelog": CHANGELOG, "demo": "DEMO.md", "decision": "DECISION.md"},
    }
    if brief:
        shutil.copyfile(brief, root / BRIEF)
        manifest["links"]["brief"] = BRIEF
    (root / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")

    common = dict(title=title, slug=slug, date=today(), owner=args.owner or "<owner>")
    (root / DISCOVERY).write_text(render("DISCOVERY.md", **common))
    (root / "RESEARCH.md").write_text(render("RESEARCH.md", **common))
    (root / CHANGELOG).write_text(render("CHANGELOG.md", **common))
    (root / "DEMO.md").write_text(render("DEMO.md", **common))
    (root / "DECISION.md").write_text(render("DECISION.md", **common))
    (root / "README.md").write_text(render("README.md", **common))
    (root / "fixtures" / "data.json").write_text(render("fixtures-data.json", **common))
    (root / "fixtures" / "README.md").write_text(render("fixtures-README.md", **common))
    (root / "versions" / "v1" / "index.html").write_text(
        render("index.html", version="v1", feedback_links=feedback_links_html(links), **common)
    )
    (root / ".gitignore").write_text("dist/\nqa/*.png\n")
    print(f"scaffolded {root}")
    print(f"next: fill {DISCOVERY}, then `prototype.py gate --dir {root}`")
    return 0


def section_bodies(markdown: str) -> dict[str, str]:
    """Return {heading text: body} for every `## ` heading."""
    out: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def gate_findings(root: Path) -> list[str]:
    path = root / DISCOVERY
    if not path.exists():
        return [f"{DISCOVERY} missing"]
    sections = section_bodies(path.read_text())
    problems = []
    for name in GATE_SECTIONS:
        body = sections.get(name)
        if body is None:
            problems.append(f"section `## {name}` missing")
            continue
        stripped = "\n".join(l for l in body.splitlines() if l.strip() and not l.strip().startswith("<!--"))
        if not stripped:
            problems.append(f"section `## {name}` is empty")
            continue
        leftovers = PLACEHOLDER.findall(stripped)
        if leftovers:
            problems.append(f"section `## {name}` still has placeholders: {', '.join(leftovers[:3])}")
    criteria = acceptance_criteria(root)
    if not criteria:
        problems.append("no `- [ ]` items under `## Acceptance criteria`")
    return problems


def acceptance_criteria(root: Path) -> list[str]:
    path = root / DISCOVERY
    if not path.exists():
        return []
    body = section_bodies(path.read_text()).get("Acceptance criteria", "")
    items = []
    for line in body.splitlines():
        m = re.match(r"\s*-\s*\[[ xX]\]\s*(.+)", line)
        if m and not PLACEHOLDER.search(m.group(1)):
            items.append(m.group(1).strip())
    return items


def cmd_gate(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    manifest = load_manifest(root)
    problems = gate_findings(root)
    if problems:
        print("gate: NOT passed")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"gate: discovery complete ({len(acceptance_criteria(root))} acceptance criteria)")
    if args.approve:
        manifest["gate"] = {"passed": True, "passed_at": now_iso(), "approved_by": args.by or "owner"}
        manifest["stage"] = "build"
        save_manifest(root, manifest)
        print("gate: recorded as passed; build may start")
    else:
        print("add --approve once the owner has agreed to the discovery doc")
    return 0


def cmd_criteria(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    load_manifest(root)
    criteria = acceptance_criteria(root)
    if not criteria:
        raise CliError("no acceptance criteria found in DISCOVERY.md")
    path = root / "reviews" / "acceptance-matrix.md"
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("| ") and not line.startswith("| Criterion") and not line.startswith("|---"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if cells:
                    existing[cells[0]] = line
    lines = [
        "# Acceptance matrix",
        "",
        f"Source: `{DISCOVERY}` ## Acceptance criteria. Refreshed {today()} by `prototype.py criteria`.",
        "Fill `How verified` with the check you ran (browser step, test, review), `Result` with pass, fail, or open, and `Capture` with the qa/ file.",
        "",
        "| Criterion | How verified | Result | Capture |",
        "|---|---|---|---|",
    ]
    for c in criteria:
        lines.append(existing.get(c, f"| {c} |  | open |  |"))
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path} ({len(criteria)} criteria)")
    return 0


def require_gate(manifest: dict, allow_skip: bool) -> None:
    if manifest.get("gate", {}).get("passed"):
        return
    if allow_skip:
        print("warning: discovery gate not passed; continuing because --skip-gate was set")
        return
    raise CliError("discovery gate not passed; run `prototype.py gate --approve` or pass --skip-gate")


def prepend_changelog(root: Path, line: str) -> None:
    path = root / CHANGELOG
    text = path.read_text() if path.exists() else "# Changelog\n\n"
    marker = "<!-- entries -->"
    if marker in text:
        text = text.replace(marker, marker + "\n" + line, 1)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text)


def cmd_version(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    manifest = load_manifest(root)
    require_gate(manifest, args.skip_gate)
    current = int(manifest["current_version"])
    src = version_dir(root, current)
    if not (src / "index.html").exists():
        raise CliError(f"{src}/index.html missing; nothing to snapshot")
    nxt = current + 1
    dst = version_dir(root, nxt)
    if dst.exists():
        raise CliError(f"{dst} already exists")
    shutil.copytree(src, dst)
    stamp_version(dst / "index.html", f"v{nxt}")
    manifest["current_version"] = nxt
    manifest["versions"].append({"n": nxt, "created": now_iso(), "note": args.note, "from": f"v{current}"})
    manifest["stage"] = "iterate"
    save_manifest(root, manifest)
    prepend_changelog(root, f"| {today()} | v{nxt} | {args.note} | from v{current} |")
    print(f"created versions/v{nxt} from v{current}; edit {dst / 'index.html'}")
    return 0


def stamp_version(index: Path, label: str) -> None:
    text = index.read_text()
    new = re.sub(r'(data-prototype-version=")[^"]*(")', rf"\g<1>{label}\2", text)
    new = re.sub(r"(<span class=\"proto-version\">)[^<]*(</span>)", rf"\g<1>{label}\2", new)
    if new != text:
        index.write_text(new)


def cmd_fork(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    manifest = load_manifest(root)
    require_gate(manifest, args.skip_gate)
    name = args.name
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise CliError("variant name must be lowercase kebab-case")
    src_name = args.from_target or f"v{manifest['current_version']}"
    src = resolve_target(root, src_name)
    dst = root / "variants" / name
    if dst.exists():
        raise CliError(f"variant {name} already exists")
    shutil.copytree(src, dst)
    stamp_version(dst / "index.html", f"{src.name} · variant {name}")
    manifest["variants"].append({"name": name, "from": src.name, "created": now_iso(), "hypothesis": args.hypothesis or ""})
    save_manifest(root, manifest)
    prepend_changelog(root, f"| {today()} | variant {name} | {args.hypothesis or 'fork'} | from {src.name} |")
    print(f"created variants/{name} from {src.name}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    manifest = load_manifest(root)
    targets = [resolve_target(root, t) for t in args.targets] if args.targets else all_targets(root)
    if len(targets) < 1:
        raise CliError("nothing to compare")
    query = ""
    if args.state:
        named = next((s for s in manifest.get("states", []) or [] if s.get("name") == args.state), None)
        query = normalise_query(named["query"] if named else args.state)
    frames = []
    for t in targets:
        rel = t.relative_to(root).as_posix()
        label = t.name if t.parent.name == "versions" else f"variant {t.name}"
        note = ""
        if t.parent.name == "versions":
            n = int(t.name[1:])
            note = next((v.get("note", "") for v in manifest["versions"] if v["n"] == n), "")
        else:
            note = next((v.get("hypothesis", "") for v in manifest["variants"] if v["name"] == t.name), "")
        frames.append(
            f'      <figure><figcaption><strong>{label}</strong> <span>{note}</span></figcaption>'
            f'<iframe src="{rel}/index.html{query}" title="{label}" loading="lazy"></iframe></figure>'
        )
    html = render("compare.html", title=manifest["title"], frames="\n".join(frames), date=today())
    out = root / (args.out or "compare.html")
    if out.suffix != ".html" or out.resolve().parent != root:
        raise CliError("--out must be an .html file name inside the prototype directory, for example compare-offer.html")
    out.write_text(html)
    state_note = f" at state {query}" if query else ""
    print(f"wrote {out} with {len(targets)} frame(s){state_note}; open it via `prototype.py serve`")
    return 0


def check_index(path: Path, manifest: dict) -> tuple[list[str], list[str]]:
    """Return (failures, warnings) for one index.html."""
    text = path.read_text()
    fails: list[str] = []
    warns: list[str] = []
    if not re.search(r"<title>\s*\S", text, re.I):
        fails.append("missing <title>")
    if 'name="viewport"' not in text:
        fails.append("missing viewport meta")
    if "data-prototype-banner" not in text:
        fails.append("missing prototype banner (data-prototype-banner) so viewers know this is a prototype")
    if 'id="feedback"' not in text:
        fails.append('missing feedback section (id="feedback")')
    if re.search(r"(transition|animation)\s*:", text) and "prefers-reduced-motion" not in text:
        fails.append("uses transition/animation without a prefers-reduced-motion rule")
    for m in re.finditer(r'<(?:script|link)[^>]+(?:src|href)="(https?://[^"]+)"', text, re.I):
        fails.append(f"external resource {m.group(1)}; prototypes are self-contained")
    for host in manifest.get("disallowed_hosts", []):
        if host in text:
            fails.append(f"references disallowed host {host}")
    for img in re.findall(r"<img[^>]*>", text, re.I):
        if "alt=" not in img:
            fails.append("an <img> has no alt attribute")
            break
    if "<main" not in text:
        warns.append("no <main> landmark")
    if "<h1" not in text:
        warns.append("no <h1>")
    if re.search(r"\b(lorem ipsum|TODO|FIXME)\b", text, re.I):
        warns.append("placeholder text (lorem ipsum, TODO, FIXME) still present")
    if "fixtures/" in text and "sample data" not in text.lower():
        warns.append("loads fixtures but does not label the data as sample data")
    warns.extend(visual_quality(text))
    return fails, warns


# Visual-quality thresholds. A page on the design baseline declares its type
# and spacing scales as custom properties and reaches for them instead of
# literal pixel values; a page that drifted back to ad-hoc CSS shows up as
# many distinct literals and no tokens.
MAX_FONT_SIZE_LITERALS = 8
MAX_SPACING_LITERALS = 12


def visual_quality(text: str) -> list[str]:
    """Return warnings when a page lacks a type scale, spacing tokens, or states.

    Applied by `check` and printed by `capture` before it shoots, so the
    author sees the drift next to the screenshots it caused.
    """
    warns: list[str] = []
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S | re.I))
    if not css:
        return warns
    if not re.search(r"--text-[a-z0-9]+\s*:", css):
        warns.append("visual: no type scale tokens (--text-*); start from reference/scaffold/tokens.css")
    if not re.search(r"--space-\d+\s*:", css):
        warns.append("visual: no spacing tokens (--space-*); start from reference/scaffold/tokens.css")
    # Literal px font sizes outside the :root token block.
    body_css = re.sub(r":root\s*\{[^}]*\}", "", css)
    sizes = set(re.findall(r"font-size\s*:\s*(\d+(?:\.\d+)?)px", body_css))
    sizes |= set(re.findall(r"font\s*:[^;]*?\b(\d+(?:\.\d+)?)px\s*(?:/|\s)", body_css))
    if len(sizes) > MAX_FONT_SIZE_LITERALS:
        warns.append(
            f"visual: {len(sizes)} distinct px font sizes in rules; use the type scale "
            f"(at most {MAX_FONT_SIZE_LITERALS} literals)"
        )
    spacing = set()
    for decl in re.findall(r"(?:padding|margin|gap|row-gap|column-gap)(?:-[a-z]+)?\s*:\s*([^;{}]+)", body_css):
        spacing |= {v for v in re.findall(r"(\d+(?:\.\d+)?)px", decl) if v not in {"0", "1", "2"}}
    if len(spacing) > MAX_SPACING_LITERALS:
        warns.append(
            f"visual: {len(spacing)} distinct px values in padding/margin/gap; use --space-* "
            f"(at most {MAX_SPACING_LITERALS} literals)"
        )
    if ":hover" not in css:
        warns.append("visual: no :hover state on any element")
    if ":focus-visible" not in css:
        warns.append("visual: no :focus-visible style")
    return warns


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    manifest = load_manifest(root)
    targets = [resolve_target(root, t) for t in args.targets] if args.targets else all_targets(root)
    if not manifest.get("gate", {}).get("passed"):
        print("warning: discovery gate not passed yet")
    total_fail = 0
    for t in targets:
        fails, warns = check_index(t / "index.html", manifest)
        rel = t.relative_to(root).as_posix()
        status = "FAIL" if fails else "ok"
        print(f"{rel}: {status}")
        for f in fails:
            print(f"  ✗ {f}")
        for w in warns:
            print(f"  ! {w}")
        total_fail += len(fails)
    return 1 if total_fail else 0


# Seconds a client may hold a connection open without sending a complete
# request before the server drops it. Each connection is handled on its own
# thread, so a stalled client (a browser tab that opened a socket and went
# idle, a half-closed proxy) never blocks the next request either way.
CLIENT_TIMEOUT = 15


class PrototypeServer(http.server.ThreadingHTTPServer):
    """Local static server bound to one prototype directory."""

    allow_reuse_address = True
    daemon_threads = True


def make_server(root: Path, port: int, verbose: bool = False) -> PrototypeServer:
    """Bind a static server to ``root`` on 127.0.0.1:``port`` (0 picks a free one).

    The handler reads files from ``root`` directly rather than from the
    process's working directory, so a deleted or moved cwd cannot break it.
    If ``root`` itself disappears (a scratch checkout removed while the server
    kept running) every request gets a 503 that names the missing path, not a
    reset connection with a traceback in the log.
    """
    warned = {"gone": False}

    class Handler(http.server.SimpleHTTPRequestHandler):
        timeout = CLIENT_TIMEOUT

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):  # noqa: D401
            if verbose:
                super().log_message(*a)

        def root_is_gone(self) -> bool:
            if root.is_dir():
                return False
            if not warned["gone"]:
                warned["gone"] = True
                print(
                    f"prototype root {root} no longer exists; restart `prototype.py serve "
                    "--dir <live path>` from a checkout that outlives this session",
                    file=sys.stderr, flush=True,
                )
            self.send_response(http.HTTPStatus.SERVICE_UNAVAILABLE)
            body = (
                f"prototype root {root} no longer exists.\n"
                "Restart `prototype.py serve --dir <live path>` from a checkout that "
                "outlives this session.\n"
            ).encode("utf-8")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return True

        def do_GET(self):  # noqa: N802
            if not self.root_is_gone():
                super().do_GET()

        def do_HEAD(self):  # noqa: N802
            if not self.root_is_gone():
                super().do_HEAD()

    try:
        return PrototypeServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        if port != 0 and exc.errno in (errno.EADDRINUSE, errno.EACCES):
            raise CliError(
                f"port {port} is already in use; pick another with --port <N>, "
                "or --port 0 to take a free one"
            ) from exc
        raise


def cmd_serve(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    load_manifest(root)
    httpd = make_server(root, args.port, verbose=args.verbose)
    with httpd:
        port = httpd.server_address[1]
        print(f"serving {root} at http://127.0.0.1:{port}/  (versions/v<N>/, variants/<name>/, compare.html)", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


BROWSER_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
    "msedge",
)


def find_browser() -> str | None:
    """A Chromium-family binary that supports --headless --screenshot.

    `PROTOTYPE_BROWSER` wins; then the common install paths; then PATH names.
    """
    env = os.environ.get("PROTOTYPE_BROWSER")
    if env:
        return env
    for cand in BROWSER_CANDIDATES:
        if os.path.sep in cand:
            if Path(cand).exists():
                return cand
        elif shutil.which(cand):
            return shutil.which(cand)
    return None


# Headless Chromium refuses a window narrower than this many CSS pixels, so a
# narrow capture renders inside an iframe of the requested size on a wider
# window and is cropped afterwards.
MIN_WINDOW_WIDTH = 500
HARNESS = ".capture-harness.html"


def crop_png(path: Path, width: int, height: int) -> None:
    """Crop an 8-bit RGB or RGBA, non-interlaced PNG in place to its top-left corner.

    Stdlib only, so the capture loop has no image library dependency.
    """
    import struct
    import zlib

    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CliError(f"{path} is not a PNG")
    pos = 8
    ihdr = None
    idat = b""
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    if ihdr is None:
        raise CliError(f"{path} has no IHDR")
    src_w, src_h, depth, color, _, _, interlace = ihdr
    if depth != 8 or color not in (2, 6) or interlace:
        raise CliError(f"{path}: unsupported PNG layout (depth {depth}, color {color}, interlace {interlace})")
    bpp = 3 if color == 2 else 4
    stride = src_w * bpp
    raw = zlib.decompress(idat)
    rows: list[bytearray] = []
    prev = bytearray(stride)
    off = 0
    for _ in range(src_h):
        ftype = raw[off]
        line = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ftype == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + pred) & 255
        rows.append(line)
        prev = line
    width = min(width, src_w)
    height = min(height, src_h)
    out = bytearray()
    for line in rows[:height]:
        out.append(0)
        out += line[:width * bpp]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    new_ihdr = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", new_ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(out), 6))
        + chunk(b"IEND", b"")
    )


def cmd_capture(args: argparse.Namespace) -> int:
    """Screenshot each target at the review widths into qa/.

    Uses a local headless Chromium through its command line, so the browser
    loop runs on any machine with no browser automation server. A review
    tool that drives a browser directly can replace this step; the output
    paths are the same. Widths under MIN_WINDOW_WIDTH render inside an
    iframe of the requested size and are cropped, because headless Chromium
    will not open a narrower window.
    """
    import subprocess
    import threading

    root = Path(args.dir).resolve()
    manifest = load_manifest(root)
    browser = find_browser()
    if not browser:
        raise CliError(
            "no Chromium-family browser found; set PROTOTYPE_BROWSER=/path/to/chrome, "
            "or run the browser loop in reference/review-loop.md with your browser tool"
        )
    targets = [resolve_target(root, t) for t in args.targets] or all_targets(root)
    if not targets:
        raise CliError("nothing to capture; no index.html under versions/ or variants/")
    states = capture_states(manifest, args.state, args.default_only)
    sizes = []
    for token in args.sizes.split(","):
        w, _, h = token.strip().partition("x")
        if not (w.isdigit() and h.isdigit()):
            raise CliError(f"bad --sizes entry {token!r}; use WIDTHxHEIGHT,WIDTHxHEIGHT")
        sizes.append((int(w), int(h)))

    qa = root / "qa"
    qa.mkdir(exist_ok=True)

    # Serve the prototype folder on an ephemeral port for the duration of the run.
    httpd = make_server(root, 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    written: list[Path] = []
    try:
        for target in targets:
            label = target_label(target)
            rel = target.relative_to(root).as_posix()
            for warn in visual_quality((target / "index.html").read_text(encoding="utf-8")):
                print(f"WARN {rel}: {warn}")
            for state in states:
                query = state["query"]
                url = f"http://127.0.0.1:{port}/{rel}/{query}"
                stem = f"{label}-{state['name']}" if state["name"] else label
                for width, height in sizes:
                    out = qa / f"{stem}-{width}.png"
                    narrow = width < MIN_WINDOW_WIDTH
                    if narrow:
                        # Lay the page out at the requested width inside an iframe,
                        # then crop the wider window shot back to the iframe.
                        (root / HARNESS).write_text(
                            "<!doctype html><html><head><meta charset='utf-8'><style>"
                            "html,body{margin:0;padding:0;background:#fff}"
                            f"iframe{{display:block;border:0;width:{width}px;height:{height}px}}"
                            f"</style></head><body><iframe src='/{rel}/{query}'></iframe></body></html>\n"
                        )
                        shot_url = f"http://127.0.0.1:{port}/{HARNESS}"
                        window = (MIN_WINDOW_WIDTH, height + 200)
                    else:
                        shot_url = url
                        window = (width, height)
                    shoot(browser, root, shot_url, out, window, args, url, width, height)
                    if narrow:
                        crop_png(out, width * args.scale, height * args.scale)
                    written.append(out)
                    print(f"wrote {out.relative_to(root)}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(root / ".capture-profile", ignore_errors=True)
        (root / HARNESS).unlink(missing_ok=True)
    print(f"{len(written)} capture(s); inspect each one against its acceptance criterion before marking it pass")
    return 0


def shoot(browser: str, root: Path, shot_url: str, out: Path, window: tuple[int, int],
          args: argparse.Namespace, url: str, width: int, height: int) -> None:
    """One headless-browser screenshot; raises CliError with the browser's stderr tail."""
    import subprocess

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={root / '.capture-profile'}",
        f"--window-size={window[0]},{window[1]}",
        f"--force-device-scale-factor={args.scale}",
        f"--virtual-time-budget={args.wait_ms}",
        f"--screenshot={out}",
        shot_url,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=args.timeout)
    except subprocess.CalledProcessError as exc:
        # Chromium sometimes exits non-zero while tearing down (display-link or
        # watchdog noise on macOS) after the PNG is already on disk. The shot
        # is what matters; only a missing file is a failure.
        if out.exists() and out.stat().st_size > 0:
            print(f"  (browser exited {exc.returncode} after writing {out.name}; shot kept)")
            return
        raise CliError(f"browser failed for {url} at {width}x{height}: {exc.stderr.decode(errors='replace')[-400:]}")
    except subprocess.TimeoutExpired:
        raise CliError(f"browser timed out for {url} at {width}x{height} after {args.timeout}s")
    if not out.exists():
        raise CliError(f"browser wrote no file for {url} at {width}x{height}")


def cmd_export(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    manifest = load_manifest(root)
    adapter = args.adapter or manifest.get("adapter", "local")
    target = resolve_target(root, args.target or f"v{manifest['current_version']}")
    if adapter not in CORE_ADAPTERS:
        raise CliError(
            f"unknown adapter {adapter!r}; core adapters are {', '.join(CORE_ADAPTERS)}. "
            "Export with --adapter static, then hand dist/ to your host's adapter (see reference/deploy-adapters.md)."
        )
    out = Path(args.out).resolve() if args.out else root / "dist"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(target, out)
    fixtures = root / "fixtures"
    if fixtures.exists():
        shutil.copytree(fixtures, out / "fixtures", dirs_exist_ok=True)
    rewrite_fixture_paths(out / "index.html")
    (out / "PROTOTYPE.json").write_text(json.dumps({
        "slug": manifest["slug"], "title": manifest["title"], "source": target.relative_to(root).as_posix(),
        "exported": now_iso(), "adapter": adapter,
    }, indent=2) + "\n")
    manifest["stage"] = "share"
    manifest["adapter"] = adapter
    save_manifest(root, manifest)
    if adapter == "local":
        print(f"exported {target.relative_to(root)} to {out}; open it with `prototype.py serve` or zip the folder")
    else:
        print(f"exported {target.relative_to(root)} to {out}; upload that folder to any static host")
    return 0


def rewrite_fixture_paths(index: Path) -> None:
    text = index.read_text()
    new = text.replace("../../fixtures/", "fixtures/")
    if new != text:
        index.write_text(new)


DECISION_OUTCOMES = ("promote", "iterate", "archive")


def cmd_decide(args: argparse.Namespace) -> int:
    """Record the decision: check DECISION.md is filled, then set the stage."""
    root = Path(args.dir).resolve()
    manifest = load_manifest(root)
    decision = root / "DECISION.md"
    if not decision.exists():
        raise CliError(f"{decision} not found; the scaffold writes it at init")
    text = decision.read_text()
    if not re.search(rf"^- Decision:\s*{args.outcome}\s*$", text, re.MULTILINE):
        raise CliError(
            f"DECISION.md does not say `- Decision: {args.outcome}`; edit the header line so the document and the manifest agree "
            "(the scaffold's `archive | promote | iterate` line is a placeholder, keep only one word)"
        )
    section = re.search(r"^## What we learned\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    body = "\n".join(l for l in (section.group(1) if section else "").splitlines() if l.strip() and not l.strip().startswith("<!--"))
    if not body or PLACEHOLDER.search(body):
        raise CliError("DECISION.md `## What we learned` is empty or still has placeholders; one bullet per hypothesis with its evidence")
    leftover = PLACEHOLDER.findall("\n".join(text.splitlines()[:6]))
    if leftover:
        raise CliError(f"DECISION.md header still has placeholders: {', '.join(leftover[:3])}")
    manifest["stage"] = "decide"
    manifest["decision"] = {"outcome": args.outcome, "decided_at": now_iso(), "by": args.by or manifest.get("owner", "")}
    save_manifest(root, manifest)
    print(f"decision recorded: {args.outcome} (stage decide); DECISION.md is the record, prototype.json points at it")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    m = load_manifest(root)
    gate = m.get("gate", {})
    decision = m.get("decision") or {}
    stage = m.get("stage")
    if decision.get("outcome"):
        stage = f"{stage} ({decision['outcome']})"
    print(f"{m['title']} ({m['slug']})  stage: {stage}  adapter: {m.get('adapter')}")
    print(f"gate: {'passed ' + str(gate.get('passed_at')) if gate.get('passed') else 'pending'}")
    print(f"current version: v{m['current_version']}  versions: {len(m['versions'])}  variants: {', '.join(v['name'] for v in m['variants']) or 'none'}")
    states = m.get("states", []) or []
    print(f"capture states: {', '.join(s.get('name', '') for s in states) or 'default only (add `states` to prototype.json or pass --state)'}")
    matrix = root / "reviews" / "acceptance-matrix.md"
    if matrix.exists():
        text = matrix.read_text()
        counts = {k: text.count(f"| {k} |") for k in ("pass", "fail", "open")}
        print(f"acceptance matrix: {counts['pass']} pass, {counts['fail']} fail, {counts['open']} open")
    else:
        print("acceptance matrix: not generated (run `prototype.py criteria`)")
    problems = gate_findings(root)
    if problems and not gate.get("passed"):
        print("discovery gaps:")
        for p in problems:
            print(f"  - {p}")
    return 0


# --- entry point -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_dir(sp):
        sp.add_argument("--dir", default=".", help="prototype directory (default: cwd)")

    sp = sub.add_parser("init", help="scaffold a prototype directory")
    sp.add_argument("slug")
    sp.add_argument("--dir", help="where to create it (default: ./<slug>)")
    sp.add_argument("--title")
    sp.add_argument("--owner")
    sp.add_argument("--feedback-link", action="append", metavar="LABEL=URL", help="feedback section link (repeatable, up to 3)")
    sp.add_argument("--brief", metavar="PATH", help="copy the requester's brief or exercise prompt into BRIEF.md and link it from the manifest")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("gate", help="check the discovery doc")
    add_dir(sp)
    sp.add_argument("--approve", action="store_true", help="record the gate as passed")
    sp.add_argument("--by", help="who approved")
    sp.set_defaults(func=cmd_gate)

    sp = sub.add_parser("criteria", help="refresh reviews/acceptance-matrix.md")
    add_dir(sp)
    sp.set_defaults(func=cmd_criteria)

    sp = sub.add_parser("version", help="snapshot the current version as v<N+1>")
    add_dir(sp)
    sp.add_argument("--note", required=True, help="one-line change log entry")
    sp.add_argument("--skip-gate", action="store_true")
    sp.set_defaults(func=cmd_version)

    sp = sub.add_parser("fork", help="copy a version into variants/<name>")
    add_dir(sp)
    sp.add_argument("name")
    sp.add_argument("--from", dest="from_target", help="source version or variant (default: current)")
    sp.add_argument("--hypothesis", help="what this variant tests")
    sp.add_argument("--skip-gate", action="store_true")
    sp.set_defaults(func=cmd_fork)

    sp = sub.add_parser("compare", help="write compare.html")
    add_dir(sp)
    sp.add_argument("targets", nargs="*", help="versions or variants (default: all)")
    sp.add_argument("--state", help="open every frame at this state: a name from prototype.json `states` or a raw query such as ?step=offer")
    sp.add_argument("--out", help="file name inside the prototype directory (default compare.html); use it to keep one page per state")
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("check", help="static checks on index.html files")
    add_dir(sp)
    sp.add_argument("targets", nargs="*")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("serve", help="serve the prototype directory")
    add_dir(sp)
    sp.add_argument("--port", type=int, default=8787, help="TCP port; 0 picks a free one and prints it")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("capture", help="screenshot versions and variants into qa/ with a local headless browser")
    add_dir(sp)
    sp.add_argument("targets", nargs="*", help="versions or variants (default: all)")
    sp.add_argument("--sizes", default="1440x900,390x844", help="comma-separated WIDTHxHEIGHT list")
    sp.add_argument("--wait-ms", type=int, default=1500, help="virtual time budget before the shot")
    sp.add_argument("--timeout", type=int, default=60, help="seconds per shot before giving up")
    sp.add_argument("--scale", type=int, default=1, help="device scale factor (2 for a retina-density shot)")
    sp.add_argument("--state", action="append", metavar="NAME[=QUERY]",
                    help="shoot only this state: a name from prototype.json `states`, or NAME=QUERY for an ad hoc one, as qa/<target>-NAME-<width>.png (repeatable)")
    sp.add_argument("--default-only", action="store_true", help="ignore prototype.json `states`; shoot only the default state")
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser("export", help="write dist/ for a static host")
    add_dir(sp)
    sp.add_argument("--adapter", help="local or static (default: manifest adapter); other names are refused with a pointer to reference/deploy-adapters.md")
    sp.add_argument("--target", help="version or variant to export (default: current)")
    sp.add_argument("--out", help="output directory (default: dist/)")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("decide", help="record promote, iterate, or archive from DECISION.md and set the stage")
    add_dir(sp)
    sp.add_argument("outcome", choices=DECISION_OUTCOMES, help="must match the `- Decision:` line in DECISION.md")
    sp.add_argument("--by", help="who decided (default: manifest owner)")
    sp.set_defaults(func=cmd_decide)

    sp = sub.add_parser("status", help="print the manifest summary")
    add_dir(sp)
    sp.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
