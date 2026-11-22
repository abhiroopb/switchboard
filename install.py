#!/usr/bin/env python3
"""Install or update PM OS from this kit checkout into your own PM OS repo.

The kit writes owner identity as placeholders ({{OWNER_NAME}}, {{OWNER_EMAIL}}, {{PM_OS_ROOT}},
...). This installer verifies the kit against its manifest, renders the
placeholders with your values, writes the tree to --destination, creates the
personal folders the kit deliberately leaves out (people/, notes/, workstreams/),
and makes the first git commit.

Modes:
  --dry-run   (default) print the plan as JSON and write nothing
  --apply     fresh install; the destination must not exist or must be empty
  --update    refresh an existing install from a newer kit; files you have
              personalized or edited are left alone and listed

Setup decisions (scope, Windows runtime, organization tools, sidebar,
connectors) are fixed in docs/install-decisions.md. This installer detects the
environment, applies them, and prints one "decision:" line per row. Three
choices belong to the owner and are asked, never assumed: where the install
folder goes (--destination; prompted on a terminal when omitted), whether to
run the full loop, and which Claude Code memory to use (docs/install-decisions.md,
"Owner choices"). Agents: apply the fixed rows, ask only the owner choices.

Example:
  sh install.sh --owner-name "Jordan Rivera" --owner-email jordan@example.com --dry-run
  sh install.sh --owner-name "Jordan Rivera" --owner-email jordan@example.com --apply
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import time
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent
MANIFEST = KIT_ROOT / ".pm-os-kit" / "source-manifest.json"
DEFAULT_DESTINATION = Path.home() / "Development" / "pm-os"
DESTINATION_PROMPT = (
    "Where should PM OS live? This folder holds the repo, your notes, people profiles and\n"
    "workstreams. Suggested: {suggested}. Type another path to choose a different\n"
    "folder or drive, or press Enter to accept. A synced folder such as Dropbox works; run PM OS\n"
    "from one device at a time there, because git locks and the background jobs clash across devices."
)
INSTALL_RECORD = ".pm-os-kit/install.json"

# Kit-only files: they describe or drive the kit and are not part of a PM OS checkout.
KIT_ONLY = {"install.sh", "install.py", "README.md", "CONTRIBUTING.md", "NOTICE.md"}

# Files a PM personalizes after the first install. --update never overwrites
# them when they already exist in the destination.
PERSONAL_FILES = {
    "AGENTS.md",
    "HOME.md",
    "system/chief-of-staff-prompt.md",
    "system/background-tasks.yaml",
    "system/scheduled-jobs.yaml",
    "routines/scheduled-jobs/CONTEXT.md",
    "workstreams/README.md",
}

# Files copied once into git-ignored runtime folders when absent (never overwritten).
SEED_FILES = {
    "templates/people/README.md": "people/README.md",
    "templates/people/_template.md": "people/_template.md",
}

PLACEHOLDERS = (
    "OWNER_NAME", "OWNER_FIRST", "OWNER_NAME_LOWER", "OWNER_EMAIL", "OWNER_USER", "OWNER_ROLE",
    "OWNER_GITHUB", "OWNER_SLACK_USER_ID", "PM_OS_SLACK_BOT_CHANNEL", "PM_OS_ROOT", "HOME",
)
PLACEHOLDER_RE = re.compile(r"\{\{(" + "|".join(PLACEHOLDERS) + r")\}\}")

DECISIONS_DOC = "docs/install-decisions.md"

# Owner data the installer and every cleanup must leave alone: local/backups holds
# the only copy of ~/.claude, ~/.pm-os and the commit history (docs/backup-and-restore.md).
# A restore the owner approves is the one exception: local_backup.py brings the owner
# files back into <destination>/local, and never over a newer file.
PROTECTED_DIRS = ("local",)
BACKUP_SCRIPT = "system/scripts/local_backup.py"


def is_protected(rel: str) -> bool:
    """True for a repo-relative path inside a protected folder."""
    first = rel.replace("\\", "/").removeprefix("./").split("/", 1)[0]
    return first.lower() in PROTECTED_DIRS

# Path segments that mark a sync client's folder. PM OS keeps git, worktrees,
# symlinks, SQLite and fast-changing sidecar state; sync clients corrupt them.
SYNCED_SEGMENTS = (
    "dropbox", "onedrive", "icloud", "com~apple~clouddocs", "mobile documents",
    "google drive", "googledrive", "cloudstorage", "box sync", "box-box",
)

RUNTIME_DIRS = (
    "people",
    "notes",
    "notes/daily",
    "outputs",
    "outputs/reviews",
    "projects",
    "workstreams/_archive",
    "system/state",
    "system/state/logs",
    "system/state/sources",
    "system/logs",
    ".state",
    ".amp/worktrees",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_text(data: bytes) -> bool:
    return b"\x00" not in data[:8192]


def log(message: str) -> None:
    print(message, file=sys.stderr)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--destination", type=Path, default=None,
                    help="where your PM OS repo lives (any folder or drive, a synced one included). Omitted: "
                         "asked on a terminal with a suggestion ($PM_OS_ROOT, else the folder recorded in "
                         f"~/.pm-os/env, else {DEFAULT_DESTINATION}); non-interactive runs take the suggestion")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan and write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="fresh install into an empty destination")
    mode.add_argument("--update", action="store_true", help="refresh an existing install, keeping personalized files")
    ap.add_argument("--owner-name", help="your full name as it appears in Slack and Gmail")
    ap.add_argument("--owner-email", help="your work email")
    ap.add_argument("--owner-role", help="your job title for the Role line of AGENTS.md (default: Product Manager)")
    ap.add_argument("--owner-user", help="your username (default: the local part of --owner-email)")
    ap.add_argument("--owner-github", help="your GitHub username (default: same as --owner-user)")
    ap.add_argument("--owner-slack-user-id", default="", help="your Slack member ID (U...); optional")
    ap.add_argument("--slack-bot-channel", default="", help="Slack channel ID your PM OS bot posts to; optional")
    ap.add_argument("--profile", choices=("auto", "company", "personal"), default="auto",
                    help="organisation profile: company (an overlay your organization writes) or personal; auto picks company when an organization pmos CLI is on PATH")
    ap.add_argument("--force", action="store_true", help="with --update: also overwrite files you edited locally (personal files still kept)")
    ap.add_argument("--no-git", action="store_true", help="with --apply: skip git init and the first commit")
    ap.add_argument("--allow-synced-path", action="store_true",
                    help=f"accepted for older scripts; a synced folder is allowed by default with worktrees and state relocated (see {DECISIONS_DOC})")
    ap.add_argument("--restore-backup", choices=("ask", "yes", "no"), default="ask",
                    help="a backup found under local/backups (destination, recorded install, or Dropbox): ask on a terminal "
                         "(default), restore it, or leave it. Newer local files are never overwritten. With no terminal, "
                         "ask restores nothing and prints the one-line command")
    args = ap.parse_args(argv)
    if not (args.apply or args.update):
        args.dry_run = True
    return args


def recorded_root(env: dict[str, str] | None = None) -> Path | None:
    """PM_OS_ROOT from the env file a previous install wrote (PM_OS_STATE_HOME/env).

    Lets `--update` find the folder the owner chose at install time without
    the shell having sourced that file. Returns None when no record exists.
    """
    env = os.environ if env is None else env
    env_file = Path(env.get("PM_OS_STATE_HOME") or Path.home() / ".pm-os") / "env"
    try:
        for line in env_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "PM_OS_ROOT" and value.strip():
                return Path(value.strip())
    except OSError:
        pass
    return None


def default_destination(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("PM_OS_ROOT"):
        return Path(env["PM_OS_ROOT"])
    return recorded_root(env) or DEFAULT_DESTINATION


def ask_destination(suggested: Path, stdin=None, stdout=None) -> Path:
    """Owner choice 1 (docs/install-decisions.md, Owner choices): where the install folder goes.

    Asked on a terminal whenever --destination is absent; PM_OS_ROOT and the
    recorded folder only shape the suggestion. Empty answer takes the
    suggestion. install.ps1 asks the same question with the same suggestion.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stderr if stdout is None else stdout
    print(DESTINATION_PROMPT.format(suggested=suggested), file=stdout)
    print(f"Install folder [{suggested}]: ", end="", file=stdout, flush=True)
    answer = clean_answer(stdin.readline())
    return Path(answer).expanduser() if answer else suggested


