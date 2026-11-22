#!/usr/bin/env python3
"""Copy manifest skills into ~/.claude/skills so Claude Code and Amp both read them.

Manifest: system/config/claude-skills-manifest.yaml. Each installed copy carries a
`.pm-os-source` file (origin, content hash, time) so `--check` can tell a source
update from a hand edit, and `--prune` only ever removes copies this script made.

Usage:
  install-claude-skills.py                 install or refresh every manifest entry
  install-claude-skills.py --check         report drift; exit 1 when any
  install-claude-skills.py --prune         remove managed copies not in the manifest
                                           (refused when more than 5 would go at once)
  install-claude-skills.py --dry-run       print the plan only
  install-claude-skills.py --skip-missing  treat every entry whose source is absent as dormant
                                           (one `·` line each, no failure); for kit installs
                                           where a source library is not present
  install-claude-skills.py vendor <name>   refresh system/vendor/skills/<name> from its pinned repo
  install-claude-skills.py vendor --all    refresh every vendor entry

An entry with `absent_ok_on: [win32]` is dormant on those platforms when its source
is absent, with no flag needed. On any other platform, and for any entry without the
field, a missing source still fails, so a skill that should exist cannot go quiet.

An entry with `only_on: [win32]` does not exist for any other platform: it is not
installed, not checked, and --prune removes a managed copy there.

A pattern entry with `machine_gate: <KEY>` is dormant unless <KEY> is on for this machine,
in the environment or in local/machines/<MACHINE>.env. The default is off.

A dormant pattern never counts as a delete. Copies that came from its source and match it
stay in place through --prune and pass --check, so a missing or empty source root or a
closed gate cannot remove anything.

A real folder at the destination with no marker and the same content as its source is
adopted: the marker is written and no other file changes. A folder that differs is still
refused without --force.

Copies are protected:
- An installed copy edited by hand is saved to gitignored local/skill-edits/<name>-<time>/
  before the source version replaces it, and before --prune removes it. Nothing edited is
  lost; move the wanted lines into the source.
- Each copy is built in ~/.claude/.skills-staging and swapped in with two renames, so one
  locked file fails that skill alone and never leaves a half-deleted folder.
- A link or junction at the destination is unlinked, never followed, so no run can delete
  a source folder. A destination inside a source root, or a source root inside it, is refused.
- Install and prune refuse to run from a task worktree against the real home: a worktree
  holds the branch's skills, which would replace main's. `sync-skills.py` always uses main.
- `--check` fails when a skill's folder name and its frontmatter `name` differ (Claude reads
  the folder, Amp the frontmatter), and lists unmanaged folders, ignoring synced/ and wmux/.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "system/config/claude-skills-manifest.yaml"
MARKER = ".pm-os-source"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".git")
SKIP_IN_HASH = {MARKER, ".DS_Store"}


LISTING_STATES = ("on", "name-only", "user-invocable-only", "off")
PLATFORM = sys.platform
GATE_ON_VALUES = {"on", "1", "true", "yes"}
# Folders in ~/.claude/skills that other installers own: Claude's account sync (synced/)
# and the wmux install (wmux/). Never managed, never reported as unmanaged.
FOREIGN_FOLDERS = {"synced", "wmux"}
PRUNE_CAP = 5
STAGING_MAX_AGE = 3600
NOTICES: list[dict] = []


def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)


def notice(kind: str, name: str, text: str, **extra) -> None:
    """One event worth a CoS line (sync-skills.py sends and dedupes them)."""
    NOTICES.append({"kind": kind, "name": name, "text": text, **extra})


def is_link(path: Path) -> bool:
    """A symlink or a Windows junction. Either is unlinked, never walked or deleted through."""
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def unlink_link(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        os.rmdir(path)  # a junction: rmdir removes the link, never its target


def is_linked_worktree(root: Path) -> bool:
    """A task worktree has a .git file pointing at main's git dir; main has a .git folder."""
    return (root / ".git").is_file()


def frontmatter_name(skill: Path) -> str | None:
    try:
        lines = (skill / "SKILL.md").read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip().strip("\"'")
    return None


