#!/usr/bin/env python3
"""Build a shareable copy of the working checkout: one source of truth, no hand-kept copies.

The working checkout stays on this machine. A target in `system/kit/tiers.yaml` (for example
`public`) is a filtered copy of it. Rules that name a company term or
the owner live in owner-local files under `local/kit/` (gitignored), so this script and its config
ship without naming anyone. Runbook: system/kit/README.md.

    python3 system/kit/build_tier.py build --tier public --out <clone> [--source <repo>] [--commit]
    python3 system/kit/build_tier.py gate --tier public --tree <dir>
    python3 system/kit/build_tier.py push --tier public --out <clone>

How a build works (each step is pure until the last one writes into --out):
  1. Filter the source at the last built revision (base) and at the new one (theirs): tracked files,
     minus the tier's exclude globs, private workstream data and the owner-local excludes, plus
     owner-local folders mapped in (`include_local`), with the owner-local rewrites applied.
  2. Merge base -> theirs into the tier's last published tree (ours) per file, three ways. A hand edit
     made for the tier survives every later build. A conflict stops the build and names the file.
  3. Copy the tier overlay over the result (the tier's own README, license, docs).
  4. Gate it: the deny lists, the private identifier guard, the public detectors and gitleaks, as
     the tier lists. A file under a `drop_on_hit` glob that fails the deny gate is left out and
     listed; any other failure stops the build. A stopped build writes nothing into --out.
  5. Write the tree into --out, record the source revision in `.pm-os-kit/tier.json`, and commit
     when --commit is given. Pushing is its own command and never forces.

Globs use fnmatch, where `*` also crosses `/` (as in check-profile-leaks.py).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml  # PyYAML ships with every PM OS install
except ImportError:  # pragma: no cover
    yaml = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scan_public import BINARY_OK, decode_for_scan, line_hits  # noqa: E402

TIER_RECORD = ".pm-os-kit/tier.json"  # older builds kept their record in the tree; it never ships now
KNOWN_GATES = {"deny", "identifiers", "public", "secrets"}
SKIP_GATES: set[str] = set()  # --skip-gate, for fix-up passes only; a build that skips a gate never commits
BINARY_SNIFF = 8192


# ---------------------------------------------------------------- config and rules

@dataclass
class Rules:
    """Owner-local rules for one tier (local/kit/tier<N>.rules). One rule per line, `#` comments.

      exclude <glob>                 leave the path out of the tier
      drop_on_hit <glob>             a file here that fails the deny gate is dropped, not fatal
      rewrite <regex> => <text>      applied to every text file of the filtered source (base and theirs)
      rewrite_in <glob> <regex> => <text>   the same, only in files matching <glob> (no spaces in the glob)
      deny <category> <regex>        an extra deny term for this tier's gate
      deny_list <path>               another deny list file in check-profile-leaks format
      skip_category <category>       deny-list categories this tier allows (for example a private tier that keeps your name)
      rename <path> => <path>        the file ships under another name (applied before rewrites)
      keep <glob>                    ship the path even when an exclude glob covers it
    """
    exclude: list[str] = field(default_factory=list)
    drop_on_hit: list[str] = field(default_factory=list)
    rewrites: list[tuple[re.Pattern, str]] = field(default_factory=list)
    scoped_rewrites: list[tuple[str, re.Pattern, str]] = field(default_factory=list)
    deny: list[tuple[str, re.Pattern]] = field(default_factory=list)
    deny_lists: list[Path] = field(default_factory=list)
    skip_categories: set[str] = field(default_factory=set)
    renames: dict[str, str] = field(default_factory=dict)
    keep: list[str] = field(default_factory=list)
    source: str = ""


class RulesError(ValueError):
    pass


def parse_rules(text: str, base_dir: Path, source: str = "") -> Rules:
    rules = Rules(source=source)
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, rest = line.partition(" ")
        rest = rest.strip()
        try:
            if kind == "exclude":
                rules.exclude.append(rest)
            elif kind == "drop_on_hit":
                rules.drop_on_hit.append(rest)
            elif kind == "rewrite":
                parts = re.split(r"\s=>(?:\s|$)", rest, maxsplit=1)
                pattern, sep, repl = (parts[0], "=>", parts[1]) if len(parts) == 2 else (rest, "", "")
                if not sep:
                    raise RulesError(f"{source}:{no}: rewrite wants '<regex> => <text>'")
                rules.rewrites.append((re.compile(pattern), repl))
            elif kind == "rewrite_in":
                glob, _, body = rest.partition(" ")
                parts = re.split(r"\s=>(?:\s|$)", body.strip(), maxsplit=1)
                if not glob or len(parts) != 2:
                    raise RulesError(f"{source}:{no}: rewrite_in wants '<glob> <regex> => <text>'")
                rules.scoped_rewrites.append((glob, re.compile(parts[0]), parts[1]))
            elif kind == "deny":
                category, _, rx = rest.partition(" ")
                rules.deny.append((category, re.compile(rx.strip())))
            elif kind == "deny_list":
                path = Path(rest).expanduser()
                rules.deny_lists.append(path if path.is_absolute() else base_dir / path)
            elif kind == "rename":
                src, sep, dest = rest.partition(" => ")
                if not sep:
                    raise RulesError(f"{source}:{no}: rename wants '<path> => <path>'")
                rules.renames[src.strip()] = dest.strip()
            elif kind == "keep":
                rules.keep.append(rest)
            elif kind == "skip_category":
                rules.skip_categories.update(rest.split())
            else:
                raise RulesError(f"{source}:{no}: unknown rule kind {kind!r}")
        except re.error as exc:
            raise RulesError(f"{source}:{no}: bad regex: {exc}") from exc
    return rules


def load_config(path: Path | None = None) -> dict:
    path = path or HERE / "tiers.yaml"
    if yaml is None:
        raise SystemExit("PyYAML is required: python3 -m pip install pyyaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def owner_root(start: Path) -> Path:
    """The folder that holds local/: the main checkout, even when run from a worktree (local/ is a link)."""
    out = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else start


def merge_rules(a: Rules, b: Rules) -> Rules:
    return Rules(exclude=a.exclude + b.exclude, drop_on_hit=a.drop_on_hit + b.drop_on_hit,
                 rewrites=a.rewrites + b.rewrites, scoped_rewrites=a.scoped_rewrites + b.scoped_rewrites, deny=a.deny + b.deny, deny_lists=a.deny_lists + b.deny_lists,
                 skip_categories=a.skip_categories | b.skip_categories, renames={**a.renames, **b.renames},
                 keep=a.keep + b.keep, source=";".join(x for x in (a.source, b.source) if x))


def load_tier_rules(cfg: dict, tier: str, root: Path, repo: Path | None = None) -> Rules:
    """The owner-local rules (local/kit/tier<N>.rules) first, then the repo-owned extra rules."""
    tier_cfg = cfg["tiers"][tier]
    rules = Rules()
    rel = tier_cfg.get("rules")
    if rel:
        path = root / rel
        if path.is_file():
            rules = parse_rules(path.read_text(encoding="utf-8"), root, str(path))
        elif tier_cfg.get("rules_required"):
            raise SystemExit(f"owner-local rules not found: {path}; a tier {tier} build never runs without them")
    extra = tier_cfg.get("rules_extra")
    if extra:
        path = (repo or HERE.parents[1]) / extra
        if path.is_file():
            rules = merge_rules(rules, parse_rules(path.read_text(encoding="utf-8"), root, str(path)))
    return rules


# ---------------------------------------------------------------- trees

Tree = dict[str, bytes]


def matches(rel: str, globs: list[str]) -> bool:
    """fnmatch, plus a glob ending in "/" names a whole folder. Case-insensitive on every OS."""
    low = rel.casefold()
    return any(fnmatch.fnmatchcase(low, g.casefold()) or (g.endswith("/") and low.startswith(g.casefold()))
               for g in globs)


def is_text(data: bytes) -> bool:
    if b"\0" in data[:BINARY_SNIFF]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def git_tree(repo: Path, rev: str) -> Tree:
    """Every tracked file at `rev`, read through git archive (no checkout, no hooks)."""
    raw = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", rev], capture_output=True, check=True).stdout
    tree: Tree = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                tree[member.name] = tar.extractfile(member).read()
    return tree


def dir_tree(root: Path) -> Tree:
    tree: Tree = {}
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.relative_to(root).parts:
            tree[path.relative_to(root).as_posix()] = path.read_bytes()
    return tree


def worktree_tree(repo: Path) -> Tree:
    """Tracked plus untracked-but-not-ignored files as they sit on disk (an unlanded task branch)."""
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "-co", "-z", "--exclude-standard"],
                         capture_output=True, check=True).stdout.decode("utf-8", "replace")
    tree: Tree = {}
    for rel in filter(None, out.split("\0")):
        path = repo / rel
        if path.is_file() and not path.is_symlink():
            tree[rel] = path.read_bytes()
    return tree


def read_source(source: Path, rev: str | None) -> Tree:
    """rev WORKTREE reads the working tree; any other rev reads git; no rev walks a plain folder."""
    if rev == "WORKTREE":
        return worktree_tree(source)
    if rev:
        return git_tree(source, rev)
    if (source / ".git").exists():
        return git_tree(source, "HEAD")
    return dir_tree(source)


def private_workstreams(tree: Tree) -> list[str]:
    raw = tree.get("system/config/private-workstreams.txt", b"").decode("utf-8", "replace")
    return [s.strip() for s in raw.splitlines() if s.strip() and not s.lstrip().startswith("#")]


def filter_tree(tree: Tree, tier_cfg: dict, rules: Rules, local_root: Path | None = None) -> Tree:
    """Step 1: the tier's view of a source tree."""
    exclude = list(tier_cfg.get("exclude") or []) + rules.exclude
    keep_paths = list(tier_cfg.get("keep") or []) + rules.keep
    if rules.renames:
        tree = {rules.renames.get(rel, rel): data for rel, data in tree.items()}
    keep = set(tier_cfg.get("private_workstream_keep") or [])
    private = [f"workstreams/{slug}/" for slug in private_workstreams(tree)] if tier_cfg.get("exclude_private_workstreams") else []
    private += [p.replace("workstreams/", "workstreams/_archive/", 1) for p in private]
    out: Tree = {}
    for rel, data in tree.items():
        if matches(rel, exclude) and not matches(rel, keep_paths):
            continue
        folder = next((p for p in private if rel.startswith(p)), None)
        if folder and rel[len(folder):] not in keep:
            continue  # private workstream data: only its tracked status files may travel
        out[rel] = data
    for src_rel, dest_rel in (tier_cfg.get("include_local") or {}).items():
        folder = (local_root / src_rel) if local_root else None
        if not folder or not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                rel = dest_rel.rstrip("/") + "/" + path.relative_to(folder).as_posix()
                if not matches(rel, exclude):
                    out[rel] = path.read_bytes()
    if rules.rewrites or rules.scoped_rewrites:
        skip = tier_cfg.get("rewrite_skip", ["system/vendor/"])  # third-party text is never rewritten
        for rel, data in list(out.items()):
            if is_text(data) and not matches(rel, skip):
                text = data.decode("utf-8")
                for glob, rx, repl in rules.scoped_rewrites:
                    if matches(rel, [glob]):
                        text = rx.sub(repl, text)
                for rx, repl in rules.rewrites:
                    text = rx.sub(repl, text)
                out[rel] = text.encode("utf-8")
    return out