def clean_answer(raw: str) -> str:
    """Trim the answer and one pair of surrounding quotes ("Copy as path" in Explorer adds them)."""
    answer = raw.strip()
    if len(answer) >= 2 and answer[0] == answer[-1] and answer[0] in "\"'":
        answer = answer[1:-1].strip()
    return answer


def destination_source(env: dict[str, str] | None = None) -> str:
    """Where the suggestion came from, for the non-interactive message."""
    env = os.environ if env is None else env
    if env.get("PM_OS_ROOT"):
        return "PM_OS_ROOT"
    if recorded_root(env):
        return "the folder recorded in the PM OS env file"
    return "the default"


def owner_values(args: argparse.Namespace, destination: Path) -> dict[str, str]:
    if args.update and not (args.owner_name and args.owner_email):
        record = destination / INSTALL_RECORD
        if record.exists():
            saved = json.loads(record.read_text()).get("values", {})
            args.owner_name = args.owner_name or saved.get("OWNER_NAME")
            args.owner_email = args.owner_email or saved.get("OWNER_EMAIL")
            args.owner_user = args.owner_user or saved.get("OWNER_USER")
            args.owner_role = args.owner_role or saved.get("OWNER_ROLE")
            args.owner_github = args.owner_github or saved.get("OWNER_GITHUB")
            args.owner_slack_user_id = args.owner_slack_user_id or saved.get("OWNER_SLACK_USER_ID", "")
            args.slack_bot_channel = args.slack_bot_channel or saved.get("PM_OS_SLACK_BOT_CHANNEL", "")
    if not args.owner_name or not args.owner_email:
        sys.exit("--owner-name and --owner-email are required")
    if "@" not in args.owner_email:
        sys.exit(f"--owner-email does not look like an email: {args.owner_email}")
    user_name = args.owner_user or args.owner_email.split("@", 1)[0]
    return {
        "OWNER_NAME": args.owner_name.strip(),
        "OWNER_FIRST": args.owner_name.strip().split()[0],
        "OWNER_NAME_LOWER": args.owner_name.strip().lower(),
        "OWNER_EMAIL": args.owner_email.strip(),
        "OWNER_USER": user_name,
        "OWNER_ROLE": (args.owner_role or "Product Manager").strip(),
        "OWNER_GITHUB": args.owner_github or user_name,
        "OWNER_SLACK_USER_ID": args.owner_slack_user_id or "",
        "PM_OS_SLACK_BOT_CHANNEL": args.slack_bot_channel or "",
        # Forward slashes on every OS: a C:/... home reads the same in Python,
        # Node, Bun and Git Bash, while the backslash form turns "\U" and
        # friends into escape sequences inside a Python or TypeScript string.
        "PM_OS_ROOT": destination.as_posix(),
        "HOME": Path.home().as_posix(),
    }