def absent_ok(entry: dict, skip_missing: bool = False) -> bool:
    """True when this entry having no source is dormant rather than a failure."""
    return skip_missing or PLATFORM in (entry.get("absent_ok_on") or [])


def machine_setting(key: str) -> str:
    """The value from the environment, else this machine's local/machines/<MACHINE>.env.

    Same file and host rule as comms_triage_setting.py. A file a sync client carried from
    another machine has that machine's name, so it never switches this one on.
    """
    if os.environ.get(key):
        return os.environ[key]
    machine = (os.environ.get("PM_OS_MACHINE") or socket.gethostname()).split(".")[0].upper()
    try:
        lines = (ROOT / "local" / "machines" / f"{machine}.env").read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    value = ""
    for line in lines:
        name, sep, val = line.strip().partition("=")
        if sep and name.strip() == key:
            value = val.strip().strip("\"'")
    return value


def load_manifest(path: Path = MANIFEST, home: Path | None = None, skip_missing: bool = False) -> dict:
    """Read the manifest and expand `pattern:` entries into one entry per skill.

    A pattern entry (`pattern: "example-*"`, `source: agents`) stands for every
    directory under that source root whose name matches the glob and holds a
    SKILL.md. An explicit `name:` entry wins over a pattern match for the same
    name, so a family can be listed as `name-only` with a few named exceptions.

    With `skip_missing`, or when the entry's `absent_ok_on` names this platform, a
    pattern that matches nothing is recorded under `data["dormant_patterns"]` instead
    of stopping the run. A kit install on a machine without an organization skill catalog
    has no `example-*` skills; that is a dormant family, not an error. A `machine_gate`
    that is not on for this machine makes a pattern dormant without reading its root.
    """
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("skills", [])
    # A name that only_on leaves out on this platform stays out: a pattern over the same
    # source root must not bring it back (a skill listed only_on: [windows] stays off macOS).
    excluded = {e["name"] for e in data["skills"] if "name" in e and e.get("only_on") and PLATFORM not in e["only_on"]}
    data["skills"] = [e for e in data["skills"] if not e.get("only_on") or PLATFORM in e["only_on"]]
    # The organisation profile (apply-profile.py) may list extra skills, such as a
    # profile's own site builders, in claude-skills-manifest.profile.yaml.
    extra_path = path.with_name("claude-skills-manifest.profile.yaml")
    if extra_path.exists():
        extra = yaml.safe_load(extra_path.read_text()) or {}
        data["skills"] = list(data["skills"]) + list(extra.get("skills") or [])
    data.setdefault("sources", {})
    data.setdefault("destination", "~/.claude/skills")
    data.setdefault("claude_settings", {})
    home = home or Path.home()
    named = [e for e in data["skills"] if "name" in e]
    names = [entry["name"] for entry in named]
    foreign = sorted(set(names) & FOREIGN_FOLDERS)
    if foreign:
        raise SystemExit(f"manifest names a folder another installer owns: {', '.join(foreign)}")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise SystemExit(f"manifest lists a skill twice: {', '.join(duplicates)}")
    expanded: list[dict] = list(named)
    seen = set(names)
    data["dormant_patterns"] = []
    for entry in data["skills"]:
        if "pattern" not in entry:
            continue
        try:
            root = resolve(data["sources"][entry["source"]], home)
        except KeyError:
            raise SystemExit(f"pattern {entry['pattern']}: unknown source '{entry.get('source')}'")
        gate = entry.get("machine_gate")
        if gate and machine_setting(gate).strip().lower() not in GATE_ON_VALUES:
            data["dormant_patterns"].append({"pattern": entry["pattern"], "source": entry["source"],
                                             "why": f"{gate} is not on for this machine"})
            continue
        matches = sorted(p for p in root.glob(entry["pattern"]) if (p / "SKILL.md").exists())
        if not matches:
            if absent_ok(entry, skip_missing):
                data["dormant_patterns"].append({"pattern": entry["pattern"], "source": entry["source"],
                                                 "why": f"no source under {root}"})
                continue
            raise SystemExit(f"pattern {entry['pattern']}: no skill matches under {root}")
        for match in matches:
            if match.name in seen or match.name in excluded or match.name in FOREIGN_FOLDERS:
                continue
            seen.add(match.name)
            child = {k: v for k, v in entry.items() if k != "pattern"}
            child["name"] = match.name
            child["from_pattern"] = entry["pattern"]
            expanded.append(child)
    for entry in expanded:
        state = entry.get("listing", "on")
        if state not in LISTING_STATES:
            raise SystemExit(f"{entry['name']}: listing must be one of {', '.join(LISTING_STATES)}, got {state!r}")
    data["skills"] = expanded
    return data


