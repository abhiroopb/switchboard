#!/usr/bin/env python3
"""One comparison page for every set of images PM OS shows the owner.

Images go on one page so the owner can compare them easily, every time images are
shared (external-replies Rule 14). Every surface that shares images with
the owner (options, variants, before and after, screenshots, even one image) puts
them on one page and gives the owner one clickable link to it. Per-image links stay
as a secondary list.

This helper writes that page: one self-contained HTML file (inline CSS and
JS, no network) with the images side by side or stacked, labels, groups (for
example a full banner plus its crop preview), caveat lines, verbatim text
blocks (for example descriptions), click to view full size, and file paths.

    image_compare.py build --title "event covers" --workstream book-club \\
        --group "Cover C1" --image path/full.png::"Full banner" --image path/crop.png::"7:1 crop" \\
        --caveat "Recommended" --group "Cover C2" --image ... --text "Description A::<verbatim>"
    image_compare.py build --spec spec.json          # same model as JSON (see SPEC below)
    image_compare.py from-report --text "<report>" --workstream SLUG --task-id rt-...
    image_compare.py open PATH_OR_FILE_URL [--force]   # open any local page or image on the owner's Mac
    image_compare.py open-report --text "<report>"      # open the compare page(s) a delivered report links

`build` prints JSON {path, main_path, file_url, link, main_link, images,
opened}. `--print link` prints only the Markdown link. `--open` opens the page
on the owner's Mac right after it is written (the default when run from a terminal;
`--no-open` skips it). `from-report` is the report-to-cos.sh backstop: when a
report names 2 or more local images and has no compare-page link, it builds a
page from them and prints {built, link, ...}; when it cannot, it prints the
`⚠️ No compare page` warning instead.

Open, never "click this". Whoever shares a compare
page or any local HTML review page with the owner opens it with macOS `open` and
mentions the path in one line. `open` resolves the copy that exists now
(review_surface.resolve_local_path: main once landed, else the live worktree
copy), runs `open` once per page version (state in main's
`.state/compare-opened.json`, keyed by the main-relative path and a content
hash so a worktree page and its landed copy open once, 10 minutes; `--force`
reopens), and prints
{opened, path, line}. Kill switch IMAGE_COMPARE_OPEN_DISABLED=1; the opener
command is `$PM_OS_OPEN_CMD` (default: a cmux browser tab on macOS, see below;
`xdg-open` on Linux with a display; the default app through os.startfile with
SW_SHOWNOACTIVATE on Windows; with none of these it prints the path and opens
nothing).

Open without stealing focus. On macOS with cmux running, the page (or image, or http(s) URL) lands
in an unselected cmux browser tab (`cmux new-surface --type browser --focus
false`) in the CoS workspace ($PM_OS_OPEN_WORKSPACE overrides; else the
focused workspace). No app is activated and the focused pane keeps focus. A
later version of the same page reuses that tab. cmux's webview reads only the
page's own folder, so the tab shows a copy with local images inlined (main's
`.state/compare-view/`). Chrome raises itself whenever another app hands it a
page, `open -g` or not, so without cmux a Chrome-family default browser is skipped and the link stays
in the message (PM_OS_OPEN_CHROME=1 opens it anyway). Other browsers get
`open -g` plus a 2-second focus guard that hands the front back to the app
the owner was using (`_guard_focus`; PM_OS_FOCUS_GUARD_DISABLED=1 turns it off). A
page whose default browser is not running is not opened (a launching browser
comes to the front; PM_OS_OPEN_COLD_BROWSER=1 opens it anyway). Windows asks
the shell not to activate the window; Linux `xdg-open` has no such flag. Off
macOS a Chromium-family default browser (Chrome, Edge, Brave) is skipped the
same way, except under Wayland, which keeps focus where it was
(`_offmac_chromium`); PM_OS_OPEN_VIA=wmux opts into the wmux browser panel.
PM_OS_OPEN_VIA=browser skips cmux. Every attempt appends one line with the
front app before and after to main's `.state/page-open.jsonl`. The
pm-os-guardrails `open-focus` rule rejects an agent's `open <page>`, `open -g
<page>`, and `qlmanage -p`; test_background_open.py fails on a bare `open` of
a page in automation code.

Paths. The repo root comes from `git rev-parse --show-toplevel` in the cwd.
The page goes to `workstreams/<slug>/assets/compare/<date>-<slug>.html` in
that checkout (or `.amp/in/artifacts/compare/` with no workstream). `link`
names the copy that exists now: in a task worktree that is the worktree page,
because the main path exists only after the branch lands. `main_link` names
where the page lands; report-to-cos.sh rewrites a worktree link to main for
a done report, which lands before delivery. Every image source is the main
path first, with the path as given as a fallback the page tries when the first
one does not load. `--embed` inlines the image bytes instead, for a page that
must survive the files moving.

SPEC (JSON): {"title", "subtitle", "workstream", "slug", "caveats": [str],
"texts": [{"label", "text"}], "images": [{"path", "label", "caveat"}],
"groups": [{"name", "key", "badge", "images": [...], "caveats": [...], "texts": [...]}]}.
Ungrouped images each get their own card; a group is one card with its images.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repo_paths  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
COMPARE_WARNING = "⚠️ No compare page"
MIN_AUTO_IMAGES = 2
IMAGE_EXTS = ("png", "jpg", "jpeg", "gif", "webp", "svg", "heic", "tif", "tiff", "bmp", "avif")


# --- model -------------------------------------------------------------------

@dataclass
class Image:
    path: str
    label: str = ""
    caveat: str = ""


@dataclass
class Text:
    label: str
    text: str


@dataclass
class Group:
    name: str = ""
    badge: str = ""
    key: str = ""
    images: list[Image] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)


@dataclass
class Page:
    title: str
    subtitle: str = ""
    workstream: str = ""
    slug: str = ""
    caveats: list[str] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)

    def all_images(self) -> list[Image]:
        return [img for g in self.groups for img in g.images]


def _texts(raw) -> list[Text]:
    out = []
    for t in raw or []:
        if isinstance(t, str):
            label, _, body = t.partition("::")
            out.append(Text(label.strip() if body else "", body if body else label))
        else:
            out.append(Text(str(t.get("label") or ""), str(t.get("text") or "")))
    return out


def _images(raw) -> list[Image]:
    out = []
    for i in raw or []:
        if isinstance(i, str):
            path, _, label = i.partition("::")
            out.append(Image(path.strip(), label.strip()))
        else:
            out.append(Image(str(i["path"]), str(i.get("label") or ""), str(i.get("caveat") or "")))
    return out


def page_from_spec(spec: dict) -> Page:
    page = Page(title=str(spec.get("title") or "Images"), subtitle=str(spec.get("subtitle") or ""),
                workstream=str(spec.get("workstream") or ""), slug=str(spec.get("slug") or ""),
                caveats=[str(c) for c in spec.get("caveats") or []], texts=_texts(spec.get("texts")))
    for img in _images(spec.get("images")):
        page.groups.append(Group(images=[img]))
    for g in spec.get("groups") or []:
        page.groups.append(Group(name=str(g.get("name") or ""), badge=str(g.get("badge") or ""),
                                 key=str(g.get("key") or ""),
                                 images=_images(g.get("images")), caveats=[str(c) for c in g.get("caveats") or []],
                                 texts=_texts(g.get("texts"))))
    return page


# --- paths -------------------------------------------------------------------

def repo_root(cwd: Path | None = None) -> Path:
    """The checkout the page is written into: `git rev-parse --show-toplevel`
    in the cwd (a task worktree for a worker), else the vault override."""
    override = os.environ.get("PM_OS_COMPARE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    probe = subprocess.run(["git", "-C", str(cwd or Path.cwd()), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
    if probe.returncode == 0 and probe.stdout.strip():
        return Path(probe.stdout.strip()).resolve()
    return (cwd or Path.cwd()).resolve()


def main_root(top: Path) -> Path:
    override = os.environ.get("PM_OS_VAULT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return repo_paths.main_checkout(top).resolve()


def to_main(path: Path, main: Path) -> Path:
    """A path inside `<main>/.amp/worktrees/<id>/` names the same path on main,
    where dispatch.py lands the branch and moves `.amp/in`."""
    trees = main / ".amp" / "worktrees"
    try:
        parts = path.relative_to(trees).parts
    except ValueError:
        return path
    return main.joinpath(*parts[1:]) if len(parts) > 1 else path


def resolve_image(raw: str, top: Path, main: Path) -> tuple[Path, Path]:
    """(path as found, path on main). Relative paths try the checkout first,
    then main. A `file://` URL is accepted."""
    raw = raw.strip()
    if raw.startswith("file://"):
        from urllib.parse import unquote, urlsplit
        raw = unquote(urlsplit(raw).path)
        raw = raw[1:] if re.match(r"^/[A-Za-z]:/", raw) else raw  # file:///C:/x is C:/x
    path = Path(raw).expanduser()
    if not path.is_absolute():
        cands = [top / path, main / path]
        path = next((c for c in cands if c.exists()), cands[0])
    path = Path(os.path.abspath(path))
    return path, to_main(path, main)


def file_url(path: Path) -> str:
    posix = path.as_posix()
    if re.match(r"^[A-Za-z]:/", posix):  # Windows drive path: file:///C:/..., colon kept
        return "file:///" + quote(posix, safe="/:")
    return "file://" + quote(posix, safe="/")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "images"


_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def default_out(page: Page, top: Path, now: datetime) -> Path:
    if page.workstream and not _SLUG.fullmatch(page.workstream):
        raise ValueError(f"workstream must be a kebab-case slug, got {page.workstream!r}")
    if page.slug and not _SLUG.fullmatch(page.slug):
        page.slug = slugify(page.slug)
    name = f"{now:%Y-%m-%d}-{page.slug or slugify(page.title)}.html"
    if page.workstream:
        return top / "workstreams" / page.workstream / "assets" / "compare" / name
    return top / ".amp" / "in" / "artifacts" / "compare" / name


# --- render ------------------------------------------------------------------

_CSS = """
:root{--ink:#111;--mute:#5c5c5c;--line:#e2e2e0;--bg:#f6f6f4;--card:#fff;--accent:#004bff;--soft:#e8efff}
@media (prefers-color-scheme:dark){:root{--ink:#eee;--mute:#a7a7a7;--line:#333;--bg:#161616;--card:#202020;--accent:#7aa2ff;--soft:#233054}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif;color:var(--ink);background:var(--bg)}
main{max-width:1760px;margin:0 auto;padding:28px 36px 72px}
h1{font-size:26px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 6px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0 18px;position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:2}
.bar button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:6px 12px;cursor:pointer}
.bar button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.bar .count{margin-left:auto;color:var(--mute)}
ul.caveats{margin:0 0 16px;padding-left:20px;color:var(--mute)}
.grid{display:grid;gap:22px;grid-template-columns:repeat(auto-fill,minmax(min(var(--min,520px),calc(50% - 11px)),1fr))}
body.stacked .grid{grid-template-columns:1fr}
article{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
article header{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
article h2{font-size:17px;margin:0}.key{background:var(--accent);color:#fff;font-weight:600;border-radius:6px;padding:1px 8px;font-size:13px}
.badge{background:var(--soft);color:var(--accent);font-weight:600;font-size:12px;border-radius:99px;padding:2px 10px}
figure{margin:0 0 14px}figure:last-of-type{margin-bottom:0}
.lbl{font-size:12px;color:var(--mute);margin:8px 0 4px;text-transform:uppercase;letter-spacing:.04em}
.frame{display:block;width:100%;padding:0;border:1px solid var(--line);border-radius:6px;background:repeating-conic-gradient(#0000 0 25%,#8881 0 50%) 0 0/16px 16px;cursor:zoom-in}
.frame img{display:block;width:100%;height:auto;border-radius:5px}
.frame img.missing{min-height:80px}
figcaption{font-size:13px;color:var(--mute);margin-top:6px}
figcaption .cav{color:var(--ink);display:block}
figcaption code,.paths code{font:12px ui-monospace,Menlo,monospace;word-break:break-all}
figcaption a,.paths a{color:var(--accent)}
.note{color:var(--mute);font-size:14px;margin:8px 0 0}
section.texts{margin-top:28px}section.texts h2,.paths h2{font-size:18px;margin:0 0 10px}
.tgrid{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(min(420px,calc(50% - 8px)),1fr))}
body.stacked .tgrid{grid-template-columns:1fr}
pre{white-space:pre-wrap;font:14px/1.5 ui-monospace,Menlo,monospace;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:0}
.paths{margin-top:32px}.paths ol{padding-left:22px}.paths li{margin:3px 0}
#lb{position:fixed;inset:0;background:#000d;display:none;z-index:9;flex-direction:column}
#lb.open{display:flex}
#lb .top{display:flex;gap:10px;align-items:center;color:#fff;padding:10px 16px}
#lb .top span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#lb button{font:inherit;background:#fff2;color:#fff;border:0;border-radius:8px;padding:6px 12px;cursor:pointer}
#lb .stage{flex:1;overflow:auto;display:flex;align-items:center;justify-content:center;padding:0 16px 16px}
#lb img{max-width:100%;max-height:100%;cursor:zoom-in}
#lb.actual .stage{align-items:flex-start;justify-content:flex-start}#lb.actual img{max-width:none;max-height:none;cursor:zoom-out}
"""

_JS = """
(function(){
  var imgs=[].slice.call(document.querySelectorAll('img[data-srcs]'));
  imgs.forEach(function(img){
    var srcs=JSON.parse(img.getAttribute('data-srcs')),i=0;
    img.addEventListener('error',function(){i+=1;if(i<srcs.length){img.src=srcs[i];}else{img.classList.add('missing');img.alt='Not found: '+img.alt;}});
    img.addEventListener('load',function(){var d=img.closest('figure').querySelector('.dim');if(d){d.textContent=img.naturalWidth+' \\u00d7 '+img.naturalHeight;}});
  });
  var lb=document.getElementById('lb'),big=lb.querySelector('img'),cap=lb.querySelector('.top span'),cur=0,last=null;
  function show(n){cur=(n+imgs.length)%imgs.length;var s=imgs[cur];big.src=s.currentSrc||s.src;big.alt=s.alt;cap.textContent=(cur+1)+' / '+imgs.length+'  \\u00b7  '+s.getAttribute('data-title');}
  function open(n){last=document.activeElement;lb.classList.add('open');lb.classList.remove('actual');show(n);lb.querySelector('.close').focus();}
  function close(){lb.classList.remove('open');big.removeAttribute('src');if(last){last.focus();}}
  imgs.forEach(function(img,n){img.closest('button').addEventListener('click',function(){open(n);});});
  lb.querySelector('.close').addEventListener('click',close);
  lb.querySelector('.prev').addEventListener('click',function(){show(cur-1);});
  lb.querySelector('.next').addEventListener('click',function(){show(cur+1);});
  lb.querySelector('.actual-btn').addEventListener('click',function(){lb.classList.toggle('actual');});
  big.addEventListener('click',function(){lb.classList.toggle('actual');});
  lb.querySelector('.stage').addEventListener('click',function(e){if(e.target===this){close();}});
  document.addEventListener('keydown',function(e){
    if(!lb.classList.contains('open')){return;}
    if(e.key==='Escape'){close();}else if(e.key==='ArrowLeft'){show(cur-1);}else if(e.key==='ArrowRight'){show(cur+1);}
    else if(e.key==='Tab'){var f=[].slice.call(lb.querySelectorAll('button'));var k=f.indexOf(document.activeElement);
      e.preventDefault();f[(k+(e.shiftKey?-1:1)+f.length)%f.length].focus();}
  });
  var side=document.getElementById('side'),stack=document.getElementById('stack');
  function layout(st){document.body.classList.toggle('stacked',st);side.setAttribute('aria-pressed',String(!st));stack.setAttribute('aria-pressed',String(st));}
  side.addEventListener('click',function(){layout(false);});stack.addEventListener('click',function(){layout(true);});
})();
"""


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _label(img: Image, found: Path) -> str:
    if img.label:
        return img.label
    stem = re.sub(r"[-_]+", " ", found.stem).strip()
    return stem[:1].upper() + stem[1:] if stem else found.name


def _srcs(found: Path, on_main: Path, out_main: Path, out_actual: Path, embed: bool) -> list[str]:
    if embed and found.exists():
        mime = mimetypes.guess_type(found.name)[0] or "image/png"
        return [f"data:{mime};base64," + base64.b64encode(found.read_bytes()).decode()]
    srcs: list[str] = []
    for page, target in ((out_main, on_main), (out_actual, found)):
        srcs.append(quote(os.path.relpath(target, page.parent), safe="/.-_~"))
    srcs += [file_url(on_main), file_url(found)]
    return list(dict.fromkeys(srcs))


def render(page: Page, out_actual: Path, out_main: Path, top: Path, main: Path, *,
           embed: bool = False, now: datetime | None = None) -> tuple[str, list[dict]]:
    """HTML for the page plus the resolved image records (label, main path, found)."""
    now = now or datetime.now(PT)
    esc = html.escape
    records: list[dict] = []
    cards: list[str] = []
    multi = sum(1 for g in page.groups if len(g.images) > 1 or g.name)
    letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for g in page.groups:
        figs: list[str] = []
        for img in g.images:
            found, on_main = resolve_image(img.path, top, main)
            label = _label(img, found)
            title = f"{g.name} · {label}" if g.name and len(g.images) > 1 else (g.name or label)
            size = _human_size(found.stat().st_size) if found.exists() else "not found"
            records.append({"label": title, "path": str(on_main), "found": str(found), "exists": found.exists()})
            srcs = _srcs(found, on_main, out_main, out_actual, embed)
            lbl = f'<div class="lbl">{esc(label)}</div>' if g.name and len(g.images) > 1 else ""
            cav = f'<span class="cav">{esc(img.caveat)}</span>' if img.caveat else ""
            figs.append(
                f'<figure>{lbl}<button class="frame" type="button" aria-label="View {esc(title, True)} full size">'
                f'<img src="{esc(srcs[0], True)}" data-srcs="{esc(json.dumps(srcs), True)}" '
                f'data-title="{esc(title, True)}" alt="{esc(title, True)}" loading="lazy"></button>'
                f'<figcaption>{cav}<span class="dim"></span> · {esc(size)} · '
                f'<a href="{esc(file_url(on_main), True)}"><code>{esc(str(on_main))}</code></a></figcaption></figure>'
            )
        head = ""
        if g.name or g.badge or len(g.images) == 1:
            letter = next(letters)
            key = g.key or (letter if multi > 1 or len(page.groups) > 1 else "")
            key = f'<span class="key">{esc(key)}</span>' if key else ""
            name = g.name or (records[-1]["label"] if g.images else "")
            badge = f'<span class="badge">{esc(g.badge)}</span>' if g.badge else ""
            head = f"<header>{key}<h2>{esc(name)}</h2>{badge}</header>"
        notes = "".join(f'<p class="note">{esc(c)}</p>' for c in g.caveats)
        texts = "".join(f'<div class="lbl">{esc(t.label)}</div><pre>{esc(t.text)}</pre>' for t in g.texts)
        cards.append(f"<article>{head}{''.join(figs)}{notes}{texts}</article>")

    n = len(records)
    widest = max((len(g.images) for g in page.groups), default=1)
    min_col = "420px" if n >= 4 and widest == 1 else "560px"
    caveats = ("<ul class=\"caveats\">" + "".join(f"<li>{esc(c)}</li>" for c in page.caveats) + "</ul>") if page.caveats else ""
    texts = ""
    if page.texts:
        texts = ('<section class="texts"><h2>Text</h2><div class="tgrid">'
                 + "".join(f'<div><div class="lbl">{esc(t.label)}</div><pre>{esc(t.text)}</pre></div>' for t in page.texts)
                 + "</div></section>")
    paths = ('<section class="paths"><h2>Each image</h2><ol>'
             + "".join(f'<li>{esc(r["label"])}: <a href="{esc(file_url(Path(r["path"])), True)}"><code>{esc(r["path"])}</code></a>'
                       + ("" if r["exists"] else " (not found)") + "</li>" for r in records)
             + "</ol></section>")
    subtitle = f'<p class="sub">{esc(page.subtitle)}</p>' if page.subtitle else ""
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(page.title)}</title><style>{_CSS}</style></head>"
        f'<body><main style="--min:{min_col}"><h1>{esc(page.title)}</h1>{subtitle}'
        f'<p class="sub">Built {now:%Y-%m-%d} {now.hour % 12 or 12}:{now:%M %p} PT. Click any image to view it full size; '
        "arrow keys step through, Escape closes.</p>"
        '<div class="bar" role="toolbar" aria-label="Layout">'
        '<button id="side" type="button" aria-pressed="true">Side by side</button>'
        '<button id="stack" type="button" aria-pressed="false">Stacked</button>'
        f'<span class="count">{n} image{"" if n == 1 else "s"}</span></div>'
        f'{caveats}<div class="grid">{"".join(cards)}</div>{texts}{paths}</main>'
        '<div id="lb" role="dialog" aria-modal="true" aria-label="Full size image">'
        '<div class="top"><span></span><button class="prev" type="button" aria-label="Previous image">&larr;</button>'
        '<button class="next" type="button" aria-label="Next image">&rarr;</button>'
        '<button class="actual-btn" type="button">Actual size</button>'
        '<button class="close" type="button" aria-label="Close">Close</button></div>'
        '<div class="stage"><img alt=""></div></div>'
        f"<script>{_JS}</script></body></html>\n"
    )
    return doc, records


def build(page: Page, *, out: Path | None = None, cwd: Path | None = None, embed: bool = False,
          now: datetime | None = None) -> dict:
    now = now or datetime.now(PT)
    top = repo_root(cwd)
    main = main_root(top)
    out_actual = Path(os.path.abspath(out or default_out(page, top, now)))
    out_main = to_main(out_actual, main)
    if out_main == out_actual and top != main:
        try:
            out_main = main / out_actual.relative_to(top)
        except ValueError:
            pass
    doc, records = render(page, out_actual, out_main, top, main, embed=embed, now=now)
    out_actual.parent.mkdir(parents=True, exist_ok=True)
    out_actual.write_text(doc, encoding="utf-8")
    n = len(records)
    label = f"Compare {n} image{'' if n == 1 else 's'}"
    # The link names the copy that exists now (the worktree page before the
    # branch lands); main_link names where it lands.
    live = out_main if out_main.exists() else out_actual
    url = file_url(live)
    return {"path": str(out_actual), "main_path": str(out_main), "file_url": url,
            "link": f"[{label}]({url})", "main_link": f"[{label}]({file_url(out_main)})", "images": records}


# --- open on the owner's Mac ----------------------------------------------------------

OPEN_DEDUPE_SECONDS = 600


def _local_target(target: str) -> Path:
    raw = (target or "").strip().strip("<>")
    if raw.startswith("file://"):
        from urllib.parse import unquote, urlsplit
        raw = unquote(urlsplit(raw).path)
        raw = raw[1:] if re.match(r"^/[A-Za-z]:/", raw) else raw  # file:///C:/x is C:/x
    return Path(raw).expanduser()


def _opened_state(main: Path) -> Path:
    return main / ".state" / "compare-opened.json"


# ShellExecute show command for a window that appears without taking focus.
SW_SHOWNOACTIVATE = 4


def _is_windows() -> bool:
    return os.name == "nt"


def _default_opener(target: Path | str) -> list[str] | None:
    """The argv that opens `target` (a path, or an http(s) URL as a string, which
    Path would fold to `https:/…`) here in the background; [] means os.startfile
    (Windows); None means no way to open it (a Linux box with no display, or no
    xdg-open). Never steal focus: `open -g` loads the page
    without bringing the browser to the front."""
    if sys.platform == "darwin":
        return ["open", "-g", str(target)]
    if _is_windows():
        return [] if hasattr(os, "startfile") else None
    xdg = shutil.which("xdg-open")
    if xdg and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return [xdg, str(target)]
    return None


# HTML content types whose default handler is the browser.
_HTML_CONTENT_TYPES = ("public.html", "public.xhtml")


def _macos_html_handler() -> str | None:
    """Bundle id of the default app for .html pages (LaunchServices), or None."""
    plist = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    try:
        import plistlib
        handlers = plistlib.loads(plist.read_bytes()).get("LSHandlers", [])
    except Exception:
        return None
    for h in handlers:
        if h.get("LSHandlerContentType") in _HTML_CONTENT_TYPES and h.get("LSHandlerRoleAll"):
            return str(h["LSHandlerRoleAll"])
    return "com.apple.Safari"


def _macos_app_running(bundle_id: str) -> bool | None:
    """True/False when lsappinfo can tell whether the app runs; None when it
    cannot. Bundle ids compare without case: LaunchServices stores
    `com.google.chrome` for the running `com.google.Chrome`."""
    try:
        r = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or "bundleID=" not in r.stdout:
        return None
    running = {b.lower() for b in re.findall(r'bundleID="([^"]+)"', r.stdout)}
    return bundle_id.lower() in running


def _browser_cold(path: Path) -> str | None:
    """On macOS, the default browser's bundle id when a page would launch it
    (not running), else None. `open -g` keeps a running browser behind the owner's
    app, but a browser that has to launch may still put its first window in
    front, so the
    automatic open waits: the link stays in the message. PM_OS_OPEN_COLD_BROWSER=1
    opens anyway."""
    if sys.platform != "darwin" or path.suffix.lower() not in (".html", ".htm"):
        return None
    if os.environ.get("PM_OS_OPEN_COLD_BROWSER") == "1":
        return None
    bundle = _macos_html_handler()
    if not bundle:
        return None
    return bundle if _macos_app_running(bundle) is False else None


FOCUS_GUARD_SECONDS = 2.0


def _frontmost_bundle() -> str | None:
    """Bundle id of the frontmost macOS app, or None when lsappinfo cannot say."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(["lsappinfo", "info", "-only", "bundleid", asn],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'"(?:CFBundleIdentifier|bundleid)"\s*=\s*"([^"]+)"', out, re.I)
    return m.group(1) if m else None


def _guard_focus(before: str | None, browser: str | None) -> str | None:
    """Keep the owner's app in front. `open -g` asks LaunchServices not to activate
    the browser, but Chrome can still raise itself within a second when a
    tab is added. For FOCUS_GUARD_SECONDS after the open, if the
    browser becomes frontmost, re-activate the app that was in front. Another
    app coming forward means the owner switched on purpose, so the guard stops.
    Returns the bundle id it restored, or None."""
    if not before or not browser or before.lower() == browser.lower():
        return None
    deadline = time.monotonic() + FOCUS_GUARD_SECONDS
    while time.monotonic() < deadline:
        front = _frontmost_bundle()
        if front and front.lower() == browser.lower():
            try:
                subprocess.run(["open", "-b", before], capture_output=True, timeout=5)  # focus-ok: hands focus back to the owner's app
            except (OSError, subprocess.TimeoutExpired):
                return None
            return before
        if front and front.lower() != before.lower():
            return None
        time.sleep(0.05)
    return None


def _open_key(path: Path, main: Path) -> str:
    """Dedupe key: the main-relative path, so a worktree copy and its landed
    main copy count as one page and report delivery never opens the landed
    copy a second time."""
    landed = to_main(path, main)
    try:
        return str(landed.resolve().relative_to(main))
    except ValueError:
        return str(path)


def _open_version(path: Path) -> str:
    """Content hash, so the same page in a worktree and on main is one version."""
    import hashlib
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return str(int(path.stat().st_mtime))


def _startfile_background(path: Path | str) -> None:
    """os.startfile without activating the window (Python 3.10+ show_cmd);
    older Pythons fall back to the plain default-app open."""
    try:
        os.startfile(str(path), show_cmd=SW_SHOWNOACTIVATE)  # type: ignore[attr-defined,call-arg]
    except TypeError:
        os.startfile(str(path))  # type: ignore[attr-defined]


# --- off macOS: Windows (wmux) and Linux ------------------------------------------
#
# cmux is macOS only, so Windows and Linux have no unselected-tab path by default.
# The rule that holds on macOS without cmux holds here too: a Chromium-family
# browser (Chrome, Edge, Brave, Vivaldi, Opera, Chromium) brings its window
# forward when handed a page, so it gets no page and the link stays in the
# message (PM_OS_OPEN_CHROME=1 opens it anyway). A default browser that cannot
# be read counts the same way, so an unknown browser never takes the page.
# Windows with another default browser gets os.startfile with
# SW_SHOWNOACTIVATE; a browser that is already running may still raise its
# window when it takes the page, which the Windows rerun checks. Linux under
# Wayland keeps xdg-open, because the compositor refuses focus to a window the
# user did not activate; WSL is not exempt, because its xdg-open hands the page
# to the Windows browser. Linux under X11 skips a Chromium-family browser.
# PM_OS_OPEN_VIA=wmux puts the page in the wmux browser panel (`wmux browser
# open <url>`); it is opt-in until a Windows run shows that it leaves focus where
# it was.

CHROMIUM_WINDOWS_PROGIDS = ("chrome", "msedge", "brave", "vivaldi", "opera", "chromium")
# Matched anywhere in the desktop id, so Flatpak ids (reverse-DNS names) count too.
CHROMIUM_LINUX_RE = re.compile(r"chrom|brave|edge|vivaldi|opera")


def _windows_default_browser(remote: bool, suffix: str) -> str | None:
    """ProgId of the app Windows hands this target to (UserChoice for https, or
    for the file's extension), or None when the registry cannot say."""
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None
    keys = [r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"] if remote else [
        rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{suffix}\UserChoice"]
    for key in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:  # type: ignore[attr-defined]
                return str(winreg.QueryValueEx(handle, "ProgId")[0])  # type: ignore[attr-defined]
        except OSError:
            continue
    if not remote:  # a fresh profile has no UserChoice yet: the class default for the extension
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, suffix) as handle:  # type: ignore[attr-defined]
                return str(winreg.QueryValueEx(handle, "")[0]) or None  # type: ignore[attr-defined]
        except OSError:
            pass
    return None


def _is_wsl() -> bool:
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()


def _linux_default_browser() -> str | None:
    """Desktop file of the default web browser (xdg-settings), or None."""
    exe = shutil.which("xdg-settings")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "get", "default-web-browser"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def _offmac_chromium(remote: bool, path: Path) -> str | None:
    """Why this page is skipped off macOS: the Chromium-family browser that
    would take it (and raise itself), "unknown" when the default browser cannot
    be read, or the WSL hand-off; else None. Images open in their viewer, not a
    browser, so only pages and URLs are checked. Linux under Wayland is exempt."""
    if sys.platform == "darwin" or os.environ.get("PM_OS_OPEN_CHROME") == "1":
        return None
    if not remote and path.suffix.lower() not in (".html", ".htm"):
        return None
    if _is_windows():
        progid = _windows_default_browser(remote, path.suffix.lower() or ".html")
        if not progid:
            return "unknown"
        return progid if progid.lower().startswith(CHROMIUM_WINDOWS_PROGIDS) else None
    if _is_wsl():
        return "of Windows (via WSL)"
    if os.environ.get("WAYLAND_DISPLAY"):
        return None
    desktop = _linux_default_browser()
    if not desktop:
        return "unknown"
    return desktop if CHROMIUM_LINUX_RE.search(desktop.lower()) else None


def _open_in_wmux(url: str) -> tuple[bool, str]:
    """`wmux browser open <url>` (PM_OS_OPEN_VIA=wmux only). Returns (ok, error)."""
    try:
        from lib import mux  # type: ignore[import-not-found]
        argv, extra = mux.wmux_cli()
    except Exception as exc:  # noqa: BLE001 - a missing helper means no wmux path
        return False, f"wmux CLI not found ({exc})"
    if not argv:
        return False, "wmux CLI not found"
    try:
        r = subprocess.run([*argv, "browser", "open", url], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL, env={**os.environ, **extra})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"wmux failed: {exc}"
    if r.returncode != 0:
        return False, f"wmux exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
    return True, ""


# --- open in a cmux browser tab (macOS, never touches the frontmost app) ------
#
# Chrome raises itself whenever another app hands it a page, `open -g` or not,
# and even with the focus guard that is a visible flash that can take
# keystrokes. So on macOS a page opens as an unselected browser
# tab inside cmux (`cmux new-surface --type browser --focus false`): no app is
# activated, the focused cmux pane stays focused, and the tab waits in the CoS
# workspace's tab bar. cmux's webview reads only the page's own folder, so the
# tab shows a copy with the images inlined (main's .state/compare-view/).

CHROME_FAMILY = ("com.google.chrome", "com.google.chrome.beta", "com.google.chrome.canary",
                 "com.brave.browser", "com.microsoft.edgemac", "company.thebrowser.browser",
                 "org.chromium.chromium", "com.vivaldi.vivaldi", "com.operasoftware.opera")
VIEW_INLINE_MAX_BYTES = 40 * 1024 * 1024
_IMG_TAG = re.compile(r'<img\b[^>]*>', re.I)
_ATTR = re.compile(r'\b(src|data-srcs)="([^"]*)"', re.I)


def _cmux_bin() -> str | None:
    """The cmux CLI when this Mac runs cmux and its socket answers, else None.
    PM_OS_OPEN_VIA=browser skips cmux (the old `open -g` path)."""
    if sys.platform != "darwin" or os.environ.get("PM_OS_OPEN_VIA") == "browser":
        return None
    exe = shutil.which("cmux") or ("/Applications/cmux.app/Contents/Resources/bin/cmux"
                                   if Path("/Applications/cmux.app/Contents/Resources/bin/cmux").exists() else None)
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "ping"], capture_output=True, text=True, timeout=5,
                           env={**os.environ, "CMUX_QUIET": "1"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return exe if r.returncode == 0 else None


def _cmux_target_workspace(exe: str, main: Path) -> str | None:
    """Where the tab goes: $PM_OS_OPEN_WORKSPACE, else the CoS workspace (where
    the report lands), else the workspace the owner has focused. Never a worker tab:
    the dispatcher reaps those, and the page would close with it."""
    pinned = os.environ.get("PM_OS_OPEN_WORKSPACE", "").strip()
    if pinned:
        return pinned
    script = main / "system" / "scripts" / "owner_tabs.py"
    if script.exists():
        try:
            r = subprocess.run([sys.executable, str(script), "cos-target"], capture_output=True,
                               text=True, timeout=15, cwd=str(main))
            ref = (json.loads(r.stdout or "{}") or {}).get("ref") if r.returncode == 0 else None
            if ref:
                return str(ref)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    try:
        r = subprocess.run([exe, "identify", "--json"], capture_output=True, text=True, timeout=5)
        return (json.loads(r.stdout or "{}").get("focused") or {}).get("workspace_ref")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _inline_src(raw: str, base: Path) -> str | None:
    """A data: URI for a local image reference (relative to the page, absolute,
    or file://), or None when it is remote, missing, or too large."""
    raw = html.unescape(raw).strip()
    if not raw or raw.startswith(("data:", "http://", "https://")):
        return None
    path = _local_target(raw)
    if not path.is_absolute():
        path = base / path
    try:
        if not path.is_file() or path.stat().st_size > VIEW_INLINE_MAX_BYTES:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def _view_copy(path: Path, main: Path, key: str) -> Path:
    """The page to show in cmux: an .html page gets a copy with every local
    <img> inlined (cmux's webview cannot read files outside the page folder);
    anything else is shown as is."""
    if path.suffix.lower() not in (".html", ".htm"):
        return path
    import hashlib
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return path
    base = path.parent

    def fix(tag: re.Match) -> str:
        attrs = dict((k.lower(), v) for k, v in _ATTR.findall(tag.group(0)))
        cands: list[str] = []
        if "data-srcs" in attrs:
            try:
                cands = [str(c) for c in json.loads(html.unescape(attrs["data-srcs"]))]
            except ValueError:
                cands = []
        if "src" in attrs:
            cands.insert(0, attrs["src"])
        uri = next((u for u in (_inline_src(c, base) for c in cands) if u), None)
        if not uri:
            return tag.group(0)
        out = _ATTR.sub(lambda m: f'{m.group(1)}="{uri}"' if m.group(1).lower() == "src"
                        else f'{m.group(1)}="{html.escape(json.dumps([uri]), quote=True)}"', tag.group(0))
        return out.replace(' loading="lazy"', "")

    view_dir = main / ".state" / "compare-view"
    view = view_dir / f"{hashlib.sha1(key.encode()).hexdigest()[:12]}-{path.name}"
    try:
        view_dir.mkdir(parents=True, exist_ok=True)
        view.write_text(_IMG_TAG.sub(fix, text), encoding="utf-8")
    except OSError:
        return path
    return view


def _open_in_cmux(exe: str, url: str, workspace: str, reuse: str | None) -> tuple[str | None, str]:
    """Show `url` in an unselected cmux browser tab. Reuses the tab this page
    opened last time when it still exists. Returns (surface ref, error)."""
    if reuse:
        try:
            r = subprocess.run([exe, "browser", reuse, "navigate", url], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return reuse, ""
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        r = subprocess.run([exe, "new-surface", "--type", "browser", "--workspace", workspace,
                            "--url", url, "--focus", "false"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"cmux failed: {exc}"
    m = re.search(r"\bsurface:\d+", r.stdout or "")
    if r.returncode != 0 or not m:
        return None, f"cmux exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
    return m.group(0), ""


def _log_open(main: Path, entry: dict) -> None:
    """One JSON line per open attempt in main's .state/page-open.jsonl, with the
    frontmost app before and after, so the next focus report has evidence."""
    try:
        log = main / ".state" / "page-open.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"), **entry},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


def open_page(target: str, *, force: bool = False, cwd: Path | None = None, task_id: str | None = None) -> dict:
    """Open a local page (or image, or an http(s) page) for the owner without
    stealing focus, once per page version. On macOS with cmux it lands in an
    unselected cmux browser tab (no app is activated); otherwise `open -g`,
    except that a Chrome-family default browser is skipped, because it raises
    itself. Returns {opened, path, line, via?, reason?}; `line` is the one line
    to tell the owner ("Opened <path>")."""
    import review_surface
    raw = (target or "").strip().strip("<>")
    remote = raw.startswith(("http://", "https://"))
    if remote:
        path = Path(raw)
    else:
        path = _local_target(target)
        if not path.is_absolute():
            path = repo_root(cwd) / path
        path = review_surface.resolve_local_path(path, "resolve", task_id)
    shown = raw if remote else str(path)
    out = {"opened": False, "path": shown}
    if not remote and not path.exists():
        return {**out, "reason": "not found", "line": f"Not found: {path}"}
    if os.environ.get("IMAGE_COMPARE_OPEN_DISABLED") == "1":
        return {**out, "reason": "disabled", "line": f"Page: {shown}"}
    main = main_root(repo_root(cwd))
    state_file = _opened_state(main)
    if remote:
        key, version, now = raw, raw, time.time()
    else:
        key, version, now = _open_key(path, main), _open_version(path), time.time()
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    seen = state.get(key) or {}
    if not force and seen.get("version") == version and now - float(seen.get("at") or 0) < OPEN_DEDUPE_SECONDS:
        return {**out, "reason": "already opened", "line": f"Already open: {shown}"}

    opener = os.environ.get("PM_OS_OPEN_CMD", "").strip()
    is_page = remote or path.suffix.lower() in (".html", ".htm")
    before = _frontmost_bundle() if sys.platform == "darwin" else None
    record: dict = {"version": version, "at": now}
    via = ""
    exe = None if opener else _cmux_bin()
    if not opener and not exe and sys.platform != "darwin" and os.environ.get("PM_OS_OPEN_VIA") == "wmux":
        ok, err = _open_in_wmux(raw if remote else file_url(path))
        _log_open(main, {"path": shown, "via": "wmux", "opened": ok, **({"reason": err} if err else {})})
        if not ok:
            return {**out, "reason": err, "line": f"Page: {shown}"}
        via = "wmux"
    elif exe:
        workspace = _cmux_target_workspace(exe, main)
        if not workspace:
            _log_open(main, {"path": shown, "via": "cmux", "opened": False, "reason": "no cmux workspace"})
            return {**out, "reason": "no cmux workspace to hold the tab", "line": f"Page: {shown}"}
        url = raw if remote else file_url(_view_copy(path, main, key))
        surface, err = _open_in_cmux(exe, url, workspace, seen.get("surface"))
        if not surface:
            _log_open(main, {"path": shown, "via": "cmux", "opened": False, "reason": err})
            return {**out, "reason": err, "line": f"Page: {shown}"}
        via, record["surface"] = "cmux", surface
        out.update(workspace=workspace, surface=surface)
    else:
        cmd = [*opener.split(), shown] if opener else _default_opener(raw if remote else path)
        if cmd is None:
            return {**out, "reason": "no opener on this machine", "line": f"Page: {shown}"}
        handler = _macos_html_handler() if (not opener and sys.platform == "darwin" and is_page) else None
        raiser = handler if handler and handler.lower() in CHROME_FAMILY and os.environ.get("PM_OS_OPEN_CHROME") != "1" else None
        if not opener and not raiser:
            raiser = _offmac_chromium(remote, path)
        if raiser:
            reason = ("default browser unknown" if raiser == "unknown" else f"default browser {raiser} raises itself on open") \
                + "; link left in the message so focus stays put"
            _log_open(main, {"path": shown, "via": "browser", "opened": False, "reason": reason, "front": before})
            return {**out, "reason": reason, "line": f"Page: {shown}"}
        cold = None if opener else _browser_cold(path)
        if cold:
            return {**out, "reason": f"browser not running ({cold}); not launched so focus stays put", "line": f"Page: {shown}"}
        guard = bool(handler) and os.environ.get("PM_OS_FOCUS_GUARD_DISABLED") != "1"
        try:
            if not cmd:  # Windows: hand the page to its default app (no exit status to read)
                _startfile_background(raw if remote else path)
                result = subprocess.CompletedProcess([], 0, "", "")
            else:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {**out, "reason": f"open failed: {exc}", "line": f"Page: {shown}"}
        if result.returncode != 0:
            return {**out, "reason": f"open exited {result.returncode}: {result.stderr.strip()[:200]}", "line": f"Page: {shown}"}
        restored = _guard_focus(before, handler) if guard else None
        if restored:
            out["focus_restored"] = restored
        via = "command" if opener else "browser"
    out["via"] = via
    after = _frontmost_bundle() if sys.platform == "darwin" and not opener else None
    if via != "command":
        _log_open(main, {"path": shown, "via": via, "opened": True, "front_before": before, "front_after": after,
                         **({"surface": record["surface"]} if "surface" in record else {}),
                         **({"focus_restored": out["focus_restored"]} if "focus_restored" in out else {})})
    state = {k: v for k, v in state.items() if now - float((v or {}).get("at") or 0) < 7 * 86400}
    state[key] = record
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(state_file)
    except OSError:
        pass
    where = {"cmux": " in a background cmux tab", "wmux": " in the wmux browser panel"}.get(via, "")
    return {**out, "opened": True, "line": f"Opened {shown}{where}"}


def open_report_pages(text: str, *, task_id: str | None = None, limit: int = 3) -> dict:
    """Open the compare pages a delivered report links (report-to-cos.sh)."""
    import review_surface
    blob = review_surface._FENCED_BLOCK.sub(" ", review_surface._LINE_MARK.sub("\n", text or ""))
    targets: list[str] = []
    for pattern in (review_surface._COMPARE_URL, review_surface._COMPARE_PATH):
        for m in pattern.finditer(blob):
            raw = (m.groupdict().get("path") or m.group(0)).rstrip(".,;:")
            if "/abs/" in raw or raw.endswith("/abs"):
                continue
            key = str(review_surface.resolve_local_path(_local_target(raw), "resolve", task_id))
            if key not in targets:
                targets.append(key)
    pages = [open_page(t, task_id=task_id) for t in targets[:limit]]
    return {"opened": any(p["opened"] for p in pages), "pages": pages}


# --- report backstop -----------------------------------------------------------

def from_report(text: str, *, workstream: str = "", task_id: str = "", title: str = "",
                cwd: Path | None = None) -> dict:
    """Build a compare page for a report that names MIN_AUTO_IMAGES or more
    local images and carries no compare-page link (report-to-cos.sh)."""
    import review_surface
    if review_surface.has_compare_link(text):
        return {"built": False, "reason": "compare page present"}
    named = review_surface.report_image_refs(text)
    if len(named) < MIN_AUTO_IMAGES:
        return {"built": False, "reason": f"{len(named)} image(s) named"}
    top = repo_root(cwd)
    main = main_root(top)
    images: list[Image] = []
    missing: list[str] = []
    for raw, label in named:
        found, on_main = resolve_image(raw, top, main)
        if found.exists() or on_main.exists():
            images.append(Image(str(found if found.exists() else on_main), label))
        else:
            missing.append(raw)
    if len(images) < MIN_AUTO_IMAGES:
        return {"built": False, "reason": "images not found", "missing": missing,
                "warning": compare_warning_text([Path(p).name for p, _ in named])}
    name = (workstream or "report").replace("-", " ").strip().title()
    page = Page(title=title or f"{name} images", workstream=workstream,
                slug=f"{task_id or 'report'}-images",
                subtitle=(f"From the {name} report" + (f" ({task_id})" if task_id else "")
                          + ". The per-image links in the report open each file on its own."))
    page.groups = [Group(images=[img]) for img in images]
    result = build(page, cwd=cwd)
    return {"built": True, **result}


def compare_warning_text(names: list[str]) -> str:
    import review_surface
    return review_surface.compare_warning_text(names)


# --- CLI -----------------------------------------------------------------------

class _Ordered(argparse.Action):
    """--group / --image / --caveat / --text keep their command-line order, so
    an --image or --caveat after a --group belongs to that group."""

    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, "items", None) or []
        items.append((self.dest, values))
        namespace.items = items


def page_from_args(args) -> Page:
    spec = json.loads(Path(args.spec).read_text()) if args.spec else {}
    page = page_from_spec(spec) if spec else Page(title="Images")
    if args.title:
        page.title = args.title
    if args.subtitle:
        page.subtitle = args.subtitle
    if args.workstream:
        page.workstream = args.workstream
    if args.slug:
        page.slug = args.slug
    group: Group | None = None
    for kind, value in getattr(args, "items", None) or []:
        if kind == "group":
            name, _, badge = value.partition("::")
            group = Group(name=name.strip(), badge=badge.strip())
            page.groups.append(group)
        elif kind == "image":
            img = _images([value])[0]
            if group is None:
                page.groups.append(Group(images=[img]))
            else:
                group.images.append(img)
        elif kind == "caveat":
            (group.caveats if group else page.caveats).append(value)
        elif kind == "text":
            (group.texts if group else page.texts).extend(_texts([value]))
    return page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="write one comparison page")
    b.add_argument("--spec", help="JSON spec file (see SPEC in the module docstring)")
    b.add_argument("--title", default="")
    b.add_argument("--subtitle", default="")
    b.add_argument("--workstream", default="", help="owning workstream slug; the page goes in its assets/compare/")
    b.add_argument("--slug", default="", help="file name slug (default: from the title)")
    b.add_argument("--out", default="", help="explicit output path")
    b.add_argument("--group", dest="group", action=_Ordered, help="start a group card: NAME[::BADGE]")
    b.add_argument("--image", dest="image", action=_Ordered, help="PATH[::LABEL]")
    b.add_argument("--caveat", dest="caveat", action=_Ordered, help="caveat line (on the current group, else the page)")
    b.add_argument("--text", dest="text", action=_Ordered, help="LABEL::VERBATIM TEXT block")
    b.add_argument("--embed", action="store_true", help="inline the image bytes (page survives files moving)")
    b.add_argument("--print", dest="print_", choices=("json", "link", "url"), default="json")
    b.add_argument("--open", dest="open_", action="store_true", default=None,
                   help="open the page on the owner's Mac (default when run from a terminal)")
    b.add_argument("--no-open", dest="open_", action="store_false", help="do not open the page")
    o = sub.add_parser("open", help="open a local page or image on the owner's Mac, once per version")
    o.add_argument("target", help="path or file:// URL; a missing main path falls back to the live worktree copy")
    o.add_argument("--force", action="store_true", help="reopen even if opened in the last 10 minutes")
    o.add_argument("--task-id", default="")
    orp = sub.add_parser("open-report", help="open the compare pages a delivered report links")
    orp.add_argument("--text", required=True)
    orp.add_argument("--task-id", default="")
    r = sub.add_parser("from-report", help="report-to-cos backstop: build a page for a report's images")
    r.add_argument("--text", required=True)
    r.add_argument("--workstream", default="")
    r.add_argument("--task-id", default="")
    r.add_argument("--title", default="")
    args = parser.parse_args(argv)

    if args.cmd == "build":
        page = page_from_args(args)
        if not page.all_images():
            print("error: no images (use --image PATH[::LABEL] or --spec)", file=sys.stderr)
            return 2
        result = build(page, out=Path(args.out) if args.out else None, embed=args.embed)
        missing = [r["found"] for r in result["images"] if not r["exists"]]
        if missing:
            print(f"warning: image not found: {', '.join(missing)}", file=sys.stderr)
        want_open = args.open_ if args.open_ is not None else (sys.stdin.isatty() and sys.stdout.isatty())
        result["opened"] = open_page(result["path"])["opened"] if want_open else False
        if result["opened"]:
            print(f"Opened {result['path']}", file=sys.stderr)
        print({"json": json.dumps(result, ensure_ascii=False), "link": result["link"],
               "url": result["file_url"]}[args.print_])
        return 0
    if args.cmd == "open":
        result = open_page(args.target, force=args.force, task_id=args.task_id or None)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["opened"] or result.get("reason") in {"already opened", "disabled", "not macOS"} else 1
    if args.cmd == "open-report":
        print(json.dumps(open_report_pages(args.text, task_id=args.task_id or None), ensure_ascii=False))
        return 0
    if os.environ.get("IMAGE_COMPARE_DISABLED") == "1":
        print(json.dumps({"built": False, "reason": "disabled"}))
        return 0
    print(json.dumps(from_report(args.text, workstream=args.workstream, task_id=args.task_id, title=args.title),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