def load_manifest() -> dict[str, dict[str, str]]:
    if not MANIFEST.exists():
        sys.exit(f"kit manifest missing: {MANIFEST}. Run this from a clone of the kit repo.")
    return json.loads(MANIFEST.read_text())["files"]


def verify_kit(files: dict[str, dict[str, str]]) -> list[str]:
    """Return problems; the kit must match its manifest exactly before anything renders."""
    problems: list[str] = []
    for rel, meta in files.items():
        p = KIT_ROOT / rel
        if p.is_symlink():
            problems.append(f"symlink in kit: {rel}")
        elif not p.is_file():
            problems.append(f"missing from kit: {rel}")
        elif sha256(p.read_bytes()) != meta["sha256"]:
            problems.append(f"checksum mismatch: {rel}")
    return problems


def render(data: bytes, values: dict[str, str]) -> bytes:
    if not is_text(data):
        return data
    text = data.decode("utf-8", errors="surrogateescape")
    text = PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)
    return text.encode("utf-8", errors="surrogateescape")


def git_executable(which=shutil.which, env: dict[str, str] | None = None) -> str:
    """git on PATH, else Git for Windows' usual folders (winget does not always add Git\\cmd to PATH)."""
    found = which("git")
    if found:
        return found
    env = os.environ if env is None else env
    for base in (env.get("ProgramFiles"), env.get("ProgramW6432"), env.get("ProgramFiles(x86)"),
                 str(Path(env["LOCALAPPDATA"]) / "Programs") if env.get("LOCALAPPDATA") else None):
        if not base:
            continue
        for rel in ("Git/cmd/git.exe", "Git/bin/git.exe"):
            candidate = Path(base) / rel
            if candidate.is_file():
                return str(candidate)
    return "git"