def sync_claude_settings(data: dict, home: Path, dry_run: bool, skip: set[str] | None = None) -> list[str]:
    """Write the manifest's listing states and listing budget into ~/.claude/settings.json.

    Touches only `skillOverrides` keys that belong to manifest skills (sets the
    non-default ones, removes a stale key when a skill goes back to `on`) and the
    keys under the manifest's `claude_settings:` block. Everything else in the
    file is left as it is. Names in `skip` (dormant skills with no source on this
    machine) get no override, so a later install of the source starts clean.
    Returns one line per change; empty when nothing moved.
    """
    path = home / ".claude" / "settings.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    overrides = dict(settings.get("skillOverrides") or {})
    changes: list[str] = []
    for entry in data["skills"]:
        name, state = entry["name"], entry.get("listing", "on")
        if skip and name in skip:
            continue
        if state == "on":
            if name in overrides:
                overrides.pop(name)
                changes.append(f"skillOverrides[{name}]: removed (back to on)")
        elif overrides.get(name) != state:
            overrides[name] = state
            changes.append(f"skillOverrides[{name}]: {state}")
    for key, value in (data.get("claude_settings") or {}).items():
        if settings.get(key) != value:
            settings[key] = value
            changes.append(f"{key}: {value!r}")
    if overrides:
        settings["skillOverrides"] = dict(sorted(overrides.items()))
    else:
        settings.pop("skillOverrides", None)
    if changes and not dry_run:
        if path.exists():
            backup = path.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, backup)
            changes.append(f"backup written to {backup}")
        path.write_text(json.dumps(settings, indent=2) + "\n")
    return changes


def resolve(raw: str, home: Path) -> Path:
    if raw.startswith("~"):
        return home / raw[1:].lstrip("/")
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def source_dir(entry: dict, data: dict, home: Path) -> Path:
    try:
        root = data["sources"][entry["source"]]
    except KeyError:
        raise SystemExit(f"{entry['name']}: unknown source '{entry.get('source')}'")
    return resolve(root, home) / entry["name"]


def tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = file.relative_to(path)
        if rel.name in SKIP_IN_HASH or "__pycache__" in rel.parts or ".git" in rel.parts or rel.suffix == ".pyc":
            continue
        digest.update(str(rel).encode())
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def read_marker(dest: Path) -> dict | None:
    marker = dest / MARKER
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text())
    except json.JSONDecodeError:
        return {}


def held_by_dormant(name: str, marker: dict, data: dict) -> bool:
    """True when this managed copy belongs to a dormant pattern, so it is not a delete.

    A marker that does not say where it came from counts as held: keeping a copy is
    the mistake that can be undone.
    """
    return any(marker.get("source_kind") in (None, d["source"]) and fnmatch.fnmatchcase(name, d["pattern"])
               for d in data.get("dormant_patterns", []))


