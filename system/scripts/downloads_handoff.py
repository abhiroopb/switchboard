#!/usr/bin/env python3
"""Hand a repo note to the owner through the Downloads folder, and keep it safe.

The Downloads folder is ~/Downloads, or ~/Dropbox/Downloads when that is where a
machine keeps it, or SWITCHBOARD_DOWNLOADS_DIR. A synced Downloads folder is shared with
every other machine, so a file that lives only there can vanish when another device
moves or deletes it, with no Recycle Bin entry here. So a hand-off file has one canonical copy
in the repo, tracked in git, and Downloads gets a copy of it.

  put <repo path> [--name NAME] [--replace]   copy the repo file into Downloads
  check [--restore] [--json]                  compare every recorded hand-off
  retire NAME [--superseded-by NAME]          stop tracking a replaced hand-off

`put` refuses a path outside the repo and refuses to overwrite a Downloads file
it did not write (or that changed since it wrote it) unless --replace is given.
`check` reports ok, missing, changed (the Downloads copy differs from the last
put), stale (the repo copy moved on since the last put), source-changed (the
Downloads copy is gone and no repo copy matches the last put), or
source-missing. --restore copies the repo file back only for missing entries,
and only the exact bytes of the last put. `retire` drops one record, so a hand-off
the owner had replaced is never
restored; it leaves the Downloads file where it is. Nothing here deletes or moves a file in Downloads (AGENTS.md: never
auto-sweep Downloads).

Records: <main checkout>/.state/downloads-handoffs.json. Each row keeps the
absolute source path, so a check from main still finds a worktree copy that
has not landed yet.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repo_paths  # noqa: E402

RECORDS = Path(".state") / "downloads-handoffs.json"


def repo_root(cwd: Path | None = None) -> Path:
    probe = subprocess.run(["git", "-C", str(cwd or Path.cwd()), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if probe.returncode or not probe.stdout.strip():
        raise SystemExit("downloads_handoff: run inside a switchboard checkout")
    return Path(probe.stdout.strip()).resolve()


def downloads_dir() -> Path:
    override = os.environ.get("SWITCHBOARD_DOWNLOADS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    home = Path.home()
    for cand in (home / "Downloads", home / "Dropbox" / "Downloads"):
        if cand.is_dir():
            return cand
    raise SystemExit("downloads_handoff: no Downloads folder (~/Downloads or ~/Dropbox/Downloads)")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def records_path(top: Path) -> Path:
    """Shared state lives on the main checkout, never in a worktree copy."""
    return repo_paths.main_checkout(top) / RECORDS


def load(top: Path) -> dict:
    try:
        data = json.loads(records_path(top).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(top: Path, data: dict) -> None:
    path = records_path(top)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".downloads-handoffs.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def candidates(row: dict, top: Path) -> list[Path]:
    rel = row.get("repo_path", "")
    main = repo_paths.main_checkout(top)
    raw = [Path(row["source_abs"])] if row.get("source_abs") else []
    raw += [top / rel, main / rel]
    return [c for c in dict.fromkeys(raw) if c.is_file()]


def canonical(row: dict, top: Path) -> Path | None:
    """Main's copy once it exists, else the copy the put came from."""
    main_copy = repo_paths.main_checkout(top) / row.get("repo_path", "")
    if main_copy.is_file():
        return main_copy
    found = candidates(row, top)
    return found[0] if found else None


def matching_source(row: dict, top: Path) -> Path | None:
    """The repo copy whose bytes match the last put. Never a different version:
    restoring other text could lose the original copy."""
    for cand in candidates(row, top):
        if sha(cand) == row.get("sha256"):
            return cand
    return None


def safe_name(name: str) -> str:
    if not name or name in (".", "..") or Path(name).name != name or "/" in name or "\\" in name:
        raise SystemExit(f"downloads_handoff: --name must be a plain file name, got {name!r}")
    return name


def put(src_arg: str, name: str | None, replace: bool, top: Path) -> dict:
    src = Path(src_arg)
    src = (src if src.is_absolute() else Path.cwd() / src).resolve()
    try:
        rel = src.relative_to(top).as_posix()
    except ValueError:
        raise SystemExit(f"downloads_handoff: {src} is outside the repo; keep the canonical copy in the repo")
    if not src.is_file():
        raise SystemExit(f"downloads_handoff: {rel} does not exist")
    folder = downloads_dir()
    dest = folder / safe_name(name or src.name)
    if dest.resolve().parent != folder.resolve():
        raise SystemExit(f"downloads_handoff: {dest} is not directly inside {folder}")
    records = load(top)
    prior = records.get(dest.name)
    if dest.exists() and not replace:
        ours = prior and prior.get("repo_path") == rel and sha(dest) == prior.get("sha256")
        if not ours and sha(dest) != sha(src):
            raise SystemExit(f"downloads_handoff: {dest} exists and was not written by this tool "
                             "(or changed since); pass --replace to overwrite it")
    shutil.copyfile(src, dest)
    row = {"repo_path": rel, "source_abs": str(src), "downloads_path": str(dest), "sha256": sha(dest),
           "put_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    records[dest.name] = row
    save(top, records)
    return row


def retire(name: str, superseded_by: str | None, top: Path) -> dict:
    records = load(top)
    key = safe_name(name)
    if key not in records:
        raise SystemExit(f"downloads_handoff: no hand-off named {key} on record")
    if superseded_by and safe_name(superseded_by) not in records:
        raise SystemExit(f"downloads_handoff: put {superseded_by} before retiring what it replaces")
    row = records.pop(key)
    save(top, records)
    return {"retired": key, "superseded_by": superseded_by or "", "downloads_path": row.get("downloads_path", "")}


BAD = ("missing", "source-missing", "source-changed", "stale")


def check(restore: bool, top: Path) -> list[dict]:
    out = []
    records = load(top)
    changed_records = False
    for key, row in sorted(records.items()):
        dest = Path(row.get("downloads_path") or downloads_dir() / key)
        item = {"name": key, "repo_path": row.get("repo_path"), "downloads_path": str(dest)}
        if not candidates(row, top):
            item["status"] = "source-missing"
        elif not dest.exists():
            src = matching_source(row, top)
            if src is None:
                item["status"] = "source-changed"  # the repo moved on; rerun put, never restore other text
            elif restore:
                shutil.copyfile(src, dest)
                row["restored_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                changed_records = True
                item["status"] = "restored"
            else:
                item["status"] = "missing"
        elif sha(dest) != row.get("sha256"):
            item["status"] = "changed"
        else:
            canon = canonical(row, top)
            item["status"] = "ok" if canon and sha(canon) == row.get("sha256") else "stale"
        out.append(item)
    if changed_records:
        save(top, records)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("put")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--replace", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("--restore", action="store_true")
    c.add_argument("--json", action="store_true")
    r = sub.add_parser("retire")
    r.add_argument("name")
    r.add_argument("--superseded-by")
    args = ap.parse_args(argv)
    top = repo_root()
    if args.cmd == "put":
        row = put(args.path, args.name, args.replace, top)
        print(json.dumps(row))
        return 0
    if args.cmd == "retire":
        print(json.dumps(retire(args.name, args.superseded_by, top)))
        return 0
    rows = check(args.restore, top)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['status']:15} {r['name']}  <- {r['repo_path']}")
    return 1 if any(r["status"] in BAD for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
