#!/usr/bin/env python3
"""Install one organisation profile into this PM OS checkout: `company` or `personal`.

Core PM OS is generic. Everything that only works inside your company (its own
skills, MCP servers, jobs and rules) lives in
profiles/company/. A personal machine gets none of it; a company machine gets all of it.

  apply-profile.py                  detect and apply (company when an organization's `pmos` CLI is
                                    on PATH, else personal)
  apply-profile.py --profile company  force a profile
  apply-profile.py --check          print the active and detected profile, change nothing
  apply-profile.py --dry-run        show what would change

What applying a profile does, all idempotent:
  1. copies profiles/<p>/overlay/** to the same paths in the checkout
     (and removes files a previously applied profile copied that this one lacks)
  2. fills the marked profile sections in AGENTS.md and adapters/SYSTEM.md from
     profiles/<p>/agents-section.md and system-section.md (empty for personal)
  3. writes PM_OS_PROFILE and the profile's env (profile.json "env") to ~/.pm-os/env
  4. records the applied files in .pm-os-kit/profile.json

Publishing the kit from a company machine: the profile section and overlay files are
machine-specific. Publish from core (`apply-profile.py --profile personal` first, or
have the publisher drop profiled files listed in .pm-os-kit/profile.json and empty the
marked sections). profiles/ itself always ships. See docs/profiles.md.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "profiles"
RECORD = ROOT / ".pm-os-kit" / "profile.json"
BACKUP = ROOT / ".pm-os-kit" / "profile-backup"
SECTION_FILES = {"AGENTS.md": "agents-section.md", "adapters/SYSTEM.md": "system-section.md"}
START = "<!-- pm-os:profile-section:start (filled by system/scripts/apply-profile.py; edit profiles/<name>/ instead) -->"
END = "<!-- pm-os:profile-section:end -->"
# Every folder under profiles/ with a profile.json (the personal and solo profiles ship;
# an organization adds its own).
KNOWN = tuple(sorted(p.name for p in PROFILES.iterdir() if (p / "profile.json").is_file())) if PROFILES.is_dir() else ("personal",)


def state_home() -> Path:
    env = os.environ.get("PM_OS_STATE_HOME")
    return Path(env) if env else Path.home() / ".pm-os"


def detect() -> str:
    """company when a native connector CLI is on PATH (system/connectors/company_cli.py), else personal."""
    helper = ROOT / "system" / "connectors" / "company_cli.py"
    try:
        spec = importlib.util.spec_from_file_location("pm_os_company_cli", helper)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return "company" if mod.has_company_cli() else "personal"
    except Exception:
        return "personal"


def recorded() -> dict:
    try:
        return json.loads(RECORD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def overlay_files(name: str) -> list[str]:
    base = PROFILES / name / "overlay"
    if not base.is_dir():
        return []
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def fill_section(text: str, body: str) -> str:
    """Replace what sits between the markers; add the markers at the end when missing."""
    block = f"{START}\n{body.strip()}\n{END}" if body.strip() else f"{START}\n{END}"
    if START in text and END in text:
        head, rest = text.split(START, 1)
        _, tail = rest.split(END, 1)
        return head + block + tail
    return text.rstrip("\n") + "\n\n" + block + "\n"


def write_env(values: dict[str, str]) -> Path:
    path = state_home() / "env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keep = [ln for ln in lines if ln.split("=", 1)[0].strip() not in values]
    path.parent.mkdir(parents=True, exist_ok=True)
    # An empty value removes the key (a previous profile's setting this one lacks).
    added = [f"{k}={v}" for k, v in values.items() if v]
    path.write_text("\n".join(keep + added) + "\n", encoding="utf-8", newline="\n")
    return path


def profile_meta(name: str) -> dict:
    try:
        return json.loads((PROFILES / name / "profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply(name: str, dry_run: bool = False) -> dict:
    if name not in KNOWN:
        raise SystemExit(f"unknown profile {name!r}; choose one of {', '.join(KNOWN)}")
    previous = recorded()
    wanted = overlay_files(name)
    stale = [rel for rel in previous.get("files", []) if rel not in wanted]
    changes: dict[str, list[str]] = {"copied": [], "removed": [], "sections": []}
    # A profile file may replace a core file of the same path (a company-flavoured SKILL.md,
    # for example). The core copy is kept in BACKUP and put back when the profile changes.
    owned = set(previous.get("files", []))
    for rel in stale:
        target, saved = ROOT / rel, BACKUP / rel
        if saved.exists():
            changes["removed"].append(rel)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(saved, target)
                saved.unlink()
        elif target.exists():
            changes["removed"].append(rel)
            if not dry_run:
                target.unlink()
    for rel in wanted:
        src, target = PROFILES / name / "overlay" / rel, ROOT / rel
        # Back up a core file before the profile owns its path, even when the bytes
        # match: removing the profile later must put the core file back, not delete it.
        if not dry_run and target.exists() and rel not in owned and not (BACKUP / rel).exists():
            (BACKUP / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, BACKUP / rel)
        if target.exists() and target.read_bytes() == src.read_bytes():
            continue
        changes["copied"].append(rel)
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
    for rel, section in SECTION_FILES.items():
        target = ROOT / rel
        if not target.exists():
            continue
        body_file = PROFILES / name / section
        body = body_file.read_text(encoding="utf-8") if body_file.exists() else ""
        text = target.read_text(encoding="utf-8")
        new = fill_section(text, body)
        if new != text:
            changes["sections"].append(rel)
            if not dry_run:
                target.write_text(new, encoding="utf-8", newline="\n")
    env = {"PM_OS_PROFILE": name, **(profile_meta(name).get("env") or {})}
    # Clear env a previous profile set and this one does not.
    for key in previous.get("env", {}):
        env.setdefault(key, "")
    if not dry_run:
        write_env(env)
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        RECORD.write_text(json.dumps({"profile": name, "files": wanted,
                                      "env": {k: v for k, v in env.items() if v}}, indent=2) + "\n", encoding="utf-8",
                          newline="\n")
    return {"profile": name, "dry_run": dry_run, **{k: v for k, v in changes.items()}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=("auto", *KNOWN), default=os.environ.get("PM_OS_PROFILE_OVERRIDE") or "auto")
    ap.add_argument("--check", action="store_true", help="print active and detected profile; change nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    detected = detect()
    if args.check:
        print(json.dumps({"active": recorded().get("profile") or "none", "detected": detected}))
        return 0
    name = detected if args.profile == "auto" else args.profile
    result = apply(name, dry_run=args.dry_run)
    print(json.dumps({**result, "detected": detected, "copied": len(result["copied"]),
                      "removed": len(result["removed"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