def write_marker(dest: Path, src: Path, entry: dict, src_hash: str) -> None:
    (dest / MARKER).write_text(json.dumps({
        "source": str(src),
        "source_kind": entry["source"],
        "hash": src_hash,
        "repo": entry.get("repo"),
        "ref": entry.get("ref"),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, indent=2) + "\n")


def backup_copy(dest: Path, backup_root: Path) -> Path:
    """Copy an installed folder aside before it is replaced or removed. Links are copied as links."""
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_root / f"{dest.name}-{stamp}"
    n = 1
    while target.exists():
        target = backup_root / f"{dest.name}-{stamp}-{n}"
        n += 1
    shutil.copytree(dest, target, symlinks=True)
    return target


def edited(dest: Path, marker: dict | None) -> bool:
    """True when the installed copy differs from what this script last wrote there."""
    return dest.is_dir() and not is_link(dest) and (marker is None or tree_hash(dest) != marker.get("hash"))


def staging_root(dest_root: Path) -> Path:
    return dest_root.parent / ".skills-staging"


def clear_stale_staging(dest_root: Path) -> None:
    """Leftovers from a run that died between its two renames. Younger ones may belong to a live run."""
    root = staging_root(dest_root)
    if not root.is_dir():
        return
    cutoff = time.time() - STAGING_MAX_AGE
    for child in root.iterdir():
        try:
            if child.lstat().st_mtime >= cutoff:
                continue
            if is_link(child):
                unlink_link(child)
            else:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def swap_in(src: Path, dest: Path, entry: dict, src_hash: str) -> None:
    """Build the copy in staging, then rename the old copy out and the new one in.

    A locked file makes a rename fail before anything is deleted; the old copy is put
    back if the second rename fails. The old copy is deleted only after the new one is
    in place, and a delete that a locked file stops is left for the next run.
    """
    root = staging_root(dest.parent)
    root.mkdir(parents=True, exist_ok=True)
    token = f"{dest.name}.{os.getpid()}.{time.time_ns()}"
    new, old = root / f"{token}.new", root / f"{token}.old"
    try:
        shutil.copytree(src, new, ignore=IGNORE)
        # The marker records what was copied, so a source that changes mid-copy reads as a
        # source change next run, never as a hand edit of the installed copy.
        write_marker(new, src, entry, tree_hash(new))
    except OSError:
        shutil.rmtree(new, ignore_errors=True)
        raise
    moved = False
    try:
        if is_link(dest):
            unlink_link(dest)  # skills are copied, never linked
        elif dest.exists():
            _os_replace(dest, old)
            moved = True
        _os_replace(new, dest)
    except OSError:
        if moved and not dest.exists():
            _os_replace(old, dest)
        shutil.rmtree(new, ignore_errors=True)
        raise
    if moved:
        shutil.rmtree(old, ignore_errors=True)


def install_one(entry: dict, data: dict, home: Path, dry_run: bool, force: bool, skip_missing: bool = False,
                backup_root: Path | None = None) -> str:
    name = entry["name"]
    src = source_dir(entry, data, home)
    dest = resolve(data["destination"], home) / name
    if not (src / "SKILL.md").exists():
        if absent_ok(entry, skip_missing):
            return f"· {name}: dormant (no source at {src})"
        return f"✗ {name}: source has no SKILL.md at {src}"
    marker = read_marker(dest)
    src_hash = tree_hash(src)
    if marker is None and dest.is_dir() and not is_link(dest) and tree_hash(dest) == src_hash:
        if dry_run:
            return f"→ {name}: would adopt {dest}, same content as {src}"
        write_marker(dest, src, entry, src_hash)
        return f"✓ {name}: adopted, same content as {src}, no file changed"
    if (dest.exists() or is_link(dest)) and marker is None and not force:
        kind = "is a link" if is_link(dest) else "exists"
        return f"✗ {name}: {dest} {kind} and was not installed by this script (use --force)"
    if marker and not is_link(dest) and marker.get("hash") == src_hash and tree_hash(dest) == src_hash:
        return f"= {name}: up to date"
    hand_edited = edited(dest, marker)
    if dry_run:
        extra = ", saving the edited copy first" if hand_edited else ""
        return f"→ {name}: would copy {src} -> {dest}{extra}"
    saved = None
    if hand_edited:
        try:
            saved = backup_copy(dest, backup_root or ROOT / "local" / "skill-edits")
        except OSError as error:
            return f"✗ {name}: could not save the edited copy ({error}); left it in place"
    try:
        swap_in(src, dest, entry, src_hash)
    except OSError as error:
        return f"✗ {name}: could not replace {dest} ({error}); the installed copy is unchanged"
    if saved:
        notice("edit-backup", name, f"the installed copy of {name} had edits; saved them to {saved} and restored the source version",
               backup=str(saved))
        return f"✓ {name}: installed from {src} (edited copy saved to {saved})"
    return f"✓ {name}: installed from {src}"


def unmanaged_folders(data: dict, home: Path) -> list[str]:
    """Skill folders in the destination that no manifest entry and no marker explains, minus foreign ones."""
    dest_root = resolve(data["destination"], home)
    if not dest_root.is_dir():
        return []
    listed = {entry["name"] for entry in data["skills"]}
    found = []
    for child in sorted(dest_root.iterdir()):
        if child.name in FOREIGN_FOLDERS or child.name.startswith(".") or child.name in listed:
            continue
        if child.is_dir() and read_marker(child) is None and (child / "SKILL.md").exists():
            found.append(child.name)
    return found


def check(data: dict, home: Path, skip_missing: bool = False) -> int:
    dest_root = resolve(data["destination"], home)
    drift = 0
    listed = set()
    if is_linked_worktree(ROOT):
        print(f"· note: checking this worktree's sources ({ROOT}); run from the main checkout for the installed truth")
    for dormant in data.get("dormant_patterns", []):
        print(f"· pattern {dormant['pattern']}: dormant ({dormant['why']})")
    for entry in data["skills"]:
        name = entry["name"]
        listed.add(name)
        src = source_dir(entry, data, home)
        dest = dest_root / name
        if not (src / "SKILL.md").exists():
            if absent_ok(entry, skip_missing):
                print(f"· {name}: dormant (no source at {src})")
                continue
            print(f"✗ {name}: source missing {src}")
            drift += 1
            continue
        declared = frontmatter_name(src)
        if declared != name:
            print(f"✗ {name}: folder name differs from its frontmatter name {declared!r} "
                  f"(Claude reads the folder name, Amp the frontmatter)")
            drift += 1
        if not dest.exists():
            print(f"✗ {name}: not installed")
            drift += 1
            continue
        marker = read_marker(dest)
        if marker is None:
            print(f"? {name}: present but unmanaged (no {MARKER})")
            drift += 1
            continue
        src_hash, dest_hash = tree_hash(src), tree_hash(dest)
        if src_hash != marker.get("hash"):
            print(f"↑ {name}: source changed since install (the next sync copies it)")
            drift += 1
        elif dest_hash != marker.get("hash"):
            print(f"✎ {name}: copy edited by hand (edit the source; the next sync saves this copy and restores the source)")
            drift += 1
        else:
            print(f"= {name}: in sync")
    if dest_root.exists():
        for child in sorted(dest_root.iterdir()):
            marker = read_marker(child) if child.is_dir() and child.name not in listed else None
            if marker is None:
                continue
            if held_by_dormant(child.name, marker, data):
                print(f"· {child.name}: kept, its source is dormant")
                continue
            print(f"− {child.name}: managed copy not in manifest (run --prune)")
            drift += 1
    for name in unmanaged_folders(data, home):
        print(f"? {name}: unmanaged folder in {dest_root} (loads in both agents; move it to .agents/skills or local/skills)")
    return 1 if drift else 0


def prune(data: dict, home: Path, dry_run: bool, backup_root: Path | None = None,
          allow_mass: bool = False, source_kinds: set[str] | None = None) -> int:
    """Remove managed copies the manifest no longer lists. Returns the failure count.

    Only folders with this script's marker are candidates; a dormant source holds its
    copies; an edited copy is saved first; a link is unlinked, never walked; more than
    PRUNE_CAP removals at once is refused, because that looks like a moved repo, not a cleanup.
    """
    dest_root = resolve(data["destination"], home)
    listed = {entry["name"] for entry in data["skills"]}
    if not dest_root.exists():
        return 0
    victims = []
    for child in sorted(dest_root.iterdir()):
        if child.name in FOREIGN_FOLDERS:
            continue
        marker = read_marker(child) if child.is_dir() and child.name not in listed else None
        if marker is None:
            continue
        if source_kinds and marker.get("source_kind") not in source_kinds:
            continue
        if held_by_dormant(child.name, marker, data):
            print(f"· {child.name}: kept, its source is dormant")
            continue
        victims.append((child, marker))
    if len(victims) > PRUNE_CAP and not allow_mass:
        names = ", ".join(child.name for child, _ in victims)
        print(f"✗ prune refused: {len(victims)} copies would go at once (cap {PRUNE_CAP}): {names}. "
              f"Check the sources, then rerun with --allow-mass-prune.")
        notice("prune-refused", "*", f"prune refused: {len(victims)} skill copies would go at once ({names})")
        return 1
    failures = 0
    for child, marker in victims:
        if dry_run:
            print(f"→ {child.name}: would remove {child}")
            continue
        try:
            if is_link(child):
                unlink_link(child)
            else:
                saved = backup_copy(child, backup_root or ROOT / "local" / "skill-edits") if edited(child, marker) else None
                shutil.rmtree(child)
                if saved:
                    notice("edit-backup", child.name,
                           f"{child.name} left the manifest; its installed copy had edits, saved to {saved}", backup=str(saved))
            print(f"− {child.name}: removed")
        except OSError as error:
            failures += 1
            print(f"✗ {child.name}: could not remove ({error})")
    return failures


def guard_destination(data: dict, home: Path, kinds: set[str] | None = None) -> str | None:
    """A reason to refuse when the destination and a source root this run reads overlap, else None.

    Codex mode installs only pm-os skills into ~/.agents/skills, which is also the `agents`
    root; with kinds limited to pm-os that root is not read, so it is not an overlap.
    """
    dest = Path(os.path.realpath(resolve(data["destination"], home)))
    for kind, raw in (data.get("sources") or {}).items():
        if kinds and kind not in kinds:
            continue
        root = Path(os.path.realpath(resolve(raw, home)))
        if dest == root or root in dest.parents or dest in root.parents:
            return f"destination {dest} overlaps the {kind} source root {root}; refusing so no sync can delete a source"
    return None


def apply_edits(entry: dict, target: Path) -> list[str]:
    """Apply the manifest's `drop` and `edits` to a fresh vendored copy.

    drop:  list of paths (relative to the skill dir) to delete.
    edits: list of {files: glob, find: str, replace: str}; `find` must match in at
           least one file, so a stale edit fails loudly when upstream changes.
    Returns one human-readable line per applied edit for SOURCE.md.
    """
    applied: list[str] = []
    for rel in entry.get("drop", []) or []:
        victim = target / rel
        if victim.is_dir():
            shutil.rmtree(victim)
        elif victim.exists():
            victim.unlink()
        else:
            raise SystemExit(f"{entry['name']}: drop path {rel} does not exist upstream")
        applied.append(f"dropped `{rel}`")
    for edit in entry.get("edits", []) or []:
        files = sorted(p for p in target.glob(edit.get("files", "SKILL.md")) if p.is_file())
        hits = 0
        for file in files:
            text = file.read_text()
            if edit["find"] in text:
                hits += text.count(edit["find"])
                file.write_text(text.replace(edit["find"], edit["replace"]))
        if not hits:
            raise SystemExit(f"{entry['name']}: edit `{edit['find']}` matched nothing in {edit.get('files', 'SKILL.md')}")
        applied.append(f"replaced `{edit['find']}` with `{edit['replace']}` ({hits} place(s) in {edit.get('files', 'SKILL.md')})")
    return applied


def vendor(entry: dict, data: dict, home: Path, dry_run: bool) -> str:
    name = entry["name"]
    if entry.get("source") != "vendor":
        return f"✗ {name}: not a vendor entry"
    for key in ("repo", "ref", "path"):
        if not entry.get(key):
            return f"✗ {name}: vendor entry needs '{key}'"
    target = source_dir(entry, data, home)
    if dry_run:
        return f"→ {name}: would fetch {entry['repo']}@{entry['ref']}:{entry['path']} -> {target}"
    with tempfile.TemporaryDirectory(prefix="pm-os-vendor-") as tmp:
        clone = Path(tmp) / "repo"
        subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
                        entry["repo"], str(clone)], check=True)
        subprocess.run(["git", "-C", str(clone), "checkout", "--quiet", entry["ref"]], check=True)
        head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        if not head.startswith(entry["ref"]):
            return f"✗ {name}: ref {entry['ref']} resolved to {head}; pin a commit sha"
        src = clone / entry["path"]
        if not (src / "SKILL.md").exists():
            return f"✗ {name}: no SKILL.md at {entry['path']} in {entry['repo']}@{head[:12]}"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target, ignore=IGNORE)
        applied = apply_edits(entry, target)
        license_text = ""
        for candidate in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
            if (clone / candidate).exists():
                shutil.copy2(clone / candidate, target / "LICENSE.upstream")
                license_text = f"License file copied to `LICENSE.upstream` ({entry.get('license', 'see file')})."
                break
        (target / "SOURCE.md").write_text(
            f"# Source\n\n"
            f"- Repository: {entry['repo']}\n"
            f"- Commit: `{head}`\n"
            f"- Path: `{entry['path']}`\n"
            f"- Fetched: {time.strftime('%Y-%m-%d')}\n"
            f"- Safety review: {entry.get('reviewed', 'not recorded')}\n"
            f"- {license_text or 'License: ' + str(entry.get('license', 'not recorded'))}\n"
            + ("".join(f"- Local edit: {line}\n" for line in applied) if applied else "")
            + "\n"
            f"Refresh with `python3 system/scripts/install-claude-skills.py vendor {name}` after "
            f"updating `ref` in `system/config/claude-skills-manifest.yaml`.\n"
        )
    return f"✓ {name}: vendored {entry['repo']}@{head[:12]} -> {target}"