def kit_version() -> str:
    """Kit checkout commit when cloned, else the KIT_VERSION stamp the publisher wrote (zip downloads)."""
    try:
        top = subprocess.run([git_executable(), "-C", str(KIT_ROOT), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        # an unzipped kit inside some other repo must not borrow that repo's commit
        if top and Path(top).resolve() == KIT_ROOT.resolve():
            out = subprocess.run([git_executable(), "-C", str(KIT_ROOT), "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            if out:
                return out
    except (OSError, subprocess.CalledProcessError):
        pass
    stamp = KIT_ROOT / "KIT_VERSION"
    if stamp.is_file():
        text = stamp.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text
    return "unknown"


def synced_segment(path: Path) -> str | None:
    """Return the path segment that marks a sync client's folder, or None."""
    for part in path.parts:
        low = part.lower()
        if any(marker in low for marker in SYNCED_SEGMENTS):
            return part
    return None


def detect_runtime(env: dict[str, str] | None = None, system: str | None = None) -> str:
    """macos | wsl | windows-native | linux, honoring PM_OS_RUNTIME=wsl|native."""
    env = os.environ if env is None else env
    system = system or platform.system()
    forced = env.get("PM_OS_RUNTIME", "").strip().lower()
    if forced in ("wsl", "native"):
        return "wsl" if forced == "wsl" else "windows-native"
    if system == "Darwin":
        return "macos"
    if system == "Windows" or env.get("MSYSTEM") or system.startswith(("MINGW", "MSYS", "CYGWIN")):
        return "windows-native"
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return "wsl"
    try:
        if "microsoft" in Path("/proc/version").read_text().lower():
            return "wsl"
    except OSError:
        pass
    return "linux"


def detect_mux(runtime: str, which=shutil.which) -> str:
    """cmux | wmux | none. Mirrors system/scripts/lib/mux.sh mux_detect without sourcing it."""
    forced = os.environ.get("PM_OS_MUX", "").strip().lower()
    if forced in ("cmux", "wmux", "none"):
        return forced
    if os.environ.get("WMUX") == "1":
        return "wmux"
    if runtime == "macos":
        if which("cmux") or Path("/Applications/cmux.app/Contents/Resources/bin/cmux").exists():
            return "cmux"
        return "none"
    if runtime in ("wsl", "windows-native") and (which("wmux") or which("wmux.cmd") or os.environ.get("WMUX_BIN")):
        return "wmux"
    return "none"


def state_home(runtime: str, env: dict[str, str] | None = None) -> Path:
    """PM_OS_STATE_HOME: fast-changing state that never lives in a synced folder."""
    env = os.environ if env is None else env
    if env.get("PM_OS_STATE_HOME"):
        return Path(env["PM_OS_STATE_HOME"])
    return Path.home() / ".pm-os"


def worktrees_dir(runtime: str, env: dict[str, str] | None = None) -> Path:
    """PM_OS_WORKTREES_DIR: task worktrees outside the checkout (decisions doc section 4)."""
    env = os.environ if env is None else env
    if env.get("PM_OS_WORKTREES_DIR"):
        return Path(env["PM_OS_WORKTREES_DIR"])
    if runtime == "windows-native":
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "pm-os" / "worktrees"
    return Path.home() / ".local" / "state" / "pm-os" / "worktrees"


def pmos_kind(which=shutil.which) -> str:
    """"real", "shim", or "absent": the one rule in system/connectors/company_cli.py, shared
    with install.ps1, verify-kit-install.sh, and install-amp-skills.sh, so the shim on
    PATH never reads as an organization CLI."""
    helper = KIT_ROOT / "system" / "connectors" / "company_cli.py"
    try:
        spec = importlib.util.spec_from_file_location("pm_os_company_cli", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.pmos_kind(which=which)[0]
    except (OSError, ImportError, AttributeError):
        return "real" if which("pmos") else "absent"


def connectors_decision(internal_tools: list[str]) -> str:
    # docs/install-decisions.md section 6: an organization pmos CLI when present, else the kit's shim.
    if "pmos" in internal_tools:
        return "pmos (organization CLI)"
    return "pmos shim (system/connectors; run pmos login)"


def detect_environment(destination: Path, allow_synced: bool = False, which=shutil.which) -> dict:
    """Apply docs/install-decisions.md to this machine. Pure read; nothing is written."""
    runtime = detect_runtime()
    mux = detect_mux(runtime, which)
    harnesses = [h for h in ("amp", "claude", "codex", "cursor") if which(h)]
    internal_tools = [t for t in ("pmos", "gh") if which(t)]
    if "pmos" in internal_tools and pmos_kind(which) != "real":
        # The kit's connector shim (system/connectors/pmos) is not an organization CLI.
        internal_tools.remove("pmos")
    synced = synced_segment(destination)
    scope = "full-loop" if "amp" in harnesses and mux in ("cmux", "wmux") else "policy-and-skills"
    if synced:
        location_state = f"synced folder '{synced}'; worktrees and state relocated"
    else:
        location_state = "not synced"
    return {
        "doc": DECISIONS_DOC,
        "runtime": runtime,
        "mux": mux,
        "harnesses": harnesses,
        "internal_tools": internal_tools,
        "location": {
            "path": str(destination),
            "synced_segment": synced,
            "state": location_state,
            "worktrees_dir": str(worktrees_dir(runtime)),
            "state_home": str(state_home(runtime)),
        },
        "scope": scope,
        "internal_tooling": "active" if "pmos" in internal_tools else "dormant",
        "sidebar": sidebar_decision(scope, mux),
        "connectors": connectors_decision(internal_tools),
        "profile": "company" if "pmos" in internal_tools else "personal",
    }


def sidebar_decision(scope: str, mux: str) -> str:
    # docs/install-decisions.md section 5: the native wmux sidebar carries the lanes through
    # set-status, blocked rows through report-agent, and unread through targeted notify.
    # Pins, colors, and order reach wmux only through seed-session.ps1 -Restart.
    # The PM OS board page is opt-in (PM_OS_BOARD_AUTOSTART=1).
    if scope != "full-loop":
        return "none (no terminal backend)"
    if mux == "wmux":
        return ("wmux-native (set-status lanes, report-agent blocked, targeted notify; "
                "seed-session.ps1 -Restart for pins, colors, order; panes open in Git Bash; board opt-in)")
    return "cmux-sidebar (native)"


def decision_lines(d: dict) -> list[str]:
    harness = ",".join(d["harnesses"]) or "none"
    pmos = "pmos on PATH" if d["internal_tooling"] == "active" else "pmos not on PATH"
    return [
        f"decision: location   {d['location']['path']} ({d['location']['state']})",
        f"decision: scope      {d['scope']} (amp: {'yes' if 'amp' in d['harnesses'] else 'no'}, mux: {d['mux']}, harnesses: {harness})",
        f"decision: runtime    {d['runtime']}",
        f"decision: internal-tooling  {d['internal_tooling']} ({pmos})",
        f"decision: sidebar    {d['sidebar']}",
        f"decision: connectors {d['connectors']}",
        f"decision: profile    {d['profile']} (profiles/{d['profile']}; company only with an organization pmos CLI)",
        f"decisions are fixed in {d['doc']}; agents apply them and do not ask",
        f"owner choices (asked, never assumed): install folder, full loop, Claude Code memory; see {d['doc']}, Owner choices",
    ]


def next_steps(d: dict) -> list[str]:
    out = ["next: bash system/scripts/verify-kit-install.sh  (from the destination)"]
    if d["location"]["synced_segment"]:
        out.append(f"synced folder: worktrees go to {d['location']['worktrees_dir']}, state to {d['location']['state_home']}; "
                   "use this checkout from one device at a time")
    if d["scope"] == "full-loop":
        out.append("scope full-loop (owner choice 2, see docs/install-decisions.md): run README steps 5 to 9 "
                   "(skills, Amp plugins, personalize, configs, background loop)")
        if d["runtime"] == "windows-native":
            out.append("Windows full loop: then run system\\windows\\install.ps1 -DryRun, then without -DryRun, from PowerShell; "
                       "see docs/install-windows.md steps 7 to 13")
        elif d["runtime"] == "wsl":
            out.append("WSL2 (override): run system\\windows\\install.ps1 -Runtime wsl from PowerShell; see docs/install-windows.md, WSL2 section")
    else:
        out.append("scope policy-and-skills: run README steps 5, 7, 8, then bash adapters/install-adapters.sh --apply; skip plugins, launchd and the background loop")
        if d["runtime"] == "windows-native":
            out.append("Windows native: Python, Git for Windows and ripgrep from winget are the whole toolchain "
                       "(Bun too for Claude Code hooks; install.ps1 -PrereqsOnly puts them on PATH); "
                       "run system\\windows\\install.ps1 -SkipTasks -SkipSeed for PATH and env only; see docs/install-windows.md. "
                       "The full loop (dispatcher, workers, daemons, scheduled jobs) is not recommended on native Windows yet: "
                       "it has not had an end-to-end run there (docs/install-decisions.md, Owner choices)")
    if "claude" in d["harnesses"]:
        out.append("Claude Code: from a plain terminal (not inside a Claude Code session), "
                   "bash system/scripts/install-claude-code.sh --dry-run, then without --dry-run "
                   "(skills copy, plugins, MCP servers; Claude Code's built-in memory is kept by default). "
                   "Owner choice 3: add --memory claude-mem only if the owner wants the third-party worker instead; "
                   "that also turns off the auto-updater and built-in memory")
    elif d["scope"] != "full-loop":
        out.append("Claude Code (optional): install it, then bash system/scripts/install-claude-code.sh")
    if d["internal_tooling"] == "dormant":
        out.append("connectors: run pmos login google, then pmos login slack, then pmos doctor "
                   "(system/connectors; see .agents/skills/using-connectors/SKILL.md)")
    return out


def plan(files: dict[str, dict[str, str]], values: dict[str, str], destination: Path, update: bool,
         force: bool) -> dict:
    previous: dict = {}
    if update:
        record = destination / INSTALL_RECORD
        if not record.exists():
            sys.exit(f"--update needs an existing install record at {record}")
        previous = json.loads(record.read_text())
    prev_files: dict[str, str] = previous.get("files", {})

    write: list[str] = []
    keep_personal: list[str] = []
    keep_modified: list[str] = []
    remove: list[str] = []
    unchanged: list[str] = []
    rendered: dict[str, bytes] = {}
    for rel in sorted(files):
        if rel in KIT_ONLY or rel.startswith(".pm-os-kit/") or is_protected(rel):
            continue
        data = render((KIT_ROOT / rel).read_bytes(), values)
        rendered[rel] = data
        target = destination / rel
        if not update:
            write.append(rel)
            continue
        if rel in PERSONAL_FILES and target.exists():
            keep_personal.append(rel)
            continue
        if target.exists():
            current = sha256(target.read_bytes())
            if current == sha256(data):
                unchanged.append(rel)
                continue
            if rel in prev_files and prev_files[rel] != current and not force:
                keep_modified.append(rel)
                continue
        write.append(rel)
    if update:
        for rel, installed_sha in sorted(prev_files.items()):
            if rel in rendered or rel in PERSONAL_FILES or is_protected(rel):
                continue
            target = destination / rel
            if target.exists() and sha256(target.read_bytes()) == installed_sha:
                remove.append(rel)
    # example configs and runtime templates become live files once, never overwritten afterwards
    seed_configs = []
    for rel in rendered:
        live = None
        if rel.startswith("system/config/") and rel.endswith(".example.yaml"):
            live = rel.replace(".example.yaml", ".yaml")
        elif rel in SEED_FILES:
            live = SEED_FILES[rel]
        if live and not (destination / live).exists():
            seed_configs.append((rel, live))
    return {
        "mode": "update" if update else "apply",
        "kit_version": kit_version(),
        "destination": str(destination),
        "values": {k: v for k, v in values.items() if k not in ("HOME",)},
        "write": write,
        "unchanged": unchanged,
        "keep_personal": keep_personal,
        "keep_locally_modified": keep_modified,
        "remove": remove,
        "seed_configs": [live for _, live in seed_configs],
        "runtime_dirs": list(RUNTIME_DIRS),
        "_rendered": rendered,
        "_seed_pairs": seed_configs,
    }


def leftover_placeholders(rendered: dict[str, bytes]) -> list[str]:
    out = []
    for rel, data in rendered.items():
        if is_text(data) and PLACEHOLDER_RE.search(data.decode("utf-8", errors="replace")):
            out.append(rel)
    return out


def python_syntax_errors(rendered: dict[str, bytes]) -> list[str]:
    """Compile every rendered .py with this interpreter; one line per file that fails.

    Runs before anything is written, so a path value that breaks a string
    literal stops the install instead of shipping a dispatcher that cannot start.
    """
    errors = []
    for rel, data in sorted(rendered.items()):
        if not rel.endswith(".py") or rel.startswith("system/vendor/") or not is_text(data):
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # invalid-escape warnings are not failures
                compile(data.decode("utf-8", errors="surrogateescape"), rel, "exec")
        except (SyntaxError, ValueError) as exc:
            errors.append(f"{rel}:{getattr(exc, 'lineno', '?')}: {exc.__class__.__name__}: {getattr(exc, 'msg', exc)}")
    return errors


def write_env_file(d: dict, destination: Path) -> Path:
    """PM_OS_STATE_HOME/env: KEY=VALUE lines every shell and scheduled task sources."""
    home = Path(d["location"]["state_home"])
    home.mkdir(parents=True, exist_ok=True)
    lines = {
        "PM_OS_ROOT": str(destination),
        "PM_OS_WORKTREES_DIR": d["location"]["worktrees_dir"],
        "PM_OS_STATE_HOME": str(home),
        "PM_OS_MUX": d["mux"],
        "PM_OS_RUNTIME": "native" if d["runtime"] == "windows-native" else d["runtime"],
        "PM_OS_SYNCED_SEGMENT": d["location"]["synced_segment"] or "",
        "PM_OS_BOARD_AUTOSTART": os.environ.get("PM_OS_BOARD_AUTOSTART", "0"),
    }
    target = home / "env"
    # Merge, never replace: install.ps1 and the owner add keys (PM_OS_PYTHON,
    # PM_OS_TOOL_PATH, PM_OS_RUBY, WMUX_BIN, PM_OS_PROFILE) that scheduled tasks
    # need. A test or second install must not repoint a live checkout either,
    # or drop those keys.
    existing: dict[str, str] = {}
    order: list[str] = []
    old_text = target.read_text() if target.exists() else ""
    for raw in old_text.splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, _, value = raw.partition("=")
            existing[key.strip()] = value.strip()
            order.append(key.strip())
    old_root = existing.get("PM_OS_ROOT")
    if (old_root and Path(old_root).resolve() != destination.resolve()
            and (Path(old_root) / "AGENTS.md").exists()
            and os.environ.get("PM_OS_ENV_REPOINT") != "1"):
        log(f"env: {target} keeps the live install (PM_OS_ROOT={old_root}); "
            f"set PM_OS_ENV_REPOINT=1 to point it at {destination}, or set PM_OS_STATE_HOME for a test install")
        # Another checkout's install: every key the live file has wins; only missing keys are added.
        lines = {k: v for k, v in lines.items() if k not in existing}
    if old_text:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (home / f"env.bak-{stamp}").write_text(old_text)
        for stale in sorted(home.glob("env.bak-*"))[:-5]:  # keep the five newest
            stale.unlink(missing_ok=True)
    merged = dict(existing)
    merged.update(lines)
    keys = [k for k in dict.fromkeys(order) if k in merged] + [k for k in lines if k not in existing]
    header = "# PM OS environment (KEY=VALUE). Written by install.py and install.ps1; edit by hand as needed.\n"
    target.write_text(header + "".join(f"{k}={merged[k]}\n" for k in keys))
    return target


DROPBOX_IGNORED = (".git", ".amp", ".state", "system/state")


def mark_dropbox_ignored(destination: Path, synced: str | None, system: str | None = None) -> list[str]:
    """Best effort: tell Dropbox to keep git, worktrees, and fast state local. Returns what was marked."""
    if not synced or "dropbox" not in synced.lower():
        return []
    system = system or platform.system()
    marked: list[str] = []
    for rel in DROPBOX_IGNORED:
        path = destination / rel
        path.mkdir(parents=True, exist_ok=True)
        try:
            if system == "Windows":
                Path(f"{path}:com.dropbox.ignored").write_text("1")
            elif system == "Darwin":
                subprocess.run(["xattr", "-w", "com.dropbox.ignored", "1", str(path)], check=True,
                               capture_output=True)
            else:
                os.setxattr(str(path), b"user.com.dropbox.ignored", b"1")  # type: ignore[attr-defined]
            marked.append(rel)
        except (OSError, subprocess.CalledProcessError, AttributeError):
            continue
    return marked


def unlink_retry(path: Path, attempts: int = 5) -> None:
    """Delete one file; on Windows retry while a sync client holds it
    ("Device or resource busy")."""
    for i in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if os.name != "nt" or i == attempts - 1:
                raise
            time.sleep(0.5 * 2 ** i)


def ensure_local_ignored(destination: Path) -> None:
    """Keep local/ out of every commit, even with an older or locally edited .gitignore:
    the install's own `git add --all` runs right after this."""
    ignore = destination / ".gitignore"
    text = ignore.read_text(encoding="utf-8", errors="replace") if ignore.exists() else ""
    if "/local/" not in text.split():
        prefix = "" if not text or text.endswith("\n") else "\n"
        ignore.write_text(text + prefix + "/local/\n", encoding="utf-8")


def execute(p: dict, files: dict[str, dict[str, str]], destination: Path, no_git: bool) -> dict[str, str]:
    rendered: dict[str, bytes] = p["_rendered"]
    installed: dict[str, str] = {}
    for rel in p["write"]:
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(rendered[rel])
        os.chmod(target, (KIT_ROOT / rel).stat().st_mode & 0o777)
    for rel in p["unchanged"] + p["keep_personal"] + p["keep_locally_modified"] + p["write"]:
        installed[rel] = sha256(rendered[rel])
    for rel in p["remove"]:
        if is_protected(rel):
            raise RuntimeError(f"refusing to delete protected path: {rel}")
        unlink_retry(destination / rel)
    for d in RUNTIME_DIRS:
        (destination / d).mkdir(parents=True, exist_ok=True)
    for example, live in p["_seed_pairs"]:
        shutil.copyfile(destination / example, destination / live)
    ensure_local_ignored(destination)
    record = destination / INSTALL_RECORD
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kit_version": p["kit_version"],
        "values": p["values"],
        "files": installed,
    }, indent=1, sort_keys=True) + "\n")
    decisions = p.get("decisions")
    if decisions:
        write_env_file(decisions, destination)
        marked = mark_dropbox_ignored(destination, decisions["location"]["synced_segment"])
        if marked:
            log(f"dropbox: marked com.dropbox.ignored on {', '.join(marked)}")
        elif decisions["location"]["synced_segment"]:
            log(f"synced folder '{decisions['location']['synced_segment']}': no ignore marker for this client; "
                "relocation of worktrees and state is the protection")
    if p["mode"] == "apply" and not no_git:
        git_bin = git_executable()
        if git_bin == "git" and not shutil.which("git"):
            log("git not found on PATH or in the Git for Windows folders; the files are written but not committed. "
                "Install Git (winget install Git.Git), open a new terminal, then run: "
                f'git -C "{destination}" init -b main and commit')
            return installed
        git = [git_bin, "-C", str(destination)]
        subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
        # The shipped .gitattributes keeps LF; core.autocrlf=false stops Git for
        # Windows from warning on every file.
        subprocess.run(git + ["config", "core.autocrlf", "false"], check=True)
        subprocess.run(git + ["add", "--all", "."], check=True)
        subprocess.run(git + ["-c", "user.name=PM OS kit", "-c", f"user.email={p['values']['OWNER_EMAIL']}",
                       "commit", "-q", "-m", f"Install PM OS kit {p['kit_version']}"], check=True)
    return installed


def load_backup_module(destination: Path):
    """local_backup.py from the kit, else from an existing install; None when neither has it."""
    for root in (KIT_ROOT, destination):
        path = root / BACKUP_SCRIPT
        if path.is_file():
            spec = importlib.util.spec_from_file_location("pm_os_local_backup", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module, path
    return None, None


def offer_backup(destination: Path, answer: str, dry_run: bool) -> dict:
    """Runs before anything is written, so env and settings come back before this
    install adds new ones. A problem here is reported and never stops the install."""
    module, script = load_backup_module(destination)
    if module is None:
        log("backup: this kit has no system/scripts/local_backup.py; backup check skipped")
        return {}
    try:
        return module.installer_offer(destination, answer=answer, dry_run=dry_run, log=log, script=script)
    except Exception as exc:  # the install goes on; nothing was changed
        log(f"backup: check failed ({type(exc).__name__}: {exc}); nothing restored, install continues")
        return {"error": str(exc)}


def blocks_fresh_install(destination: Path) -> bool:
    """A fresh install needs an empty folder, except for protected folders: a moved
    or synced folder can already hold local/backups, and it stays where it is."""
    if not destination.exists():
        return False
    return any(not is_protected(child.name) for child in destination.iterdir())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.destination is not None:
        destination = args.destination
    elif not sys.stdin.isatty():
        destination = default_destination()
        log(f"no --destination and no terminal: using the suggestion {destination} "
            f"(from {destination_source()}; pass --destination to choose)")
    else:
        destination = ask_destination(default_destination())
    destination = destination.expanduser().resolve()
    files = load_manifest()
    problems = verify_kit(files)
    if problems:
        for line in problems[:50]:
            log(f"  {line}")
        log(f"kit does not match its manifest ({len(problems)} problem(s)); re-clone and retry.")
        return 1
    decisions = detect_environment(destination, allow_synced=args.allow_synced_path)
    for line in decision_lines(decisions):
        log(line)
    if decisions["location"]["synced_segment"]:
        log(f"synced folder '{decisions['location']['synced_segment']}': allowed. Worktrees go to "
            f"{decisions['location']['worktrees_dir']} and state to {decisions['location']['state_home']}. "
            f"Use this checkout from one device at a time. See {DECISIONS_DOC}.")
    values = owner_values(args, destination)
    if args.apply and blocks_fresh_install(destination):
        log(f"destination exists and is not empty: {destination}. Use --update to refresh an install.")
        return 1
    p = plan(files, values, destination, update=args.update, force=args.force)
    p["decisions"] = decisions
    leftovers = leftover_placeholders(p["_rendered"])
    if leftovers:
        for rel in leftovers:
            log(f"  unrendered placeholder in {rel}")
        log("the kit contains placeholders this installer does not know; update install.py")
        return 1
    syntax = python_syntax_errors(p["_rendered"])
    if syntax:
        for line in syntax[:50]:
            log(f"  {line}")
        log(f"{len(syntax)} rendered Python file(s) do not compile; nothing was written. "
            "Report this with the lines above.")
        return 1
    # After validation, so an abort above has changed nothing, and before execute(), so
    # restored settings exist when write_env_file merges this install's keys. --update
    # is a routine refresh of a live machine: it reports unless told to restore.
    env_file = Path(decisions["location"]["state_home"]) / "env"
    env_was_there = env_file.exists()
    report_only = bool(args.dry_run) or (args.update and args.restore_backup == "ask")
    backup = offer_backup(destination, args.restore_backup, dry_run=report_only)
    if backup.get("restored") and not env_was_there:
        # The restored env names the old folder. The owner just picked this one.
        os.environ["PM_OS_ENV_REPOINT"] = "1"
    summary = {k: v for k, v in p.items() if not k.startswith("_")}
    summary["backup"] = backup
    summary["counts"] = {k: len(summary[k]) for k in ("write", "unchanged", "keep_personal", "keep_locally_modified", "remove", "seed_configs")}
    if args.dry_run:
        summary["dry_run"] = True
        print(json.dumps(summary, indent=1))
        log("dry run; nothing written. Re-run with --apply (fresh) or --update (existing install).")
        return 0
    execute(p, files, destination, no_git=args.no_git)
    summary["dry_run"] = False
    print(json.dumps(summary, indent=1))
    log(f"{p['mode']}: wrote {len(p['write'])} file(s) to {destination}")
    if p["keep_locally_modified"]:
        log(f"kept {len(p['keep_locally_modified'])} locally modified file(s); pass --force to overwrite them")
    profile = decisions["profile"] if args.profile == "auto" else args.profile
    apply_script = destination / "system" / "scripts" / "apply-profile.py"
    if apply_script.is_file():
        applied = subprocess.run([sys.executable, str(apply_script), "--profile", profile],
                                 capture_output=True, text=True)
        log(f"profile {profile}: " + (applied.stdout.strip() or applied.stderr.strip())[:300])
    else:
        log(f"profile {profile}: skipped (this kit has no system/scripts/apply-profile.py)")
    log("next: bash system/scripts/install-all.sh   (from the destination; on Windows from Git Bash). "
        "It installs skills, adapters, Amp plugins, Claude Code, the background services for this OS, and verifies; no questions.")
    for line in next_steps(decisions):
        log(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