# ---------------------------------------------------------------- merge

@dataclass
class MergeReport:
    clean: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    upstream_change_to_removed: list[str] = field(default_factory=list)
    settled_for_source: list[str] = field(default_factory=list)
    settled_for_tier: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: sorted(v) for k, v in self.__dict__.items()}


def merge_text(base: bytes, ours: bytes, theirs: bytes, prefer: str = "") -> tuple[bytes, bool]:
    """git merge-file. prefer="source" or "tier" settles each conflicting hunk with that side; the rest still merges."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = Path(tmp) / name
            p.write_bytes(data)
            paths.append(str(p))
        flags = {"source": ["--theirs"], "tier": ["--ours"]}.get(prefer, [])
        out = subprocess.run(["git", "merge-file", "-p", *flags, "-L", "tier", "-L", "base", "-L", "source", *paths],
                             capture_output=True)
        return out.stdout, out.returncode == 0


def merge_trees(base: Tree, ours: Tree, theirs: Tree, prefer: str = "",
                tier_globs: list[str] | None = None) -> tuple[Tree, MergeReport]:
    """Step 2: carry the source's change (base -> theirs) into the tier's tree (ours)."""
    result: Tree = {}
    rep = MergeReport()
    for rel in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(rel), ours.get(rel), theirs.get(rel)
        if o is not None and t is not None:
            if o == t:
                result[rel] = o
            elif b is None:
                rep.conflicts.append(rel)  # added on both sides with different content
                result[rel] = o
            elif t == b:
                result[rel] = o
            elif o == b:
                result[rel] = t
                rep.clean.append(rel)
            elif is_text(b) and is_text(o) and is_text(t):
                side = "tier" if tier_globs and matches(rel, tier_globs) else prefer
                merged, ok = merge_text(b, o, t, side)
                result[rel] = merged if ok else o
                (rep.clean if ok else rep.conflicts).append(rel)
                if ok and side in ("source", "tier") and merge_text(b, o, t)[1] is False:
                    (rep.settled_for_source if side == "source" else rep.settled_for_tier).append(rel)
            else:
                rep.conflicts.append(rel)
                result[rel] = o
        elif o is not None:  # gone from the source, or a tier-only file
            if b is None:
                result[rel] = o  # the tier's own file
            elif o == b:
                rep.deleted.append(rel)  # the source deleted it and the tier never edited it
            else:
                rep.conflicts.append(rel)  # the source deleted a file the tier edited
                result[rel] = o
        elif t is not None:
            if b is None:
                result[rel] = t
                rep.added.append(rel)
            elif t != b:
                rep.upstream_change_to_removed.append(rel)  # the tier removed it on purpose; it stays out
    return result, rep


def apply_overlay(tree: Tree, source: Tree, overlay_dirs: list[str]) -> list[str]:
    """Step 3: every file under an overlay folder of the source lands at the same path under the root."""
    written = []
    for folder in overlay_dirs:
        prefix = folder.rstrip("/") + "/"
        for rel, data in source.items():
            if rel.startswith(prefix):
                dest = rel[len(prefix):]
                tree[dest] = data
                written.append(dest)
    return written


# ---------------------------------------------------------------- gates

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SCRIPTS = HERE.parents[1] / "system" / "scripts"  # the helpers ship beside this engine


def leak_module(root: Path | None = None):
    return _load_module("pm_os_check_profile_leaks", SCRIPTS / "check-profile-leaks.py")


def deny_hits(tree: Tree, deny_lists: list[Path], extra: list[tuple[str, re.Pattern]], skip: set[str],
              root: Path, skip_globs: list[str] | None = None) -> list[dict]:
    """Every deny-list hit in the tree: content rules and `path` rules. One hit per line."""
    leaks = leak_module(root)
    rules: list[tuple[str, re.Pattern]] = []
    paths: list[tuple[str, str]] = []
    allow = []
    for path in deny_lists:
        deny = leaks.load_deny_list(path)
        rules += [r for r in deny.terms + deny.kit if r[0] not in skip]
        paths += [p for p in deny.paths if p[0] not in skip]
        allow.append(deny)
    rules += [r for r in extra if r[0] not in skip]
    hits: list[dict] = []
    for rel, data in sorted(tree.items()):
        if skip_globs and matches(rel, skip_globs):
            continue
        denied = next((c for c, g in paths if fnmatch.fnmatch(rel, g)), None)
        if denied is not None:
            hits.append({"file": rel, "line": 0, "category": denied, "kind": "path"})
            continue
        for category, rx in rules:
            m = rx.search(rel)
            if m and not any(d.allowed(rel, rel, m.group(0)) for d in allow):
                hits.append({"file": rel, "line": 0, "category": category, "kind": "path-name"})
                break
        text = decode_for_scan(data)
        if text is None:
            if not rel.lower().endswith(BINARY_OK):
                hits.append({"file": rel, "line": 0, "category": "BIN", "kind": "binary-unscanned"})
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for category, rx in rules:
                if any(not any(d.allowed(rel, line, m.group(0)) for d in allow) for m in rx.finditer(line)):
                    hits.append({"file": rel, "line": no, "category": category, "kind": "term"})
                    break
    return hits


def identifier_hits(tree: Tree, root: Path) -> tuple[str, list[dict]]:
    """The private identifier guard over every file, binary included. Never returns a value."""
    pid = _load_module("pm_os_private_identifiers", SCRIPTS / "private_identifiers.py")
    try:
        listed = pid.load_list(root)
    except Exception as exc:  # an unreadable list fails closed
        return "error", [{"file": "(list)", "kind": f"unreadable: {type(exc).__name__}"}]
    if listed is None:
        return "absent", []
    hits = []
    for rel, data in sorted(tree.items()):
        for kind in sorted(listed.kinds_in(rel)):
            hits.append({"file": "(path withheld)", "kind": kind})
        text = decode_for_scan(data)
        if text is None:
            text = data.decode("latin-1")
        for line in text.splitlines():
            for kind in sorted(listed.kinds_in(line)):
                hits.append({"file": rel, "kind": kind})
    return "checked", hits


def write_tree(tree: Tree, out: Path) -> None:
    for rel, data in tree.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def gitleaks_hits(tree: Tree, config: Path | None = None) -> tuple[str, list[dict]]:
    exe = os.environ.get("PM_OS_GITLEAKS") or shutil.which("gitleaks")
    if not exe:
        return "missing", []
    if config is not None and not config.is_file():
        return "error", [{"file": "(gitleaks)", "kind": "configured gitleaks config is missing"}]
    with tempfile.TemporaryDirectory() as tmp:
        scan = {rel: data for rel, data in tree.items()
                if Path(rel).name not in (".gitleaksignore", ".gitleaks.toml", ".gitleaksbaseline")}
        write_tree(scan, Path(tmp) / "t")
        (Path(tmp) / "ignore").mkdir()
        report = Path(tmp) / "r.json"
        cmd = [exe, "dir", str(Path(tmp) / "t"), "--no-banner", "--redact", "--report-format", "json",
               "--report-path", str(report), "--exit-code", "0", "--ignore-gitleaks-allow",
               "--gitleaks-ignore-path", str(Path(tmp) / "ignore")]
        if config is not None:
            cmd += ["--config", str(config)]
        run = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp)
        if run.returncode != 0 or not report.exists():
            return "error", [{"file": "(gitleaks)", "kind": "gitleaks failed"}]
        found = json.loads(report.read_text(encoding="utf-8") or "[]")
    return "checked", [{"file": str(f.get("File", "")).replace("\\", "/").split("/t/", 1)[-1], "line": f.get("StartLine"),
                        "kind": f.get("RuleID")} for f in found]


def run_gates(tree: Tree, tier: str, cfg: dict, rules: Rules, root: Path, meta: str = "") -> dict:
    """Step 4. Returns {passed, dropped, gates: {name: {status, hits}}}; never prints a matched value.
    `meta` is text that ships outside the tree (the commit message, author, name) and is gated too."""
    tier_cfg = cfg["tiers"][tier]
    configured = list(tier_cfg.get("gates") or [])
    result: dict = {"passed": True, "dropped": [], "gates": {}}
    unknown = sorted(set(configured) - KNOWN_GATES)
    needs_all = bool(tier_cfg.get("visibility") or tier_cfg.get("remote_name") or tier_cfg.get("repo"))
    missing_gates = sorted(KNOWN_GATES - set(configured)) if needs_all else ([] if configured else ["(any)"])
    if unknown or missing_gates:
        result["gates"]["config"] = {"status": "error", "hits": [{"file": "(tiers.yaml)", "kind": f"unknown gate {g}"} for g in unknown]
                                     + [{"file": "(tiers.yaml)", "kind": f"missing gate {g}"} for g in missing_gates]}
        result["passed"] = False
    gates = [g for g in configured if g in KNOWN_GATES and g not in SKIP_GATES]
    deny_lists = [root / p for p in tier_cfg.get("deny_lists") or []] + rules.deny_lists
    meta_tree = {"(commit metadata)": meta.encode("utf-8")} if meta else {}
    drop_globs = list(tier_cfg.get("drop_on_hit") or []) + rules.drop_on_hit
    if "deny" in gates:
        missing = [str(p) for p in deny_lists if not p.is_file()]
        if missing or not deny_lists:
            result["gates"]["deny"] = {"status": "error", "hits": [{"file": m, "kind": "deny list missing"} for m in missing]
                                       or [{"file": "(none)", "kind": "no deny list configured"}]}
            result["passed"] = False
        else:
            hits = deny_hits({**tree, **meta_tree}, deny_lists, rules.deny, rules.skip_categories, root,
                             skip_globs=tier_cfg.get("deny_skip") or [])
            dropped = sorted({h["file"] for h in hits if matches(h["file"], drop_globs)})
            for rel in dropped:
                tree.pop(rel, None)
            fatal = [h for h in hits if h["file"] not in dropped]
            result["dropped"] = dropped
            result["gates"]["deny"] = {"status": "hit" if fatal else "clean", "hits": fatal}
            result["passed"] &= not fatal
    if "identifiers" in gates:
        status, hits = identifier_hits({**tree, **meta_tree}, root)
        ok = status in ("checked", "absent") and not hits
        if status == "absent" and tier_cfg.get("identifiers_required"):
            ok = False
        result["gates"]["identifiers"] = {"status": status if not hits else "hit", "hits": hits}
        result["passed"] &= ok
    if "public" in gates:
        scan = _load_module("pm_os_scan_public", HERE / "scan_public.py")
        hits = scan.scan_tree({**tree, **meta_tree}, allow=tier_cfg.get("public_allow") or {})
        result["gates"]["public"] = {"status": "hit" if hits else "clean", "hits": hits}
        result["passed"] &= not hits
    if "secrets" in gates:
        config = tier_cfg.get("gitleaks_config")
        status, hits = gitleaks_hits(tree, (HERE.parents[1] / config) if config else None)
        allowed = tier_cfg.get("secrets_allow") or []
        hits = [h for h in hits if not matches(h["file"], allowed)]
        required = tier_cfg.get("secrets_required", False)
        result["gates"]["secrets"] = {"status": status if not hits else "hit", "hits": hits}
        result["passed"] &= not hits and status != "error" and not (required and status == "missing")
    withhold_paths(result, tree, deny_lists, rules, root)
    return result


def withhold_paths(result: dict, tree: Tree, deny_lists: list[Path], rules: Rules, root: Path) -> None:
    """A path that itself matches a deny rule, the identifier list or a detector is reported by number only."""
    flagged: set[str] = set()
    for rel in tree:
        if line_hits(rel):
            flagged.add(rel)
    leaks_ok = all(p.is_file() for p in deny_lists)
    if leaks_ok and deny_lists:
        leaks = leak_module()
        lists = [leaks.load_deny_list(p) for p in deny_lists]
        rx_all = [rx for d in lists for c, rx in d.terms + d.kit if c not in rules.skip_categories]
        rx_all += [rx for c, rx in rules.deny if c not in rules.skip_categories]
        flagged |= {rel for rel in tree if any(rx.search(rel) for rx in rx_all)}
    try:
        pid = _load_module("pm_os_private_identifiers", SCRIPTS / "private_identifiers.py")
        listed = pid.load_list(root)
        if listed is not None:
            flagged |= {rel for rel in tree if listed.kinds_in(rel)}
    except Exception:
        pass
    alias = {rel: f"(path withheld #{i})" for i, rel in enumerate(sorted(flagged), 1)}
    for gate in result["gates"].values():
        for hit in gate["hits"]:
            if hit.get("file") in alias:
                hit["file"] = alias[hit["file"]]
    result["dropped"] = [alias.get(r, r) for r in result["dropped"]]
    result["withheld"] = len(alias)  # the count only: the map would print the paths


# ---------------------------------------------------------------- finalize

MANIFEST = ".pm-os-kit/source-manifest.json"


def finalize(tree: Tree, tier_cfg: dict, source_rev: str, overlaid: list[str]) -> None:
    """Line endings, the version stamp, and the manifest install.py verifies before it renders."""
    for rel, data in list(tree.items()):
        if matches(rel, tier_cfg.get("crlf") or []) and is_text(data):
            tree[rel] = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    stamp = tier_cfg.get("version_file")
    if stamp:
        day = _dt.datetime.now().strftime("%Y-%m-%d")
        tree[stamp] = f"{tier_cfg.get('name') or 'build'} {source_rev[:7]} {day}\n".encode("utf-8")
    if tier_cfg.get("manifest"):
        import hashlib
        tree.pop(MANIFEST, None)
        files = {rel: {"sha256": hashlib.sha256(data).hexdigest(), "source": "overlay" if rel in overlaid else "repo"}
                 for rel, data in sorted(tree.items()) if rel != TIER_RECORD}
        tree[MANIFEST] = (json.dumps({"count": len(files), "files": files}, indent=1) + "\n").encode("utf-8")


# ---------------------------------------------------------------- build

def record_path(root: Path, tier: str) -> Path:
    return root / "local" / "kit" / f"{tier}.record.json"


def clone_head(out: Path) -> str:
    run = subprocess.run(["git", "-C", str(out), "rev-parse", "--verify", "-q", "HEAD"], capture_output=True, text=True)
    return run.stdout.strip() if run.returncode == 0 else ""


def read_record(root: Path, tier: str, out: Path) -> dict:
    """The source revision of the last build, used only when that build's commit is the clone's HEAD or an
    ancestor of it (hand commits on top keep it valid; a record from another clone or a lost commit does not)."""
    try:
        rec = json.loads(record_path(root, tier).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    head = clone_head(out)
    if not rec.get("commit") or not head:
        return {}
    ancestor = subprocess.run(["git", "-C", str(out), "merge-base", "--is-ancestor", rec["commit"], head],
                              capture_output=True).returncode == 0
    return rec if ancestor else {}


def write_record(root: Path, tier: str, out: Path, source_rev: str) -> None:
    path = record_path(root, tier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tier": tier, "commit": clone_head(out), "source_rev": source_rev,
                                "built_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}, indent=1)
                    + "\n", encoding="utf-8", newline="\n")


def rules_snapshot_dir(root: Path, tier: str) -> Path:
    """The rules the last committed build used. Owner-local, never shipped."""
    return root / "local" / "kit" / f"{tier}.last-rules"


def save_rules_snapshot(root: Path, tier: str, cfg_path: Path, cfg: dict, repo: Path) -> None:
    snap = rules_snapshot_dir(root, tier)
    snap.mkdir(parents=True, exist_ok=True)
    tier_cfg = cfg["tiers"][tier]
    shutil.copyfile(cfg_path, snap / "tiers.yaml")
    for key, base_dir, name in (("rules", root, "owner.rules"), ("rules_extra", repo, "extra.rules")):
        rel = tier_cfg.get(key)
        src = base_dir / rel if rel else None
        if src and src.is_file():
            shutil.copyfile(src, snap / name)
        elif (snap / name).exists():
            (snap / name).unlink()


def load_rules_snapshot(snap: Path, tier: str, root: Path) -> tuple[dict, Rules]:
    """(tier config, rules) as the last build saw them, so a rule change reaches the base and merges like a source
    change. Without this, a new rule on an unchanged line would never reach the published copy."""
    tier_cfg = load_config(snap / "tiers.yaml")["tiers"][tier]
    rules = Rules()
    for name in ("owner.rules", "extra.rules"):
        if (snap / name).is_file():
            rules = merge_rules(rules, parse_rules((snap / name).read_text(encoding="utf-8"), root, str(snap / name)))
    return tier_cfg, rules


def safe_scratch(path: Path, out: Path, source: Path) -> bool:
    """A folder --keep-failed may empty: never the clone or the source, never a folder holding either, and inside
    the source only where git ignores it (a scratch folder)."""
    path = path.resolve()
    if inside(path, out):
        return False
    if path == source.resolve() or path in source.resolve().parents:
        return False
    if source.resolve() in path.parents:
        rel = path.relative_to(source.resolve()).as_posix() + "/"
        return subprocess.run(["git", "-C", str(source), "check-ignore", "-q", rel], capture_output=True).returncode == 0
    return True


def inside(a: Path, b: Path) -> bool:
    """True when a and b are the same folder or one holds the other."""
    a, b = a.resolve(), b.resolve()
    return a == b or a in b.parents or b in a.parents


def rev_parse(repo: Path, rev: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", rev], capture_output=True, text=True,
                          check=True).stdout.strip()


_T0 = [0.0]


def stage(name: str) -> None:
    import time
    now_ = time.monotonic()
    if _T0[0]:
        print(f"  [{name}: {now_ - _T0[0]:.0f}s]", file=sys.stderr)
    _T0[0] = now_


def build(args: argparse.Namespace) -> int:
    stage("start")
    SKIP_GATES.update(args.skip_gate or [])
    if SKIP_GATES:
        args.dry_run, args.commit = True, False
    cfg = load_config(Path(args.config) if args.config else None)
    tier = str(args.tier)
    tier_cfg = cfg["tiers"][tier]
    source = Path(args.source).resolve() if args.source else owner_root(Path.cwd())
    root = owner_root(Path(args.owner_root).resolve() if args.owner_root else Path.cwd())
    rules = load_tier_rules(cfg, tier, root)
    out = Path(args.out).resolve()
    if args.keep_failed and not safe_scratch(Path(args.keep_failed), out, source):
        print("refusing: --keep-failed must be a folder of its own: not --out, not --source, and not a tracked part of either",
              file=sys.stderr)
        return 2
    if (out / ".git").exists() and not args.ours_worktree and subprocess.run(
            ["git", "-C", str(out), "status", "--porcelain"], capture_output=True, text=True).stdout.strip():
        print(f"refusing: {out} has uncommitted changes; commit or remove them first", file=sys.stderr)
        return 2
    record = read_record(root, tier, out)
    new_rev = rev_parse(source, "HEAD") if args.rev == "WORKTREE" else rev_parse(source, args.rev)
    base_rev = args.base_rev or record.get("source_rev")
    local_root = owner_root(source) if tier_cfg.get("include_local") else None

    theirs_raw = read_source(source, "WORKTREE" if args.rev == "WORKTREE" else new_rev)
    theirs = filter_tree(theirs_raw, tier_cfg, rules, local_root)
    base_cfg, base_rules = tier_cfg, rules
    snap = Path(args.base_rules) if args.base_rules else rules_snapshot_dir(root, tier)
    if (snap / "tiers.yaml").is_file():
        base_cfg, base_rules = load_rules_snapshot(snap, tier, root)
    elif args.base_rules:
        print(f"refusing: --base-rules {snap} has no tiers.yaml", file=sys.stderr)
        return 2
    if args.bootstrap_base:
        base = filter_tree(read_source(Path(args.bootstrap_base), None), base_cfg, base_rules, local_root)
    elif base_rev:
        base = filter_tree(read_source(source, base_rev), base_cfg, base_rules, local_root)
    else:
        base = {}
    if args.bootstrap_ours:
        ours = read_source(Path(args.bootstrap_ours), None)
    elif (out / ".git").exists() and not args.ours_worktree:
        ours = git_tree(out, "HEAD") if clone_head(out) else {}  # an empty clone has no HEAD yet
    else:
        ours = dir_tree(out)
    ours.pop(TIER_RECORD, None)
    ours = filter_tree(ours, {k: v for k, v in tier_cfg.items() if k not in ("include_local",)},
                       Rules(exclude=rules.exclude, rewrites=base_rules.rewrites, scoped_rewrites=base_rules.scoped_rewrites,
                             keep=rules.keep), None)  # ours came from the base rules: filter it the same way
    seeded = []
    for glob_ in args.seed or []:  # first build: paths the tier above never shipped but this tier keeps
        for rel, data in base.items():
            if rel not in ours and matches(rel, [glob_]):
                ours[rel] = data
                seeded.append(rel)
    for glob_ in args.prefer_source or []:  # first build: the source's own copy wins over ours
        for rel in [r for r in set(base) | set(ours) if matches(r, [glob_])]:
            if rel in base:
                ours[rel] = base[rel]
            else:
                ours.pop(rel, None)
            seeded.append(rel)
    if not base and not args.allow_empty_base:
        base = dict(ours)  # first build with no history: everything new is an add, nothing is a delete
    stage("read and filter")
    tree, rep = merge_trees(base, ours, theirs, args.conflicts, args.conflicts_tier)
    stage("merge")
    overrides = dir_tree(Path(args.override)) if args.override else {}
    excluded = [rel for rel in overrides if matches(rel, list(tier_cfg.get("exclude") or []) + rules.exclude)
                and not matches(rel, list(tier_cfg.get("keep") or []) + rules.keep)]
    if excluded:
        print(f"refusing: {len(excluded)} override file(s) sit on paths this tier excludes", file=sys.stderr)
        return 2
    overrides = filter_tree(overrides, {"rewrite_skip": tier_cfg.get("rewrite_skip", ["system/vendor/"])},
                            Rules(rewrites=rules.rewrites, scoped_rewrites=rules.scoped_rewrites), None)  # the same rewrites, so a hand file cannot skip them
    for rel, data in overrides.items():
        tree[rel] = data  # a hand resolution or edit for this build; later builds keep it through ours
        if rel in rep.conflicts:
            rep.conflicts.remove(rel)
            rep.clean.append(rel)
    overlaid = apply_overlay(tree, theirs, tier_cfg.get("overlay") or [])
    for rel in [r for r in tree if matches(r, tier_cfg.get("drop_after_overlay") or [])]:
        tree.pop(rel)
    tree.pop(TIER_RECORD, None)
    if tier_cfg.get("adapter_indexes"):
        _load_module("adapter_indexes", HERE / "adapter_indexes.py").regenerate(tree)
    finalize(tree, tier_cfg, new_rev, overlaid)  # before the gates, so the stamp and manifest are gated too
    message = args.message or f"{tier_cfg.get('name') or tier}: build from {new_rev[:7]}"
    author = tier_cfg.get("author") or {}
    meta = "\n".join([message, str(tier_cfg.get("name") or ""), str(author.get("name") or ""), str(author.get("email") or "")])
    gates = run_gates(tree, tier, cfg, rules, root, meta=meta)
    if gates["dropped"]:
        finalize(tree, tier_cfg, new_rev, overlaid)  # a dropped file leaves the manifest too
    stage("gates")
    report = {"tier": tier, "source": str(source) if args.show_paths else source.name, "source_rev": new_rev,
              "base_rev": base_rev or ("bootstrap" if args.bootstrap_base else None), "files": len(tree),
              "seeded": len(seeded), "overlay": len(overlaid), "merge": rep.as_dict(), "gates": gates,
              "built_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    ok = gates["passed"] and not rep.conflicts
    report["result"] = "ok" if ok else ("conflicts" if rep.conflicts else "gate-failed")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1), encoding="utf-8")
    summary(report)
    if not ok or args.dry_run:
        if not ok and args.keep_failed:
            failed = Path(args.keep_failed)
            if failed.exists():
                shutil.rmtree(failed)
            write_tree(tree, failed)
        return 1 if not ok else 0
    sync_into(out, tree)
    if args.commit:
        rc = commit(out, tier_cfg, message)
        if rc == 0:
            write_record(root, tier, out, new_rev)
            save_rules_snapshot(root, tier, Path(args.config) if args.config else HERE / "tiers.yaml", cfg, source)
        return rc
    return 0


def sync_into(out: Path, tree: Tree) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(out.rglob("*"), reverse=True):
        rel = path.relative_to(out)
        if ".git" in rel.parts:
            continue
        if path.is_file() and rel.as_posix() not in tree:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    write_tree(tree, out)


def commit(out: Path, tier_cfg: dict, message: str) -> int:
    env = dict(os.environ, TZ="UTC")  # commit dates carry no local time zone
    author = tier_cfg.get("author")
    if author:  # a public tier never carries the builder's own git identity
        env.update({"GIT_AUTHOR_NAME": author["name"], "GIT_AUTHOR_EMAIL": author["email"],
                    "GIT_COMMITTER_NAME": author["name"], "GIT_COMMITTER_EMAIL": author["email"]})
    if not (out / ".git").exists():
        subprocess.run(["git", "-C", str(out), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(out), "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "add", "-A"],
                   check=True)
    if subprocess.run(["git", "-C", str(out), "diff", "--cached", "--quiet"]).returncode == 0:
        print("nothing to commit")
        return 0
    subprocess.run(["git", "-C", str(out), "-c", "core.hooksPath=", "commit", "-q", "-m", message], check=True, env=env)
    print("committed", rev_parse(out, "HEAD")[:10])
    return 0


def summary(report: dict) -> None:
    m = report["merge"]
    print(f"tier {report['tier']}: {report['result']}; {report['files']} files from {report['source_rev'][:10]} "
          f"(base {str(report['base_rev'])[:10]}); merged {len(m['clean'])}, added {len(m['added'])}, "
          f"deleted {len(m['deleted'])}, conflicts {len(m['conflicts'])}, seeded {report['seeded']}, "
          f"overlay {report['overlay']}, dropped {len(report['gates']['dropped'])}")
    for name, gate in report["gates"]["gates"].items():
        files = sorted({h.get("file", "?") for h in gate["hits"]})
        print(f"  gate {name}: {gate['status']} ({len(gate['hits'])} hits in {len(files)} files)")
        for rel in files[:15]:
            cats = sorted({str(h.get("category") or h.get("kind")) for h in gate["hits"] if h.get("file") == rel})
            print(f"    {rel} [{', '.join(cats)}]")
    for rel in m["conflicts"][:20]:
        print(f"  conflict {rel}")


def gate_cmd(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    tier = str(args.tier)
    root = owner_root(Path(args.owner_root).resolve() if args.owner_root else Path.cwd())
    rules = load_tier_rules(cfg, tier, root)
    tree = dir_tree(Path(args.tree))
    cfg["tiers"][tier]["drop_on_hit"] = []  # a gate run judges the tree as it is
    rules.drop_on_hit = []
    gates = run_gates(tree, tier, cfg, rules, root)
    report = {"tier": tier, "result": "ok" if gates["passed"] else "gate-failed", "files": len(tree),
              "source_rev": "-", "base_rev": "-", "seeded": 0, "overlay": 0,
              "merge": MergeReport().as_dict(), "gates": gates}
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1), encoding="utf-8")
    summary(report)
    return 0 if gates["passed"] else 1


def push_cmd(args: argparse.Namespace) -> int:
    """A normal push of main to the tier's configured repository. Never forces, never another branch, and
    the tree and every commit message being pushed pass the gates again first."""
    cfg = load_config(Path(args.config) if args.config else None)
    tier = str(args.tier)
    tier_cfg = cfg["tiers"][tier]
    out = Path(args.out).resolve()
    remote = tier_cfg.get("remote_name", "origin")
    urls = subprocess.run(["git", "-C", str(out), "remote", "get-url", "--push", "--all", remote],
                          capture_output=True, text=True)
    pushes = [u for u in urls.stdout.split() if u]
    if urls.returncode or len(pushes) != 1:
        print(f"refusing: remote {remote!r} must have exactly one push URL", file=sys.stderr)
        return 2
    url = pushes[0]
    local = "://" not in url and not url.startswith("git@")
    if args.skip_visibility_check and not local:
        print("refusing: --skip-visibility-check is only for a local test remote", file=sys.stderr)
        return 3
    if not local:
        slug = re.sub(r"^(?:https://|ssh://git@|git@)github\.com[/:]|\.git$", "", url)
        want = re.sub(r"^(?:https://)?github\.com/", "", str(tier_cfg.get("repo") or ""))
        if not want or slug.casefold() != want.casefold():
            print(f"refusing: the push URL is not the repository tiers.yaml names", file=sys.stderr)
            return 3
        view = subprocess.run(["gh", "repo", "view", slug, "--json", "visibility", "-q", ".visibility"],
                              capture_output=True, text=True)
        if view.returncode or view.stdout.strip().upper() != str(tier_cfg.get("visibility") or "").upper():
            print(f"refusing: {slug} visibility is {view.stdout.strip() or 'unknown'}, tier wants {tier_cfg.get('visibility')}",
                  file=sys.stderr)
            return 3
    branch = subprocess.run(["git", "-C", str(out), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
    if branch != "main":
        print(f"refusing: on {branch!r}, only main is pushed", file=sys.stderr)
        return 3
    if not args.skip_regate:
        root = owner_root(Path(args.owner_root).resolve() if args.owner_root else Path.cwd())
        rules = load_tier_rules(cfg, tier, root)
        upstream = subprocess.run(["git", "-C", str(out), "rev-parse", "--verify", "-q", f"{remote}/main"],
                                  capture_output=True, text=True).stdout.strip()
        span = f"{upstream}..main" if upstream else "main"
        messages = subprocess.run(["git", "-C", str(out), "log", "--format=%an%n%ae%n%cn%n%ce%n%B", span],
                                  capture_output=True, text=True, check=True).stdout
        tier_cfg["drop_on_hit"], rules.drop_on_hit = [], []
        gates = run_gates(git_tree(out, "main"), tier, cfg, rules, root, meta=messages)
        if not gates["passed"]:
            summary({"tier": tier, "result": "gate-failed", "files": 0, "source_rev": "-", "base_rev": "-",
                     "seeded": 0, "overlay": 0, "merge": MergeReport().as_dict(), "gates": gates})
            print("refusing: what would be pushed fails the gates", file=sys.stderr)
            return 4
    run = subprocess.run(["git", "-C", str(out), "push", remote, "refs/heads/main:refs/heads/main"],
                         capture_output=True, text=True)
    print((run.stdout + run.stderr).strip())
    return run.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--tier", required=True)
    b.add_argument("--out", required=True, help="the tier's clone (a git repo, or a new folder)")
    b.add_argument("--source", help="the tier above (default: this checkout's main folder)")
    b.add_argument("--rev", default="HEAD")
    b.add_argument("--base-rev", help="override the recorded source revision of the last build")
    b.add_argument("--base-rules", help="folder with the last build's tiers.yaml, owner.rules and extra.rules "
                                        "(default: local/kit/<tier>.last-rules, written by every committed build)")
    b.add_argument("--bootstrap-base", help="first build: the folder the tier above was built from")
    b.add_argument("--bootstrap-ours", help="first build: an existing tree to start the tier from")
    b.add_argument("--seed", action="append", help="first build: glob of base paths to keep that ours lacks")
    b.add_argument("--conflicts", choices=["stop", "source"], default="stop",
                   help="source: settle each conflicting text hunk with the source's side; the report lists the files")
    b.add_argument("--conflicts-tier", action="append", default=[],
                   help="glob whose conflicting hunks keep the tier's side (public-only edits win there)")
    b.add_argument("--override", help="folder of hand-resolved or hand-edited files that replace the merge result")
    b.add_argument("--prefer-source", action="append", help="first build: glob whose source copy replaces ours")
    b.add_argument("--ours-worktree", action="store_true", help="read --out from disk, not from its HEAD commit")
    b.add_argument("--allow-empty-base", action="store_true")
    b.add_argument("--owner-root", help="the folder that holds local/ (default: from the cwd)")
    b.add_argument("--report")
    b.add_argument("--keep-failed", help="write a failed build's tree here for inspection")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--commit", action="store_true")
    b.add_argument("--message")
    b.add_argument("--show-paths", action="store_true")
    b.add_argument("--skip-gate", action="append", default=[], help="fix-up passes only: implies --dry-run")
    g = sub.add_parser("gate")
    g.add_argument("--tier", required=True)
    g.add_argument("--tree", required=True)
    g.add_argument("--owner-root")
    g.add_argument("--report")
    p = sub.add_parser("push")
    p.add_argument("--tier", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--skip-visibility-check", action="store_true", help="tests only: a local remote")
    p.add_argument("--skip-regate", action="store_true", help="tests only: skip the gates before the push")
    p.add_argument("--owner-root")
    args = ap.parse_args(argv)
    return {"build": build, "gate": gate_cmd, "push": push_cmd}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
