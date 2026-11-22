#!/usr/bin/env python3
"""Keep the installed skills in step with their sources, with no one running an installer.

Skills are copies, kept current automatically. Sources are `.agents/skills` (shared, tracked), `local/skills` (private,
gitignored), `system/vendor/skills` (pinned third-party) and `~/.agents/skills` (the pmos
library, plus `todo`). `~/.claude/skills` is output only; Claude and Amp both read it.

Runs:
- on every command-center tick (command-center-loop.sh),
- right after a branch lands on main (dispatch.py finish, after the landing lock frees),
- by hand: `python3 system/scripts/sync-skills.py` from any checkout.

One run, always against the main checkout, never a worktree's copy:
  0. moves to the trash skill folders that a deleted skill left behind with no SKILL.md and only
     gitignored cache files (__pycache__ and the like), untouched for an hour, in the shared sources and
     ~/.claude/skills (prune_skill_husks);
  1. applies the tracked overrides (system/skill-overrides) to ~/.agents/skills/todo,
     installing the whole override when no skill catalog is present and todo is missing;
  2. runs main's install-claude-skills.py --prune: changed skills are copied, an edited
     installed copy is saved to local/skill-edits first, and more than 5 deletes at once
     is refused;
  3. where `codex` is on PATH, refreshes the PM OS copies in ~/.agents/skills, which Codex
     reads; elsewhere it moves old unmarked repo copies there to the Recycle Bin (or the
     macOS Trash), because Amp reads ~/.agents/skills first and a stale copy masks the
     current one. `todo` always stays there;
  4. with --notify, sends CoS one deduped line for an edited copy it saved, an unmanaged
     folder in ~/.claude/skills, a refused prune, or three failed runs in a row.

Skips, exit 0: another sync running, a landing or rebase in flight, PM_OS_TEST_SANDBOX=1,
SKILL_SYNC_DISABLED=1. State: system/state/skill-sync.json. Log: the caller's.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import flock_compat as fcntl  # type: ignore

FAILURE_ALERT_AFTER = 3
KEEP_IN_AGENTS = {"todo"}
TRASH_CAP = 20
STANDING_KINDS = {"unmanaged", "prune-refused"}


def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main_checkout() -> Path:
    """The main checkout, from the cwd's repo when there is one, else from this file."""
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        probe = subprocess.run(["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
        common = Path(probe.stdout.strip()) if probe.returncode == 0 and probe.stdout.strip() else None
        if common and common.name == ".git" and (common.parent / "system/scripts/install-claude-skills.py").exists():
            return common.parent
    return Path(__file__).resolve().parents[2]


@contextlib.contextmanager
def try_lock(path: Path):
    """Yield True when the lock is ours, False when another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def rebase_in_flight(main: Path) -> bool:
    git_dir = main / ".git"
    bases = [git_dir, *sorted(p for p in (git_dir / "worktrees").glob("*") if p.is_dir())]
    return any((base / name).exists() for base in bases for name in ("rebase-merge", "rebase-apply"))


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _os_replace(tmp, path)


def run(cmd: list[str], timeout: int = 300, env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")
    except OSError as error:
        return subprocess.CompletedProcess(cmd, 127, "", str(error))


def company_cli(main: Path) -> bool:
    helper = main / "system/connectors/company_cli.py"
    if helper.exists():
        return run([sys.executable, str(helper), "--quiet"], timeout=30).returncode == 0
    return shutil.which("pmos") is not None


def apply_overrides(main: Path, home: Path) -> tuple[bool, str]:
    """Step 4 of the design: the tracked todo override goes on before any copy."""
    todo = home / ".agents" / "skills" / "todo"
    override = main / "system" / "skill-overrides" / "todo"
    if not todo.is_dir() and override.is_dir() and not company_cli(main):
        # No company catalog: the tracked override is a complete skill (install-amp-skills.sh does the same).
        shutil.copytree(override, todo, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return True, "todo installed whole from the tracked override (no skill catalog)"
    script = main / "system" / "scripts" / "sync-skill-overrides.sh"
    if not script.exists():
        return True, "no override script"
    result = run(["bash", str(script)], timeout=120, env={"HOME": str(home)} if home != Path.home() else None)
    lines = (result.stdout + result.stderr).strip().splitlines()
    # An override whose skill the skill catalog does not carry on this machine is skipped. That is no sync
    # failure: nothing is stale, there is nothing to patch. The script exits 3 when skips are its only
    # problem; any other nonzero exit is a failure, even with skip lines beside it, because a command that
    # fails under `set -e` (a locked file on Windows) prints no line of its own.
    skipped = [l for l in lines if "skill not installed" in l]
    if result.returncode == 3:
        return True, f"{len(skipped)} override file(s) skipped: skill not installed here"
    real = [l for l in lines if "skill not installed" not in l]
    return result.returncode == 0, " | ".join((real or lines)[-3:])


def install(main: Path, home: Path, extra: list[str], notices: Path) -> tuple[int, str]:
    cmd = [sys.executable, str(main / "system/scripts/install-claude-skills.py"), "--home", str(home),
           "--notices-json", str(notices), *extra]
    if os.environ.get("PM_OS_SKILL_EDITS_DIR"):  # tests; the default is main's local/skill-edits
        cmd += ["--backup-dir", os.environ["PM_OS_SKILL_EDITS_DIR"]]
    result = run(cmd, timeout=600)
    return result.returncode, (result.stdout + result.stderr).strip()


def move_to_trash(path: Path, main: Path) -> tuple[bool, str]:
    """Recycle Bin on Windows, the Trash on macOS, through pm-os-trash.py, which refuses
    rather than hard-deletes when the bin cannot take it and logs every move."""
    helper = main / "system" / "scripts" / "pm-os-trash.py"
    result = run([sys.executable, str(helper), "-rf", str(path)], timeout=180)
    said = (result.stderr + result.stdout).strip()
    if result.returncode == 0 and "moved" in said and not path.exists():
        return True, said.split("(", 1)[-1].split(")", 1)[0] if "(" in said else "the Trash"
    return False, said[:200] or f"pm-os-trash.py exited {result.returncode}"


def tidy_agents_copies(main: Path, home: Path, dry_run: bool) -> list[str]:
    """Old copies of repo skills that install-amp-skills.sh put in ~/.agents/skills.

    Only a folder that (1) has a repo source of the same name, (2) has no installer marker
    (so it was not made for Codex), (3) is not todo, and (4) has a managed copy in
    ~/.claude/skills that Amp will read instead. Moved to the trash, never deleted.
    """
    agents = home / ".agents" / "skills"
    repo = main / ".agents" / "skills"
    claude = home / ".claude" / "skills"
    if not agents.is_dir():
        return []
    candidates = [p for p in sorted(agents.iterdir())
                  if p.is_dir() and not is_link(p) and p.name not in KEEP_IN_AGENTS
                  and (repo / p.name / "SKILL.md").exists() and not (p / ".pm-os-source").exists()
                  and (claude / p.name / ".pm-os-source").exists()]
    if len(candidates) > TRASH_CAP:
        return [f"✗ {len(candidates)} old Amp copies found, over the cap of {TRASH_CAP}; none moved"]
    lines = []
    for path in candidates:
        if dry_run:
            lines.append(f"→ {path.name}: would move the old Amp copy {path} to the trash")
            continue
        ok, where = move_to_trash(path, main)
        lines.append(f"{'♻' if ok else '✗'} {path.name}: old Amp copy {'moved to ' + where if ok else 'not moved: ' + where}")
    return lines


CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
CACHE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
CLAUDE_OWN_FOLDERS = {"synced"}


def is_cache_path(rel: Path) -> bool:
    """A file a tool regenerates on its own: bytecode, a test cache, Finder or Explorer litter."""
    return (bool(CACHE_DIRS.intersection(rel.parts[:-1])) or rel.suffix in {".pyc", ".pyo"}
            or rel.name in CACHE_FILES)


HUSK_MIN_AGE_SECONDS = 3600


def husk_roots(main: Path, home: Path) -> list[Path]:
    """Shared skill sources in the main checkout, then the installed copies. The private
    skills folder is left out: it is gitignored, so git cannot vouch for it, and Dropbox
    syncs it between machines, so a folder there may be half downloaded."""
    return [main / ".agents" / "skills", main / "system" / "vendor" / "skills", home / ".claude" / "skills"]


def newest_mtime(folder: Path) -> float:
    stamps = [folder.stat().st_mtime]
    stamps += [p.stat().st_mtime for p in folder.rglob("*")]
    return max(stamps)


def git_tracks(main: Path, path: Path) -> bool:
    """True when git tracks any file under path, or git cannot say (then the folder is left)."""
    try:
        rel = path.relative_to(main)
    except ValueError:
        return False
    result = run(["git", "-C", str(main), "ls-files", "--", rel.as_posix()], timeout=30)
    return result.returncode != 0 or bool(result.stdout.strip())


def prune_skill_husks(main: Path, home: Path, dry_run: bool) -> list[str]:
    """Skill folders left with no SKILL.md and nothing but cache files.

    Why: a commit that deletes a skill removes its tracked files, and the fast-forward
    of main does the same, but git leaves the gitignored __pycache__ a test run wrote.
    The folder then holds only scripts/__pycache__ and tests/__pycache__, and a skill
    check counts it as a broken skill. A husk is a folder that is not a link,
    has no SKILL.md, holds at least one file and only cache files, has not changed in the
    last hour, and, inside the main checkout, has no file git tracks. It goes to the trash
    (Recycle Bin or macOS Trash) through pm-os-trash.py, never a plain delete.
    """
    lines: list[str] = []
    now = time.time()
    for root in husk_roots(main, home):
        if not root.is_dir() or is_link(root):
            continue
        for folder in sorted(p for p in root.iterdir() if p.is_dir() and not is_link(p)):
            if (folder / "SKILL.md").exists():
                continue
            if any(is_link(p) for p in folder.rglob("*")):
                continue
            files = [p for p in folder.rglob("*") if not p.is_dir()]
            # An empty folder may be a skill still arriving (a Dropbox download, a copy in
            # flight, Claude's own synced/ folder): only cache files left behind count.
            if not files or any(not is_cache_path(p.relative_to(folder)) for p in files):
                continue
            installed = not folder.is_relative_to(main)
            if installed and folder.name in CLAUDE_OWN_FOLDERS:
                continue
            try:
                if now - newest_mtime(folder) < HUSK_MIN_AGE_SECONDS:
                    continue  # touched in the last hour: files may still be arriving
            except OSError:
                continue
            if git_tracks(main, folder):
                continue
            where = f"~/.claude/skills/{folder.name}" if installed else folder.relative_to(main).as_posix()
            if dry_run:
                lines.append(f"→ {folder.name}: would move the empty skill folder {where} ({len(files)} cache files) to the trash")
                continue
            ok, said = move_to_trash(folder, main)
            if not ok:
                lines.append(f"✗ {folder.name}: empty skill folder {where} not moved: {said}")
                continue
            lines.append(f"♻ {folder.name}: moved the empty skill folder {where} ({len(files)} cache files) to {said}")
    return lines


def is_link(path: Path) -> bool:
    """A symlink or a Windows junction: never a tidy candidate (the trash helper refuses links)."""
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def cos_line(main: Path, text: str, status: str = "done") -> bool:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cmd = ["bash", str(main / "system/scripts/report-to-cos.sh"), "--task-id", f"skill-sync-{stamp}", "--origin", "cos",
           "--workstream", "skills-system", "--status", status, "--create-if-missing", text]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PM_OS_WORKER")}
    env["REPORT_TO_COS_NO_HOOKS"] = "1"
    result = run(cmd, timeout=180, env=env)
    return result.returncode == 0


def notify(main: Path, state: dict, notices: list[dict], failed: bool, failure_text: str, send: bool) -> list[str]:
    """Pick the lines worth sending, dedupe them against state, send at most one CoS line."""
    sent = state.setdefault("sent", {})
    lines, keys = [], []
    # A standing condition is news once; when it clears, its key goes, so a return is news again.
    standing_now = {f"{n['kind']}:{n['name']}" for n in notices if n["kind"] in STANDING_KINDS}
    for key in [k for k in sent if k.split(":", 1)[0] in STANDING_KINDS and k not in standing_now]:
        sent.pop(key)
    for item in notices:
        if item["kind"] == "install-failed":
            continue  # counted in the failure streak, not one line per skill
        key = f"{item['kind']}:{item.get('backup') or item['name']}"
        if key in sent:
            continue
        keys.append(key)
        if item["kind"] == "unmanaged":
            lines.append(f"{item['text']}. Follow-up: the owner says whether {item['name']} moves to .agents/skills "
                         f"(shared) or local/skills (private), or goes.")
        else:
            lines.append(item["text"])
    streak = state.get("failure_streak", 0) + 1 if failed else 0
    state["failure_streak"] = streak
    if failed and streak >= FAILURE_ALERT_AFTER and f"failure-streak:{state.get('last_ok_at', 'never')}" not in sent:
        lines.append(f"skill sync failed {streak} runs in a row; installed skills may be stale. Last error: {failure_text[:300]}")
        keys.append(f"failure-streak:{state.get('last_ok_at', 'never')}")
    if not lines:
        return []
    text = "Skill sync: " + " ".join(line.rstrip(".") + "." for line in lines)
    if send and cos_line(main, text, status="failed" if failed and streak >= FAILURE_ALERT_AFTER else "done"):
        for key in keys:
            sent[key] = now_iso()
    return lines


def main_cli(argv: list[str]) -> int:
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reason", default="manual", help="tick | landing | manual (logged only)")
    parser.add_argument("--notify", action="store_true", help="send CoS one deduped line for events and repeated failure")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    parser.add_argument("--home", type=Path, default=None, help="alternate home, for tests")
    parser.add_argument("--state", type=Path, default=None, help="state file (default system/state/skill-sync.json)")
    args = parser.parse_args(argv)

    if os.environ.get("SKILL_SYNC_DISABLED") == "1":
        print("skill sync: disabled (SKILL_SYNC_DISABLED=1)")
        return 0
    if os.environ.get("PM_OS_TEST_SANDBOX") == "1" and args.home is None:
        print("skill sync: skipped in the test sandbox (real home)")
        return 0
    home = args.home or Path.home()
    main = main_checkout()
    state_path = args.state or main / "system/state/skill-sync.json"
    locks = Path(os.environ.get("PM_OS_LOCKS_DIR") or main / ".amp" / "locks")
    print(f"skill sync ({args.reason}) from {main} at {now_iso()}")

    with try_lock(locks / "skill-sync.lock") as mine:
        if not mine:
            print("skill sync: another sync is running; skipped")
            return 0
        with try_lock(locks / "landing.lock") as free:
            if not free:
                print("skill sync: a landing is in flight; skipped (the post-landing run follows)")
                return 0
            if rebase_in_flight(main):
                print("skill sync: a rebase is in flight; skipped")
                return 0
            # Held to the end: a landing waits these few seconds rather than change sources mid-copy.
            return synced(args, main, home, state_path)


def synced(args: argparse.Namespace, main: Path, home: Path, state_path: Path) -> int:
    """One run under both locks: overrides, install, old Amp copies, Codex copies, notices."""
    state = load_state(state_path)
    errors: list[str] = []
    if not args.dry_run:
        ok, detail = apply_overrides(main, home)
        print(f"{'✓' if ok else '✗'} overrides: {detail}")
        if not ok:
            errors.append(f"overrides: {detail}")

    for line in prune_skill_husks(main, home, args.dry_run):
        print(line)
        if line.startswith("✗"):
            errors.append(line)

    with tempfile.TemporaryDirectory(prefix="pm-os-skill-sync-") as tmp:
        notices_path = Path(tmp) / "notices.json"
        # --skip-missing: a library skill this machine lacks (firecrawl on a kit Mac) is dormant,
        # never a failure that stops every tick; dormant copies are held, never pruned.
        # install-claude-skills.py --check stays strict for people and the kit verifier.
        extra = ["--prune", "--skip-missing", "--settings-state", str(state_path.with_name("skill-sync-settings.hash"))]
        if args.dry_run:
            extra.append("--dry-run")
        code, output = install(main, home, extra, notices_path)
        print(output)
        if code:
            errors.append(next((l for l in output.splitlines() if l.startswith("✗")), output[-300:]))
        notices = json.loads(notices_path.read_text(encoding="utf-8")) if notices_path.exists() else []

        for line in tidy_agents_copies(main, home, args.dry_run):
            print(line)
            if line.startswith("✗"):
                errors.append(line)
        if shutil.which("codex"):
            # Codex reads ~/.agents/skills: managed pm-os copies go back there, marked, after the
            # unmarked old ones left for the trash. No --force, so a catalog skill folder that shares
            # a name is refused, never overwritten.
            codex_dest = str(home / ".agents" / "skills")
            code, output = install(main, home, ["--destination", codex_dest, "--source-kind", "pm-os",
                                                "--skip-missing", "--prune", "--no-settings",
                                                *(["--dry-run"] if args.dry_run else [])],
                                   Path(tmp) / "codex.json")
            print("codex copies:\n" + output)
            if code:
                errors.append("codex copies: " + next((l for l in output.splitlines() if l.startswith("✗")), output[-200:]))

    failed = bool(errors)
    if args.dry_run:
        return 1 if failed else 0
    sent = notify(main, state, notices, failed, "; ".join(errors), send=args.notify)
    for line in sent:
        print(f"notice: {line}")
    state.update(last_run_at=now_iso(), last_reason=args.reason, last_result="failed" if failed else "ok",
                 last_errors=errors[:5])
    if not failed:
        state["last_ok_at"] = state["last_run_at"]
    save_state(state_path, state)
    print(f"skill sync: {'failed: ' + '; '.join(errors) if failed else 'ok'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_cli(sys.argv[1:]))