def settings_fingerprint(data: dict, skip: set[str]) -> str:
    """What the settings sync would write: listing states and claude_settings, nothing else."""
    wanted = sorted((e["name"], e.get("listing", "on")) for e in data["skills"] if e["name"] not in skip)
    blob = json.dumps({"listing": wanted, "claude_settings": data.get("claude_settings") or {}}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def main(argv: list[str]) -> int:
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Task Scheduler gives a cp1252 pipe
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--home", type=Path, default=Path.home(), help="alternate home for tests")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--allow-mass-prune", action="store_true",
                        help=f"let --prune remove more than {PRUNE_CAP} copies in one run")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="replace an unmanaged directory at the destination (saved first)")
    parser.add_argument("--only", action="append", default=[], metavar="NAME", help="limit to these skills")
    parser.add_argument("--skip-missing", action="store_true",
                        help="treat skills and patterns with no source on this machine as dormant, not failures")
    parser.add_argument("--destination", help="install somewhere other than the manifest destination (Codex's ~/.agents/skills)")
    parser.add_argument("--source-kind", action="append", default=[], metavar="KIND",
                        help="limit install and prune to entries from these sources (pm-os, local, vendor, agents)")
    parser.add_argument("--backup-dir", type=Path, default=None,
                        help="where edited copies are saved before replacement (default local/skill-edits)")
    parser.add_argument("--notices-json", type=Path, default=None, help="write events worth a CoS line here as JSON")
    parser.add_argument("--settings-state", type=Path, default=None,
                        help="skip the settings.json sync while the wanted listing matches the hash stored here")
    parser.add_argument("--no-settings", action="store_true", help="leave ~/.claude/settings.json alone")
    parser.add_argument("--allow-worktree", action="store_true",
                        help="with --only: install those skills from a task worktree (testing one skill)")
    sub = parser.add_subparsers(dest="command")
    vend = sub.add_parser("vendor", help="refresh pinned third-party copies")
    vend.add_argument("names", nargs="*")
    vend.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    home = args.home
    data = load_manifest(args.manifest, home, skip_missing=args.skip_missing)
    if args.destination:
        data["destination"] = args.destination
    entries = data["skills"]
    if args.only:
        entries = [e for e in entries if e["name"] in args.only]
    kinds = set(args.source_kind)
    if kinds:
        entries = [e for e in entries if e.get("source") in kinds]

    if args.command == "vendor":
        wanted = [e for e in entries if e.get("source") == "vendor"]
        if not args.all:
            wanted = [e for e in wanted if e["name"] in args.names]
            missing = set(args.names) - {e["name"] for e in wanted}
            if missing:
                print(f"✗ not vendor entries in manifest: {', '.join(sorted(missing))}")
                return 1
        failures = 0
        for entry in wanted:
            line = vendor(entry, data, home, args.dry_run)
            print(line)
            failures += line.startswith("✗")
        return 1 if failures else 0

    if args.check:
        return check(data, home, args.skip_missing)

    real_dest = Path(os.path.realpath(resolve(data["destination"], home)))
    real_roots = [Path(os.path.realpath(Path.home() / sub)) for sub in (".claude", ".agents")]
    real_home = (os.path.realpath(home) == os.path.realpath(Path.home())
                 or any(real_dest == r or r in real_dest.parents for r in real_roots))
    if not args.dry_run and real_home and is_linked_worktree(ROOT) and not (args.allow_worktree and args.only):
        print(f"✗ refusing to install from a task worktree ({ROOT}): its skills are this branch's, and installing them "
              f"would replace main's and could prune newer ones. Run `python3 system/scripts/sync-skills.py`, which always "
              f"uses the main checkout, or pass --only NAME --allow-worktree to test one skill.")
        return 2
    overlap = guard_destination(data, home, kinds or None)
    if overlap:
        print(f"✗ {overlap}")
        return 2

    failures = 0
    dormant: set[str] = set()
    dest_root = resolve(data["destination"], home)
    dest_root.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        clear_stale_staging(dest_root)
    for dormant_pattern in data.get("dormant_patterns", []):
        print(f"· pattern {dormant_pattern['pattern']}: dormant ({dormant_pattern['why']})")
    for entry in entries:
        line = install_one(entry, data, home, args.dry_run, args.force, args.skip_missing, backup_root=args.backup_dir)
        print(line)
        failures += line.startswith("✗")
        if line.startswith("✗"):
            notice("install-failed", entry["name"], line[2:])
        if line.startswith("·"):
            dormant.add(entry["name"])
    if args.prune and not args.only:
        failures += prune(data, home, args.dry_run, backup_root=args.backup_dir, allow_mass=args.allow_mass_prune,
                          source_kinds=kinds or None)
    default_dest = os.path.realpath(resolve("~/.claude/skills", home)) == os.path.realpath(dest_root)
    if not args.dry_run and default_dest:
        for name in unmanaged_folders(data, home):
            notice("unmanaged", name, f"{name} is an unmanaged skill folder in {dest_root}; it loads in both agents",
                   path=str(dest_root / name))
    if not args.only and not kinds and not args.no_settings and default_dest:
        fingerprint = settings_fingerprint(data, dormant)
        stored = ""
        if args.settings_state:
            with contextlib.suppress(OSError):
                stored = args.settings_state.read_text(encoding="utf-8").strip()
        if stored == fingerprint:
            print("· settings: listing unchanged since the last sync; settings.json left alone")
        else:
            changes = sync_claude_settings(data, home, args.dry_run, skip=dormant)
            prefix = "→ would set " if args.dry_run else "⚙ settings "
            shown = changes if len(changes) <= 12 else changes[:6] + [f"... {len(changes) - 12} more skillOverrides"] + changes[-6:]
            for line in shown:
                print(prefix + line)
            if args.settings_state and not args.dry_run:
                args.settings_state.parent.mkdir(parents=True, exist_ok=True)
                args.settings_state.write_text(fingerprint + "\n", encoding="utf-8")
    if args.notices_json:
        args.notices_json.parent.mkdir(parents=True, exist_ok=True)
        args.notices_json.write_text(json.dumps(NOTICES, indent=2) + "\n", encoding="utf-8")
    summary = f"{len(entries)} manifest skill(s) processed, {failures} failure(s)"
    if dormant:
        summary += f", {len(dormant)} dormant"
    print(summary + ".")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
