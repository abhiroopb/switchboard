#!/usr/bin/env python3
"""Nightly backup of ~/.claude, ~/.pm-os and the owner files into local/backups/<hostname>/<date>/.

local/ sits inside the PM OS repo, gitignored and left out of the kit, so a
synced repo folder (such as Dropbox) carries the copy off the laptop. One dated backup:

    local/backups/<hostname>/<YYYY-MM-DD>/
        manifest.json    counts, sizes, SHA-256 of every file and archive
        LEFT-OUT.txt     what was left out, why, and how to get each one back
        claude.zip       ~/.claude
        pm-os.zip        ~/.pm-os
        owner.zip        local/agents-local.md, local/user-lens.yaml, local/machines/, local/skills/
        repo.bundle      git bundle of all refs (.git never syncs through Dropbox)

Commands (run from anywhere; all paths resolve to the main checkout):

    python3 system/scripts/local_backup.py run             take a backup now
    python3 system/scripts/local_backup.py list            backups on disk, with sizes
    python3 system/scripts/local_backup.py verify          re-check the newest backup
    python3 system/scripts/local_backup.py restore         show what a restore would do
    python3 system/scripts/local_backup.py restore --apply restore; newer local files are kept
    python3 system/scripts/local_backup.py restore-git --into <folder>
    python3 system/scripts/local_backup.py health --alert  CoS line on failure or age over 48 h

Rules:
  * Secrets stay out. Credential files are never read into a backup. Other files
    are scanned for token patterns: a transcript keeps its text with the token
    replaced by [REDACTED:<kind>]; any other file with a hit is left out whole.
    A transcript also loses any run of plain lowercase words that reads like a recovery
    phrase. The scan is still pattern-based, so a short password in plain words is not caught.
  * Every backup is built in a .staging folder, verified member by member, then
    renamed into place. A refused or crashed run publishes nothing and deletes nothing.
    One case publishes on purpose: when only the git bundle fails, the verified home
    copy still goes out, with no repo.bundle, marked partial, and the run still counts
    as failed and alerts CoS. It prunes nothing, and a later run that day replaces it.
  * Retention runs only after a verified backup: 14 newest daily copies, then the
    newest copy of each of the 8 next weeks. It only touches this host's folder.
  * A restore never overwrites a local file newer than the backup copy, and a
    replaced older file goes to <root>/.pre-restore-<stamp>/ first.
  * The git history is scanned for token shapes every night. A hit withholds the
    bundle and alerts CoS, because a bundle cannot be redacted.
  * A run that would publish an empty or collapsed copy (a wrong folder, a wiped
    home) fails instead, so one bad night cannot push out good copies.
  * One process plus four git calls (log, bundle, verify, list-heads). No per-file
    forks: Windows has a process leak.

Standard library only and importable on its own, because the kit's install.py
loads this file before the destination exists. Guide: docs/backup-and-restore.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

SCHEMA = 1
DAILY_KEEP = 14
WEEKLY_KEEP = 8
ALERT_AGE_HOURS = 48
ALERT_REPEAT_HOURS = 24
SHRINK_LIMIT = 0.5  # a root under half its previous file count is refused
MTIME_SLOP = 2.0  # seconds; zip and filesystem clocks round
MAX_PLAIN_BYTES = 64 * 1024 * 1024  # non-transcript files larger than this are listed, not copied
STATE_FILE = "system/state/local-backup.json"
BUNDLE_NAME = "repo.bundle"
OWNER_ROOT = "owner"
ROOT_NAMES = ("claude", "pm-os", OWNER_ROOT)
# The only paths read under local/. Named one by one, so local/backups is never walked
# and a new folder there stays out until someone adds it here.
OWNER_PATHS = ("agents-local.md", "user-lens.yaml", "machines", "skills")
OWNER_SCRATCH_DIR = "pm-os-local"  # stands in for local/ under an explicit home
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ZIP_TIME_FLOOR = (1980, 1, 1, 0, 0, 0)

# ---------------------------------------------------------------- what stays out

RECOVERY = {
    "claude-login": "Sign in again: run claude, then /login. Nothing else depends on this file.",
    "google-connector": "Run: pmos login google. It opens the consent page and writes a new token.",
    "google-client": "Download the OAuth client JSON again from Google Cloud Console (APIs & Services, Credentials), "
                     "save it to ~/.pm-os/connectors/google-oauth-client.json, then run: pmos login google.",
    "slack-connector": "Run: pmos login slack.",
    "dropbox-connector": "Run: pmos login dropbox --app-key <KEY>. The app key is on the app's page at "
                         "https://www.dropbox.com/developers/apps.",
    "secret-file": "Copy it from its source or issue a new one. It holds a credential, so it never goes to Dropbox.",
    "secret-content": "The file matched a token pattern (listed), so it was left out whole. Copy it by hand after "
                      "removing the token, or recreate it.",
    "env-secret": "The line was removed from the backed-up env file. Add KEY=VALUE back to ~/.pm-os/env by hand; "
                  "issue a new value from the service if the old one is gone.",
    "shell-snapshot": "Nothing to do. Claude Code writes new shell snapshots. These can hold exported secrets.",
    "plugin-cache": "Run: bash system/scripts/install-claude-code.sh. Plugins and marketplaces download again.",
    "transient": "Nothing to do. Claude Code or PM OS recreates this as it runs.",
    "undo-history": "Not recoverable. It is Claude Code's per-session edit undo history.",
    "secure-store": "Nothing to do. Files in ~/.pm-os/secure stay on this machine and never go to Dropbox, and they are never "
                    "opened by a backup.",
    "transcripts-off": "PM_OS_BACKUP_TRANSCRIPTS=off is set. Remove that line from ~/.pm-os/env to back them up again.",
    "history-secret": "Commit history holds a token shape, so repo.bundle was left out of this backup. Remove it from "
                      "history, or set PM_OS_BACKUP_ALLOW_HISTORY=on in ~/.pm-os/env if it is a false alarm.",
    "too-large": "Copy it by hand if you need it. It is over the 64 MB limit for a non-transcript file.",
    "unreadable": "The file was locked or vanished during the run. The next night's run picks it up.",
    "claude-json": "Run: bash system/scripts/install-claude-code.sh for MCP servers, then sign in to Claude Code. "
                   "Project trust prompts come back once each.",
    "figma-token": "Issue a new personal access token in Figma (Settings, Security), then save it to ~/.figma-token.",
    "amp-secrets": "Recreate ~/.config/amp/secrets.env from each service's settings page.",
    "gcal-credentials": "Run the gcal skill's sign-in again, or use: pmos login google.",
    "gdrive-credentials": "Run the gdrive skill's sign-in again, or use: pmos login google.",
}

# First path segment under ~/.claude (or a longer prefix) -> recovery key.
CLAUDE_SKIP_PREFIXES = {
    ".credentials.json": "claude-login",
    "shell-snapshots": "shell-snapshot",
    "plugins/marketplaces": "plugin-cache",
    "plugins/cache": "plugin-cache",
    "plugins/plugin-directory-cache-v2.json": "plugin-cache",
    "security": "transient",  # plugin virtualenv, pip scratch, per-session warning state (over 400 MB)
    "cache": "transient",
    "downloads": "transient",
    "backups": "transient",
    "bridge-spawn": "transient",
    "session-env": "transient",
    "sessions": "transient",
    "state": "transient",
    "telemetry": "transient",
    "debug": "transient",
    "statsig": "transient",
    "paste-cache": "transient",
    "image-cache": "transient",
    "ide": "transient",
    "file-history": "undo-history",
}
PMOS_SKIP_PREFIXES = {
    "connectors/google-token.json": "google-connector",
    "connectors/google-oauth-client.json": "google-client",
    "connectors/slack-token.json": "slack-connector",
    "connectors/dropbox-token.json": "dropbox-connector",
    "connectors": "secret-file",
    "secure": "secure-store",
    "logs": "transient",
    "verify-logs": "transient",
    "wmux-session-store.json": "transient",
    "wmux-sidecar.json": "transient",
}
SKIP_PREFIXES = {"claude": CLAUDE_SKIP_PREFIXES, "pm-os": PMOS_SKIP_PREFIXES, OWNER_ROOT: {}}

TRANSIENT_NAME_RE = re.compile(r"^(\.last-.*|\.pre-restore-.*|\.session-.*\.tmp|\.sidecar-.*\.tmp|.*\.lock|\.DS_Store|Thumbs\.db)$")
# File names that hold a credential by their nature. skills/ is exempt from the
# token-name rule (design-token files), and the content scan still reads it.
SECRET_NAME_RE = re.compile(
    r"^(\.?credentials(\..*)?|.*credentials.*\.json|\.netrc|\.env(\..*)?|secrets?\.env|auth\.json|oauth.*\.json"
    r"|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_(rsa|ed25519|ecdsa|dsa)|cookies(\.sqlite)?|\.figma-token)$", re.I)
# A copy of ~/.claude.json holds the account record and MCP environment values. The
# file itself is never read, and a saved copy elsewhere stays out with it.
CLAUDE_JSON_RE = re.compile(r"^(\.claude\.json.*|claude\.before-.*\.json)$", re.I)
TOKEN_NAME_RE = re.compile(r"(^|[-_.])(token|tokens|secret|secrets)([-_.].*)?\.json$", re.I)
ENV_NAME_RE = re.compile(r"^env(\.(bak|clobbered)[-.].*)?$")
ENV_SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL|AUTH)", re.I)

# (name, pattern). Group 1, when present, is the only part replaced, so JSON stays valid.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = tuple((n, re.compile(p)) for n, p in (
    ("anthropic-key", rb"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai-key", rb"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    ("slack-token", rb"(?<![A-Za-z0-9])xox[abeoprs]-[A-Za-z0-9-]{10,}"),
    ("github-token", rb"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ("google-token", rb"(?<![A-Za-z0-9])ya29\.[A-Za-z0-9_-]{20,}"),
    ("google-refresh-token", rb"(?<![A-Za-z0-9/])1//[0-9A-Za-z_-]{30,}"),
    ("google-api-key", rb"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"),
    ("google-client-secret", rb"(?<![A-Za-z0-9])GOCSPX-[A-Za-z0-9_-]{20,}"),
    ("aws-key", rb"(?<![A-Z0-9])A(?:KIA|SIA)[0-9A-Z]{16}"),
    ("aws-secret", rb"(?i:aws_?secret(?:_?access)?_?key)[\"'\\]*\s*[=:]\s*[\"'\\]*([A-Za-z0-9/+=]{40})"),
    ("figma-token", rb"(?<![A-Za-z0-9])figd_[A-Za-z0-9_-]{20,}"),
    ("jwt", rb"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    # Bounded to key characters, so an unterminated key cannot swallow the rest of a JSONL line.
    # A backslash only as part of an escaped newline: stopping on half of \" would break the JSON.
    ("private-key", rb"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[A-Za-z0-9+/=\s]|\\{1,4}[nr]){0,8192}(?:-----END [A-Z ]*PRIVATE KEY-----)?"),
    ("json-credential", rb"\\?[\"'](?:refresh_token|access_token|id_token|client_secret|api_key|apiKey|password)\\?[\"']"
                        rb"\s*:\s*\\?[\"']([^\"'\s\\]{16,})"),
    ("bearer-token", rb"(?i:bearer)\s+([A-Za-z0-9._~+/-]{24,})"),
    ("basic-auth", rb"(?i:authorization)[\"'\\]*\s*:\s*[\"'\\]*(?i:basic)\s+([A-Za-z0-9+/=]{12,})"),
    ("password-field", rb"(?<![A-Za-z0-9_])(?i:password|passwd|pwd)[\"'\\]*\s*[=:]\s*[\"'\\]*([^\s\"'\\,;{}\[\]]{6,})"),
    # The value must hold a digit, so TOKEN = some_function_name is not a hit. The
    # lookbehind stops a base64 blob in a transcript from costing a rescan per byte.
    ("assigned-secret", rb"(?<![A-Za-z0-9_])(?i:[A-Z0-9_]{0,40}(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]{0,40})[\"'\\]*\s*[=:]\s*[\"'\\]*"
                        rb"((?=[A-Za-z_+=-]{0,256}[0-9])[A-Za-z0-9_+=-]{20,})"),
    # A folder listing can print one from a filename.
    ("ssn-shape", rb"(?<![\d-])(?:\d{3}-\d{2}-\d{4}|\d{3} \d{2} \d{4})(?![\d-])"),
))
# Shape-only patterns: code and docs in a skill trip them, and skills are copies from the repo.
GENERIC_KINDS = {"assigned-secret", "json-credential", "bearer-token", "basic-auth", "password-field", "ssn-shape"}
# Too loose to leave a whole file out over ("password: required"), and harmless to redact in a chat log.
TRANSCRIPT_ONLY_KINDS = {"password-field", "ssn-shape"}
# A literal each pattern needs, so most lines skip the regex: a substring test is
# far cheaper than a lookbehind regex on a megabyte transcript line. Lowercase needles
# are tested against the lowercased line.
NEEDLES: dict[str, tuple[bytes, ...]] = {
    "anthropic-key": (b"sk-ant-",), "openai-key": (b"sk-",), "slack-token": (b"xox",),
    "github-token": (b"gho_", b"ghp_", b"ghu_", b"ghs_", b"ghr_", b"github_pat_"), "google-token": (b"ya29.",),
    "google-refresh-token": (b"1//",), "google-api-key": (b"AIza",), "google-client-secret": (b"GOCSPX-",),
    "aws-key": (b"AKIA", b"ASIA"), "figma-token": (b"figd_",), "jwt": (b"eyJ",), "private-key": (b"PRIVATE KEY",),
    "ssn-shape": (b"-", b" "),
}
LOWER_NEEDLES: dict[str, tuple[bytes, ...]] = {
    "json-credential": (b"refresh_token", b"access_token", b"id_token", b"client_secret", b"api_key", b"apikey", b"password"),
    "bearer-token": (b"bearer",),
    "basic-auth": (b"authorization",),
    "password-field": (b"password", b"passwd", b"pwd"),
    "aws-secret": (b"aws_secret", b"awssecret", b"aws_access", b"secret_access"),
    "assigned-secret": (b"apikey", b"api_key", b"secret", b"token", b"password", b"passwd"),
}


# A recovery phrase has no token shape: it is 12 to 24 plain lowercase words. Prose is too, so a run
# only counts if a stretch of WORD_RUN_MIN words has almost no function words, barely repeats, and
# is not an alphabetical list. No wordlist ships here, so the check covers any phrase scheme.
# A separator is whitespace, an escaped newline, or a list number ("1. word 2. word"). A comma isn't:
# on real transcripts nearly every hit was a comma-separated keyword list in a skill description.
# A word may follow an escaped newline, which is why that case comes before the lookbehind on a backslash.
WORD_RUN_KIND = "word-run"
WORD_RUN_MIN = 12
WORD_RUN_RE = re.compile(
    rb"(?:(?<=\\[nrt])|(?<![A-Za-z0-9_'\\/.:@#$%&=+-]))"
    rb"[a-z]{3,10}(?:(?:\s|\\[nrt]|(?<!\d)\d{1,2}(?!\d)[.):]?)+[a-z]{3,10}){" + str(WORD_RUN_MIN - 1).encode() + rb",}"
    rb"(?![A-Za-z0-9_'\\/-])")
WORD_RUN_TOKEN_RE = re.compile(rb"\\[nrt]|[a-z]{3,10}")
WORD_RUN_COMMON = frozenset(
    b"the and for that this with you was but not have from they will would there their what which when were been "
    b"has had can should could does did just your our its how why she her him his them then than these those "
    b"because while are any all".split())


def word_run_spans(data: bytes) -> list[tuple[int, int]]:
    """Byte ranges that read like a recovery phrase, merged, in order."""
    spans: list[tuple[int, int]] = []
    if len(data) < WORD_RUN_MIN * 4 - 1:
        return spans
    for run in WORD_RUN_RE.finditer(data):
        words = [m for m in WORD_RUN_TOKEN_RE.finditer(data, run.start(), run.end()) if m.group(0)[:1] != b"\\"]
        for i in range(len(words) - WORD_RUN_MIN + 1):
            window = [m.group(0) for m in words[i:i + WORD_RUN_MIN]]
            if (sum(w in WORD_RUN_COMMON for w in window) > 1 or len(set(window)) < WORD_RUN_MIN - 2
                    or window == sorted(window)):
                continue
            start, end = words[i].start(), words[i + WORD_RUN_MIN - 1].end()
            if spans and start <= spans[-1][1]:
                spans[-1] = (spans[-1][0], end)
            else:
                spans.append((start, end))
    return spans


def candidates(data: bytes) -> list[tuple[str, re.Pattern[bytes]]]:
    """The patterns whose needed literal is in this text."""
    lowered = data.lower()
    out = []
    for name, pat in SECRET_PATTERNS:
        needles = NEEDLES.get(name)
        if needles is not None:
            if any(n in data for n in needles):
                out.append((name, pat))
        elif any(n in lowered for n in LOWER_NEEDLES[name]):
            out.append((name, pat))
    return out

CANNOT_RESTORE = (
    "Sign-ins and tokens: Claude Code login, Google, Slack and Dropbox connector tokens, Figma token, anything in secrets.env. "
    "Each one is issued again by its service.",
    "~/.claude.json: MCP server list, account record and project trust prompts live there and it is not backed up. "
    "install-claude-code.sh re-registers the MCP servers.",
    "Claude Code undo history (file-history), shell snapshots, session state, and plugin caches (they download again).",
    "Text a transcript's token pattern replaced stays [REDACTED:<kind>] in the restored copy.",
    "Anything changed after the backup ran, up to about a day of work.",
    "Project memory is keyed by folder path. If PM OS moves to a different folder, rename the matching folder under "
    "~/.claude/projects or Claude Code starts that project's memory empty.",
    "Scheduled tasks, launchd agents, PATH edits and installed programs live outside these folders: install-all.sh "
    "rebuilds them.",
    "people/, notes/, workstreams/ and the rest of the repo, apart from the four owner paths in local/, are not in "
    "this backup: Dropbox carries those files, and the bundle carries committed history only.",
)


class BackupError(RuntimeError):
    pass


# ---------------------------------------------------------------- paths

def main_checkout(start: Path) -> Path:
    """The main checkout, read from the .git file with no git process.

    In a linked worktree .git is a file naming <main>/.git/worktrees/<id>."""
    git_path = start / ".git"
    if git_path.is_file():
        try:
            text = git_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return start
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (start / gitdir).resolve()
            if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                return gitdir.parent.parent.parent
    return start


def default_repo() -> Path:
    return main_checkout(Path(__file__).resolve().parents[2])


def backups_root(repo: Path) -> Path:
    return repo / "local" / "backups"


def host_name() -> str:
    raw = (socket.gethostname() or platform.node() or "unknown-host").strip().lower()
    raw = raw.removesuffix(".local")
    return re.sub(r"[^a-z0-9._-]+", "-", raw) or "unknown-host"


def os_name() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def home_roots(home: Path | None = None, env: dict[str, str] | None = None, repo: Path | None = None) -> dict[str, Path]:
    """Where each root lives. An explicit home ignores every override and the repo, so a
    scratch run or restore can never reach the real folders. The owner files live in the
    PM OS folder, so with no home and no repo that root is absent."""
    if home is not None:
        return {"claude": home / ".claude", "pm-os": home / ".pm-os", OWNER_ROOT: home / OWNER_SCRATCH_DIR}
    env = os.environ if env is None else env
    base = Path.home()
    roots = {"claude": Path(env.get("CLAUDE_CONFIG_DIR") or base / ".claude"),
             "pm-os": Path(env.get("PM_OS_STATE_HOME") or base / ".pm-os")}
    if repo is not None:
        roots[OWNER_ROOT] = repo / "local"
    return roots


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def rename_retry(src: Path, dst: Path, attempts: int = 6) -> None:
    """Rename, retrying while a sync client holds the folder on Windows."""
    for i in range(attempts):
        try:
            os.rename(src, dst)
            return
        except OSError:
            if os.name != "nt" or i == attempts - 1:
                raise
            time.sleep(0.5 * 2 ** i)


def remove_tree(path: Path, attempts: int = 5) -> None:
    """Delete a folder, retrying while a sync client holds a file on Windows."""
    def clear_readonly(func: Callable, target: str, _exc: Any) -> None:
        os.chmod(target, 0o700)
        func(target)
    for i in range(attempts):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=clear_readonly)
            else:
                shutil.rmtree(path, onerror=clear_readonly)
            return
        except FileNotFoundError:
            return
        except OSError:
            if os.name != "nt" or i == attempts - 1:
                raise
            time.sleep(0.5 * 2 ** i)


# ---------------------------------------------------------------- scanning

def decode_wide(data: bytes) -> bytes | None:
    """UTF-8 for a UTF-16 file, else None. PowerShell 5.1's > writes UTF-16, and every
    pattern here is bytes, so a wide file would otherwise pass the scan clean."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace").encode("utf-8")
    head = data[:4096]
    if len(head) >= 8:
        if head[1::2].count(0) >= len(head) // 2 * 0.8:
            return data.decode("utf-16-le", "replace").encode("utf-8")
        if head[0::2].count(0) >= len(head) // 2 * 0.8:
            return data.decode("utf-16-be", "replace").encode("utf-8")
    return None


def secret_kinds(data: bytes, transcript: bool = False) -> list[str]:
    view = decode_wide(data) or data
    kinds = [name for name, pat in candidates(view)
             if (transcript or name not in TRANSCRIPT_ONLY_KINDS) and pat.search(view)]
    if transcript and word_run_spans(view):
        kinds.append(WORD_RUN_KIND)
    return kinds


def redact_line(line: bytes) -> tuple[bytes, list[str]]:
    """Replace every token on one line. Returns the new line and the kinds found."""
    found: list[str] = []
    for name, pat in candidates(line):
        if not pat.search(line):
            continue
        marker = b"[REDACTED:" + name.encode() + b"]"

        def swap(m: re.Match[bytes], marker: bytes = marker) -> bytes:
            if m.re.groups:
                start, end = m.span(1)
                base = m.start()
                whole = m.group(0)
                return whole[:start - base] + marker + whole[end - base:]
            return marker
        line = pat.sub(swap, line)
        found.append(name)
    spans = word_run_spans(line)
    if spans:
        marker = b"[REDACTED:" + WORD_RUN_KIND.encode() + b"]"
        for start, end in reversed(spans):
            line = line[:start] + marker + line[end:]
        found.append(WORD_RUN_KIND)
    return line, found


def redact_transcript_line(line: bytes) -> tuple[bytes, list[str]]:
    """redact_line, with a guarantee: a line that was valid JSON stays valid JSON.

    The patterns work on raw bytes and aim to keep JSON intact. If one ever does not, the
    line is parsed, each string is redacted decoded, and the line is written again."""
    clean, kinds = redact_line(line)
    if not kinds:
        return line, kinds
    try:
        json.loads(clean)
        return clean, kinds
    except ValueError:
        pass
    try:
        parsed = json.loads(line)
    except ValueError:
        return clean, kinds  # never was JSON, so there is nothing to keep valid

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return redact_line(node.encode("utf-8", "surrogatepass"))[0].decode("utf-8", "surrogatepass")
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node
    ending = line[len(line.rstrip(b"\r\n")):]
    return json.dumps(walk(parsed), ensure_ascii=False).encode("utf-8", "surrogatepass") + ending, kinds


def scrub_env(data: bytes) -> tuple[bytes, list[str]]:
    """Keep env settings, drop any line whose key or value looks like a secret."""
    kept: list[bytes] = []
    dropped: list[str] = []
    for number, raw in enumerate((decode_wide(data) or data).splitlines(keepends=True), 1):
        stripped = raw.strip()
        if not stripped or stripped == b"#":
            kept.append(raw)
            continue
        # Every line is scanned: a commented-out KEY=token is still a token.
        hit = bool(secret_kinds(raw, transcript=True))
        if stripped.startswith(b"#") or b"=" not in stripped:
            if hit:
                dropped.append(f"(comment or bare text on line {number})")
            else:
                kept.append(raw)
            continue
        key, _, value = stripped.partition(b"=")
        key_text = key.decode("utf-8", "replace").strip().removeprefix("export ").strip()
        if hit or (ENV_SECRET_KEY_RE.search(key_text) and value.strip()):
            # The key is recorded by name, so a token standing in its place stays out.
            dropped.append(f"(key on line {number})" if secret_kinds(key, transcript=True) else key_text)
            continue
        kept.append(raw)
    return b"".join(kept), dropped


def is_transcript(root: str, rel: str) -> bool:
    if root != "claude" or not rel.endswith(".jsonl"):
        return False
    return rel == "history.jsonl" or rel.startswith("projects/")


def skip_reason(root: str, rel: str) -> str | None:
    """Recovery key when a path is left out by rule, else None. rel uses forward slashes."""
    # Longest prefix wins, so connectors/google-token.json beats connectors.
    for prefix in sorted(SKIP_PREFIXES[root], key=len, reverse=True):
        if rel == prefix or rel.startswith(prefix + "/"):
            return SKIP_PREFIXES[root][prefix]
    name = PurePosixPath(rel).name
    if rel.split("/", 1)[0].startswith(".pre-restore-") or TRANSIENT_NAME_RE.match(name):
        return "transient"
    if CLAUDE_JSON_RE.match(name):
        return "claude-json"
    if SECRET_NAME_RE.match(name):
        return "secret-file"
    if TOKEN_NAME_RE.search(name) and not rel.startswith("skills/"):
        return "secret-file"
    return None


def is_link(entry: os.DirEntry) -> bool:
    """A symlink or a Windows junction. Never followed, so a skill link cannot pull
    the repo into the backup and a loop cannot hang it."""
    if entry.is_symlink():
        return True
    try:
        return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def walk(root: Path, only: tuple[str, ...] | None = None) -> Iterable[tuple[str, os.DirEntry | None, str]]:
    """Yield (rel, entry, kind) for each file and link. kind is file, link or error.
    With only, top-level names outside it are not even opened."""
    stack = [(root, "")]
    while stack:
        folder, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            yield prefix.rstrip("/") or ".", None, "error"
            continue
        for entry in entries:
            if only is not None and not prefix and entry.name.lower() not in only:
                continue
            rel = prefix + entry.name
            if is_link(entry):
                yield rel, entry, "link"
            elif entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), rel + "/"))
            elif entry.is_file(follow_symlinks=False):
                yield rel, entry, "file"


def member_info(arc: str, mtime: float) -> zipfile.ZipInfo:
    stamp = time.localtime(mtime)[:6]
    if stamp < ZIP_TIME_FLOOR:
        stamp = ZIP_TIME_FLOOR
    info = zipfile.ZipInfo(arc, date_time=stamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def env_setting(roots: dict[str, Path], key: str) -> str | None:
    """A setting from the environment, else from ~/.pm-os/env."""
    value = os.environ.get(key)
    if value is None:
        try:
            for line in (roots["pm-os"] / "env").read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith(f"{key}="):
                    value = line.split("=", 1)[1].strip()
        except OSError:
            pass
    return value


def transcripts_enabled(roots: dict[str, Path]) -> bool:
    """PM_OS_BACKUP_TRANSCRIPTS=off leaves transcripts out."""
    return (env_setting(roots, "PM_OS_BACKUP_TRANSCRIPTS") or "on").lower() not in ("off", "0", "no", "false")


# ---------------------------------------------------------------- archive

class LeftOut:
    """Collects left-out paths: credentials one by one, plain folders as one row."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._folders: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(self, root: str, rel: str, recovery: str, size: int = 0, kinds: list[str] | None = None) -> None:
        if recovery in ("transient", "plugin-cache", "undo-history", "shell-snapshot", "transcripts-off"):
            top = "/".join(rel.split("/")[:2]) if rel.startswith("plugins/") else rel.split("/")[0]
            row = self._folders.setdefault((root, top, recovery), {
                "root": root, "path": top, "recovery": recovery, "files": 0, "bytes": 0})
            row["files"] += 1
            row["bytes"] += size
            return
        row = {"root": root, "path": rel, "recovery": recovery, "files": 1, "bytes": size}
        if kinds:
            row["kinds"] = sorted(set(kinds))
        self.rows.append(row)

    def all(self) -> list[dict[str, Any]]:
        return self.rows + list(self._folders.values())


def write_archive(root_name: str, source: Path, target: Path, left: LeftOut, include_transcripts: bool) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    links: list[dict[str, str]] = []
    redacted_files = redactions = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        if not source.is_dir():
            return {"source": str(source), "missing": True, "files": [], "links": [], "redacted_files": 0, "redactions": 0}
        for rel, entry, kind in walk(source, OWNER_PATHS if root_name == OWNER_ROOT else None):
            if kind == "error":
                if rel == ".":
                    raise BackupError(f"{source} cannot be read")
                left.add(root_name, rel, "unreadable")
                continue
            reason = skip_reason(root_name, rel)
            if kind == "link":
                if reason is None:
                    try:
                        links.append({"path": rel, "target": os.readlink(entry.path)})
                    except OSError:
                        left.add(root_name, rel, "unreadable")
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                left.add(root_name, rel, "unreadable")
                continue
            if reason:
                left.add(root_name, rel, reason, st.st_size)
                continue
            transcript = is_transcript(root_name, rel)
            if transcript and not include_transcripts:
                left.add(root_name, "transcripts", "transcripts-off", st.st_size)
                continue
            if not transcript and st.st_size > MAX_PLAIN_BYTES:
                left.add(root_name, rel, "too-large", st.st_size)
                continue
            try:
                row = copy_member(archive, root_name, rel, Path(entry.path), st, transcript, left)
            except OSError:
                left.add(root_name, rel, "unreadable", st.st_size)
                continue
            if row:
                files.append(row)
                if row.get("redactions"):
                    redacted_files += 1
                    redactions += row["redactions"]
    return {"source": str(source), "files": files, "links": links,
            "redacted_files": redacted_files, "redactions": redactions}


def copy_member(archive: zipfile.ZipFile, root_name: str, rel: str, path: Path, st: os.stat_result,
                transcript: bool, left: LeftOut) -> dict[str, Any] | None:
    digest = hashlib.sha256()
    size = 0
    count = 0
    if transcript:
        # Line by line: a transcript can run to tens of MB, and memory can be short.
        with path.open("rb") as src, archive.open(member_info(rel, st.st_mtime), "w", force_zip64=True) as out:
            for line in src:
                clean, kinds = redact_transcript_line(line)
                count += len(kinds)
                digest.update(clean)
                size += len(clean)
                out.write(clean)
    else:
        data = path.read_bytes()
        if root_name == "pm-os" and ENV_NAME_RE.match(PurePosixPath(rel).name):
            data, dropped = scrub_env(data)
            for key in dropped:
                left.rows.append({"root": root_name, "path": f"{rel} line {key}", "recovery": "env-secret",
                                  "files": 0, "bytes": 0})
        else:
            kinds = secret_kinds(data)
            # A skill's code and docs trip the shape-only patterns. Its JSON files do not get
            # that pass, so a token.json with a real refresh_token stays out.
            if rel.startswith(("skills/", "plugins/")) and not rel.lower().endswith(".json"):
                kinds = [k for k in kinds if k not in GENERIC_KINDS]
            # Everything under machines/ is an env file, whatever its name: a secret-looking
            # key leaves it out whole. A restore replaces the file, so a scrubbed copy
            # would lose the line.
            if root_name == OWNER_ROOT and rel.lower().startswith("machines/"):
                kinds += [f"env key {key}" for key in scrub_env(data)[1]]
            # A session's saved tool output is chat text too, and a text file can't be redacted by line.
            if root_name == "claude" and rel.startswith("projects/") and word_run_spans(decode_wide(data) or data):
                kinds.append(WORD_RUN_KIND)
            if kinds:
                left.add(root_name, rel, "secret-content", st.st_size, kinds)
                return None
        digest.update(data)
        size = len(data)
        archive.writestr(member_info(rel, st.st_mtime), data)
    row: dict[str, Any] = {"p": rel, "s": size, "h": digest.hexdigest(), "m": st.st_mtime}
    if count:
        row["redactions"] = count
    return row


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- git bundle

def git_exe() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if base and (Path(base) / "Git/cmd/git.exe").is_file():
            return str(Path(base) / "Git/cmd/git.exe")
    return None


# Children get their own hidden console on Windows. Task Scheduler stops a task by killing
# its root (conhost --headless) once the wall-clock limit passes, which counts time asleep.
# A run that starts in a brief Modern Standby wake can freeze for hours; at wake the limit
# has passed, conhost dies, Python lives on, and every git it starts inherits the dead
# console and fails with 0xC0000142 (STATUS_DLL_INIT_FAILED).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SCAN_RETRY_WAIT = 20  # seconds before the one retry of the history scan
SLEPT_WARN_MINUTES = 60  # a run this long was probably frozen by sleep
SCAN_TIMEOUT_SECONDS = 30 * 60  # a hung git log is killed, so the retry runs and the lock is freed


def run_git(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    exe = git_exe()
    if not exe:
        raise BackupError("git not found")
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          encoding="utf-8", errors="replace", creationflags=NO_WINDOW)


# Reviewed false alarms in history, as (commit, path, kind). A matching shape anywhere else still
# withholds the bundle. Add an entry only after reviewing the commit.
HISTORY_FALSE_ALARMS: set[tuple[str, str, str]] = set()


def scan_history(repo: Path, attempts: int = 2, wait: float | None = None) -> list[dict[str, str]]:
    """Token shapes added anywhere in history. A git log that fails to run is tried once
    more after a short wait: a process that cannot start right after wake, or under memory
    pressure, often starts a moment later."""
    wait = SCAN_RETRY_WAIT if wait is None else wait
    for attempt in range(1, attempts + 1):
        try:
            return _scan_history_once(repo)
        except (BackupError, OSError) as exc:
            if attempt == attempts or str(exc) == "git not found":
                if attempt > 1:
                    raise BackupError(f"{exc} (after {attempt} tries)") from exc
                raise
            time.sleep(wait)
    raise BackupError("history scan did not run")  # unreachable: attempts >= 1


def _scan_history_once(repo: Path) -> list[dict[str, str]]:
    """Token shapes added anywhere in history, stashes included. One streaming git log.

    Only the high-confidence shapes: a bundle cannot be redacted, so a hit withholds
    it, and a fixture that merely looks like an assignment must not do that."""
    exe = git_exe()
    if not exe:
        raise BackupError("git not found")
    strong = [(n, p) for n, p in SECRET_PATTERNS if n not in GENERIC_KINDS]
    with subprocess.Popen([exe, "-C", str(repo), "log", "--all", "-p", "--diff-merges=first-parent", "--no-color",
                           "--no-ext-diff", "--format=commit %H"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                          creationflags=NO_WINDOW) as proc:
        fired = threading.Event()

        def stop() -> None:
            fired.set()
            proc.kill()

        watchdog = threading.Timer(SCAN_TIMEOUT_SECONDS, stop)
        watchdog.daemon = True
        watchdog.start()
        try:
            hits = list(_scan_lines(proc.stdout, strong))
        finally:
            watchdog.cancel()
    if fired.is_set():
        raise BackupError(f"git log was stopped after {SCAN_TIMEOUT_SECONDS // 60} minutes")
    if proc.returncode:
        raise BackupError(f"git log failed with exit code {proc.returncode}")
    return hits


def _scan_lines(stream: Iterable[bytes], strong: list[tuple[str, re.Pattern[bytes]]]) -> Iterable[dict[str, str]]:
    commit = path = ""
    found = 0
    for line in stream:
        if line.startswith(b"commit "):
            commit = line[7:47].decode("ascii", "replace")
        elif line.startswith(b"+++ b/"):
            path = line[6:].strip().decode("utf-8", "replace")
        elif line.startswith(b"+"):
            for name, pat in strong:
                needles = NEEDLES.get(name)
                if needles and not any(n in line for n in needles):
                    continue
                if pat.search(line) and found < 50 and (commit, path, name) not in HISTORY_FALSE_ALARMS:
                    found += 1
                    yield {"kind": name, "commit": commit, "path": path}


def write_bundle(repo: Path, target: Path, allow_history: bool = False) -> dict[str, Any]:
    """git bundle of all refs, verified. .git is not synced, so this is the only offsite history."""
    if not (repo / ".git").exists():
        return {"skipped": f"{repo} has no .git"}
    if not git_exe():
        return {"error": "git not found"}
    try:
        hits = scan_history(repo)
    except (BackupError, OSError) as exc:
        return {"error": f"history scan failed: {exc}"}
    if hits and not allow_history:
        first = hits[0]
        return {"error": f"git history holds a token shape, so the bundle was withheld: {first['kind']} in {first['path']} "
                         f"at commit {first['commit'][:10]} ({len(hits)} hit(s)). Remove it from history, or set "
                         "PM_OS_BACKUP_ALLOW_HISTORY=on if it is a false alarm", "history_hits": hits}
    made = run_git(["-C", str(repo), "bundle", "create", "--quiet", str(target), "--all"])
    if made.returncode or not target.exists():
        return {"error": f"git bundle create failed: {(made.stderr or made.stdout).strip()[:300]}"}
    checked = run_git(["-C", str(repo), "bundle", "verify", "--quiet", str(target)])
    if checked.returncode:
        return {"error": f"git bundle verify failed: {(checked.stderr or checked.stdout).strip()[:300]}"}
    heads = run_git(["bundle", "list-heads", str(target)]).stdout.splitlines()
    head = next((line.split()[0] for line in heads if line.endswith(" HEAD")), "")
    return {"file": BUNDLE_NAME, "bytes": target.stat().st_size, "sha256": sha256_file(target),
            "refs": len(heads), "head": head, "verified": True, "history_hits": hits}


# ---------------------------------------------------------------- run

def outside_rows(home: Path | None) -> list[dict[str, Any]]:
    """Credentials outside the two folders. Named here so the list is complete; never read."""
    base = home if home is not None else Path.home()
    rows = [(".claude.json", "claude-json"), (".figma-token", "figma-token"),
            (".config/amp/secrets.env", "amp-secrets"), (".config/gcal-skill/credentials.json", "gcal-credentials"),
            (".config/gdrive-skill/credentials.json", "gdrive-credentials")]
    return [{"root": "home", "path": rel, "recovery": key, "files": 1, "bytes": 0}
            for rel, key in rows if (base / rel).exists()]


def left_out_text(manifest: dict[str, Any]) -> str:
    lines = [f"Left out of the backup taken {manifest['finished_at']} on {manifest['host']}.",
             "No secret value appears in this file or anywhere in this backup folder, except that the token scan",
             "is pattern-based: a password typed in plain words in a transcript would not be caught.", ""]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in manifest["left_out"]:
        groups.setdefault(row["recovery"], []).append(row)
    order = [k for k in RECOVERY if k in groups]
    for key in order:
        lines.append(f"How to get it back: {RECOVERY[key]}")
        for row in groups[key]:
            note = f"{row['files']} files, {fmt_bytes(row['bytes'])}" if row.get("files", 0) > 1 else fmt_bytes(row.get("bytes", 0))
            kinds = f" (matched {', '.join(row['kinds'])})" if row.get("kinds") else ""
            lines.append(f"  - {row['root']}: {row['path']}  [{note}]{kinds}")
        lines.append("")
    redacted = [f"{name}: {info['redacted_files']} files, {info['redactions']} tokens"
                for name, info in manifest["roots"].items() if info.get("redactions")]
    if redacted:
        lines += ["Transcripts kept with tokens replaced by [REDACTED:<kind>]: " + "; ".join(redacted), ""]
    lines.append("What a restore cannot bring back:")
    lines += [f"  - {item}" for item in CANNOT_RESTORE]
    return "\n".join(lines) + "\n"


def select_keep(names: Iterable[str], daily: int = DAILY_KEEP, weekly: int = WEEKLY_KEEP) -> set[str]:
    """Newest `daily` dates, then the newest date of each of the next `weekly` ISO weeks."""
    dates = sorted({n for n in names if DATE_RE.match(n)}, reverse=True)
    keep = set(dates[:daily])
    weeks: dict[tuple[int, int], str] = {}
    for name in dates[daily:]:
        try:
            year, week, _ = date_cls.fromisoformat(name).isocalendar()
        except ValueError:
            keep.add(name)  # odd name: leave it alone
            continue
        if (year, week) not in weeks and len(weeks) < weekly:
            weeks[(year, week)] = name
    return keep | set(weeks.values())


def prune(host_dir: Path, daily: int = DAILY_KEEP, weekly: int = WEEKLY_KEEP) -> list[str]:
    """Delete dated backups outside the keep set. Only dated folders in this one host folder."""
    names = [p.name for p in host_dir.iterdir() if p.is_dir() and DATE_RE.match(p.name)]
    keep = select_keep(names, daily, weekly)
    removed = []
    for name in sorted(set(names) - keep):
        # Renamed first, so a delete that a sync client interrupts cannot leave a dated
        # folder that reads as a backup. clear_stale_staging finishes it on a later run.
        doomed = host_dir / f".replaced-{name}-{os.getpid()}"
        rename_retry(host_dir / name, doomed)
        removed.append(name)
        remove_tree(doomed)
    return removed


def clear_stale_staging(host_dir: Path, older_than_hours: float = 24) -> None:
    cutoff = time.time() - older_than_hours * 3600
    for path in host_dir.glob(".*-*"):
        if path.is_dir() and path.name.startswith((".staging-", ".replaced-")) and path.stat().st_mtime < cutoff:
            remove_tree(path)


def take_lock(host_dir: Path) -> Path:
    lock = host_dir / ".run.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime > 6 * 3600:
        lock.unlink(missing_ok=True)
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError as exc:
        raise BackupError(f"another backup is running ({lock})") from exc
    return lock


def newest_manifest(host_dir: Path) -> dict[str, Any] | None:
    for name in sorted((p.name for p in host_dir.iterdir() if p.is_dir() and DATE_RE.match(p.name)), reverse=True):
        try:
            return read_manifest(host_dir / name)
        except (OSError, ValueError):
            continue
    return None


def check_not_collapsed(root_info: dict[str, Any], previous: dict[str, Any] | None) -> None:
    """Refuse a copy that looks like a wrong folder or a wiped home. The earlier copies
    are the only way back from either, and a published empty copy would count toward
    retention and pass every later check."""
    # The owner files live in the repo, so they say nothing about a wiped home.
    total = sum(info["files"] for name, info in root_info.items() if name != OWNER_ROOT)
    if total == 0:
        raise BackupError("no files found in ~/.claude or ~/.pm-os, so nothing was published (check CLAUDE_CONFIG_DIR "
                          "and PM_OS_STATE_HOME)")
    if not previous:
        return
    for name, info in root_info.items():
        before = previous.get("roots", {}).get(name, {}).get("files", 0)
        if before and info.get("missing"):
            raise BackupError(f"{info['source']} is missing, and the last copy had {before} files from it; nothing was "
                              "published. Use run --allow-shrink if this is intended")
        if before >= 20 and info["files"] < before * SHRINK_LIMIT:
            raise BackupError(f"{name} fell from {before} files to {info['files']}, so nothing was published. Use "
                              "run --allow-shrink if this is intended")


def slept_note(started: datetime, finished: datetime) -> str | None:
    """A run this long was probably frozen by sleep (Modern Standby), which explains its age and any odd failure."""
    minutes = (finished - started).total_seconds() / 60
    if minutes < SLEPT_WARN_MINUTES:
        return None
    return (f"the run took {int(minutes // 60)}h {int(minutes % 60)}m of wall time, so the machine probably slept during it; "
            "a normal run takes a few minutes")


def run_backup(repo: Path, home: Path | None = None, day: str | None = None, host: str | None = None,
               fail_after_archives: bool = False, allow_shrink: bool = False) -> dict[str, Any]:
    started = now_utc()
    host = host or host_name()
    day = day or datetime.now().strftime("%Y-%m-%d")
    roots = home_roots(home, repo=repo)
    host_dir = backups_root(repo) / host
    host_dir.mkdir(parents=True, exist_ok=True)
    lock = take_lock(host_dir)
    staging = host_dir / f".staging-{day}-{os.getpid()}"
    warnings: list[str] = []
    try:
        try:
            clear_stale_staging(host_dir)
        except OSError as exc:
            warnings.append(f"stale staging not cleared: {exc}")
        previous = newest_manifest(host_dir)
        staging.mkdir()
        left = LeftOut()
        include_transcripts = transcripts_enabled(roots)
        root_info: dict[str, Any] = {}
        all_files: dict[str, list[dict[str, Any]]] = {}
        for name in ROOT_NAMES:
            archive = staging / f"{name}.zip"
            info = write_archive(name, roots[name], archive, left, include_transcripts)
            all_files[name] = info.pop("files")
            root_info[name] = {**info, "archive": archive.name, "archive_bytes": archive.stat().st_size,
                               "archive_sha256": sha256_file(archive), "files": len(all_files[name]),
                               "bytes": sum(f["s"] for f in all_files[name])}
        warnings += [f"owner file left out: {row['path']} ({row['recovery']}, see LEFT-OUT.txt)"
                     for row in left.rows if row["root"] == OWNER_ROOT]
        current = {row["p"] for row in all_files[OWNER_ROOT]}
        left_paths = {row["path"] for row in left.rows if row["root"] == OWNER_ROOT}
        warnings += [f"owner file gone since the last backup: {row['p']}"
                     for row in ((previous or {}).get("files") or {}).get(OWNER_ROOT, [])
                     if row["p"] not in current and row["p"] not in left_paths]
        if not allow_shrink:
            check_not_collapsed(root_info, previous)
        if fail_after_archives:
            raise BackupError("test failure after archives")
        archives_done = now_utc()
        allow_history = (env_setting(roots, "PM_OS_BACKUP_ALLOW_HISTORY") or "off").lower() in ("on", "1", "yes", "true")
        bundle = write_bundle(repo, staging / BUNDLE_NAME, allow_history=allow_history)
        if bundle.get("skipped") and previous and (previous.get("git_bundle") or {}).get("file"):
            bundle = {"error": f"the last copy had a git bundle, and now {bundle['skipped']}"}
        if bundle.get("error") and (host_dir / day).exists():
            # A same-day rerun must not trade a good bundle for none.
            raise BackupError(f"{bundle['error']}; the earlier copy from {day} stays")
        manifest = {
            "schema": SCHEMA, "host": host, "platform": os_name(), "date": day, "started_at": iso(started),
            "archives_done_at": iso(archives_done), "finished_at": iso(now_utc()), "pm_os_root": str(repo),
            "transcripts": include_transcripts, "partial": bool(bundle.get("error")),
            "roots": root_info, "files": all_files, "git_bundle": bundle,
            "left_out": left.all() + outside_rows(home),
            "totals": {"files": sum(len(v) for v in all_files.values()),
                       "bytes": sum(i["bytes"] for i in root_info.values()),
                       "archive_bytes": sum(i["archive_bytes"] for i in root_info.values()) + int(bundle.get("bytes", 0))},
            "retention": {"daily": DAILY_KEEP, "weekly": WEEKLY_KEEP},
        }
        atomic_write(staging / "manifest.json", json.dumps(manifest, indent=1) + "\n")
        atomic_write(staging / "LEFT-OUT.txt", left_out_text(manifest))
        problems = verify_dir(staging)
        if problems:
            raise BackupError("verification failed: " + "; ".join(problems[:5]))
        final = host_dir / day
        replaced = None
        if final.exists():
            replaced = host_dir / f".replaced-{day}-{os.getpid()}"
            rename_retry(final, replaced)
        try:
            rename_retry(staging, final)
        except OSError:
            if replaced:
                rename_retry(replaced, final)  # the earlier backup goes back
            raise
        # The copy is published. Nothing below can undo that, so a sync client holding a
        # file is a warning, and the next run clears it. A failed bundle prunes nothing:
        # nights with no history must not push out copies that have it.
        removed: list[str] = []
        try:
            if replaced:
                remove_tree(replaced)
            if not bundle.get("error"):
                removed = prune(host_dir)
        except OSError as exc:
            warnings.append(f"cleanup incomplete: {exc}")
        note = slept_note(started, now_utc())
        if note:
            warnings.append(note)
        result = {"status": "failed" if bundle.get("error") else "ok", "path": str(final), "date": day, "host": host,
                  "totals": manifest["totals"], "pruned": removed, "git_bundle": bundle, "warnings": warnings,
                  "left_out_rows": len(manifest["left_out"]), "roots": {
                      n: {"files": i["files"], "bytes": i["bytes"], "archive_bytes": i["archive_bytes"],
                          "redacted_files": i["redacted_files"], "redactions": i["redactions"]}
                      for n, i in root_info.items()}}
        if bundle.get("error"):
            # Say what did go out, so a folder dated today is not read as a full copy.
            result["partial"] = True
            result["error"] = (f"{bundle['error']}; the home folders were still published to {day} as a partial copy "
                               "with no repo.bundle, and nothing was pruned")
        return result
    finally:
        try:
            if staging.exists():
                remove_tree(staging)
        except OSError:
            pass  # clear_stale_staging removes it on a later run
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass  # a lock over 6 hours old counts as stale


# ---------------------------------------------------------------- verify and find

def read_manifest(backup: Path) -> dict[str, Any]:
    return json.loads((backup / "manifest.json").read_text(encoding="utf-8"))


def verify_dir(backup: Path, quick: bool = False, git_check: bool = False) -> list[str]:
    """Problems with a backup folder; empty means good. quick checks shape and sizes only."""
    try:
        manifest = read_manifest(backup)
    except (OSError, ValueError) as exc:
        return [f"manifest unreadable: {exc}"]
    problems: list[str] = []
    for name, info in manifest.get("roots", {}).items():
        archive = backup / info.get("archive", f"{name}.zip")
        if not archive.is_file():
            problems.append(f"{archive.name} missing")
            continue
        if archive.stat().st_size != info.get("archive_bytes"):
            problems.append(f"{archive.name} size differs from the manifest")
            continue
        if quick:
            continue
        if sha256_file(archive) != info.get("archive_sha256"):
            problems.append(f"{archive.name} hash differs from the manifest")
            continue
        try:
            with zipfile.ZipFile(archive) as handle:
                names = set(handle.namelist())
                for row in manifest.get("files", {}).get(name, []):
                    if row["p"] not in names:
                        problems.append(f"{name}:{row['p']} missing from the archive")
                        continue
                    digest = hashlib.sha256()
                    with handle.open(row["p"]) as member:
                        for chunk in iter(lambda: member.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != row["h"]:
                        problems.append(f"{name}:{row['p']} hash differs from the manifest")
        except (OSError, zipfile.BadZipFile) as exc:
            problems.append(f"{archive.name} unreadable: {exc}")
    bundle = manifest.get("git_bundle") or {}
    if bundle.get("file"):
        path = backup / bundle["file"]
        if not path.is_file() or path.stat().st_size != bundle.get("bytes"):
            problems.append(f"{bundle['file']} missing or wrong size")
        elif not quick and sha256_file(path) != bundle.get("sha256"):
            problems.append(f"{bundle['file']} hash differs from the manifest")
        elif git_check and git_exe():
            heads = run_git(["bundle", "list-heads", str(path)])
            if heads.returncode:
                problems.append(f"{bundle['file']} does not read as a git bundle")
    return problems


def list_backups(root: Path) -> list[dict[str, Any]]:
    """Every published backup under one local/backups folder, newest first."""
    found: list[dict[str, Any]] = []
    if not root.is_dir():
        return found
    for host_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for folder in sorted(p for p in host_dir.iterdir() if p.is_dir() and DATE_RE.match(p.name)):
            try:
                manifest = read_manifest(folder)
            except (OSError, ValueError):
                found.append({"path": folder, "host": host_dir.name, "date": folder.name, "finished_at": None,
                              "platform": "", "files": 0, "bytes": 0, "archive_bytes": 0, "broken": True})
                continue
            totals = manifest.get("totals", {})
            found.append({"path": folder, "host": manifest.get("host", host_dir.name), "date": folder.name,
                          "finished_at": manifest.get("finished_at"), "platform": manifest.get("platform", ""),
                          "files": totals.get("files", 0), "bytes": totals.get("bytes", 0),
                          "archive_bytes": totals.get("archive_bytes", 0), "broken": False,
                          "partial": bool(manifest.get("partial") or (manifest.get("git_bundle") or {}).get("error"))})
    return sorted(found, key=lambda r: (r["finished_at"] or "", r["date"]), reverse=True)


def rank(rows: list[dict[str, Any]], host: str | None = None, os_label: str | None = None) -> list[dict[str, Any]]:
    """This host first, then this OS, then the rest; newest first inside each group."""
    host = host or host_name()
    os_label = os_label or os_name()
    usable = [r for r in rows if not r.get("broken")]
    return ([r for r in usable if r["host"] == host]
            + [r for r in usable if r["host"] != host and r["platform"] == os_label]
            + [r for r in usable if r["host"] != host and r["platform"] != os_label])


def resolve_backup(ref: str | None, repo: Path, host: str | None = None) -> Path:
    """latest (or None), a date, or a path. Raises BackupError with a plain message."""
    if ref and ref != "latest":
        as_path = Path(ref)
        if as_path.is_dir():
            return as_path
    rows = list_backups(backups_root(repo))
    if host:
        rows = [r for r in rows if r["host"] == host]
    if ref and ref != "latest":
        rows = [r for r in rows if r["date"] == ref]
    ranked = rank(rows)
    if not ranked:
        raise BackupError(f"no backup found under {backups_root(repo)}" + (f" for {ref}" if ref else ""))
    return Path(ranked[0]["path"])


# ---------------------------------------------------------------- restore

# Files full of this machine's paths: skipped when the backup came from another OS.
OTHER_OS_SKIP = {"claude": ("settings.json", "CLAUDE.md", "plugins/installed_plugins.json",
                            "plugins/known_marketplaces.json"),
                 "pm-os": ("env",)}


def safe_target(base: Path, rel: str) -> Path:
    parts = PurePosixPath(rel).parts
    # A colon is a normal filename character on a Mac and a drive or stream on Windows.
    bad_colon = os.name == "nt" and ":" in rel
    if (not parts or bad_colon or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel)
            or any(p in ("..", "") or "\\" in p for p in parts)):
        raise BackupError(f"unsafe path in manifest: {rel!r}")
    return base.joinpath(*parts)


def path_is_link(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return os.path.islink(path) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def link_in_the_way(base: Path, target: Path, cache: dict[Path, bool]) -> bool:
    """True when a folder between the root and the target is a link or junction. Writing
    through one would land outside the root, for example in the repo a skill links to."""
    for parent in target.relative_to(base).parents:
        folder = base / parent
        if folder == base:
            continue
        if folder not in cache:
            cache[folder] = path_is_link(folder)
        if cache[folder]:
            return True
    return path_is_link(target)


def merge_env(local: bytes, restored: bytes) -> bytes:
    """Restored values win, and every key only the local file has stays. The backup's env
    file had its secret lines removed, so replacing the file would lose them."""
    def key_of(line: bytes) -> bytes | None:
        text = line.strip()
        return None if not text or text.startswith(b"#") or b"=" not in text else text.split(b"=", 1)[0].strip()
    restored_keys = {key_of(line) for line in restored.splitlines()} - {None}
    extra = [line for line in local.splitlines() if key_of(line) and key_of(line) not in restored_keys]
    body = restored if restored.endswith(b"\n") or not restored else restored + b"\n"
    return body + b"".join(line + b"\n" for line in extra)


def restore_plan(backup: Path, roots: dict[str, Path], allow_other_os: bool = True) -> dict[str, Any]:
    """What a restore would do. Same file: nothing. Local newer or same age: kept."""
    manifest = read_manifest(backup)
    other_os = manifest.get("platform") != os_name()
    out: dict[str, list[dict[str, Any]]] = {"new": [], "replace_older": [], "keep_newer": [], "same": [], "skip_other_os": [],
                                            "skip_unsafe": [], "skip_link": []}
    cache: dict[Path, bool] = {}
    no_target: list[str] = []
    for name, rows in manifest.get("files", {}).items():
        if name not in roots:
            if rows:
                no_target.append(name)
            continue
        for row in rows:
            try:
                target = safe_target(roots[name], row["p"])
            except BackupError:
                out["skip_unsafe"].append({"root": name, "path": row["p"], "row": row})
                continue
            item = {"root": name, "path": row["p"], "target": target, "row": row}
            if other_os and row["p"] in OTHER_OS_SKIP.get(name, ()):
                out["skip_other_os"].append(item)
                continue
            if link_in_the_way(roots[name], target, cache):
                out["skip_link"].append(item)
                continue
            try:
                st = target.stat()
            except OSError:
                out["new"].append(item)
                continue
            if st.st_size == row["s"] and sha256_file(target) == row["h"]:
                out["same"].append(item)
            elif st.st_mtime >= row["m"] - MTIME_SLOP:
                out["keep_newer"].append(item)
            else:
                out["replace_older"].append(item)
    return {"backup": str(backup), "manifest": manifest, "other_os": other_os, "no_target": no_target, **out}


def plan_counts(plan: dict[str, Any]) -> dict[str, int]:
    return {k: len(plan[k]) for k in ("new", "replace_older", "keep_newer", "same", "skip_other_os", "skip_unsafe", "skip_link")}


def plan_skips(plan: dict[str, Any]) -> list[str]:
    """Plain lines for everything a restore will not write, beyond the newer-file rule."""
    lines = [f"{i['root']}:{i['path']} not restored: a link or junction is in the way" for i in plan["skip_link"]]
    lines += [f"{i['root']}:{i['path']} not restored: the name is not valid on this machine" for i in plan["skip_unsafe"]]
    lines += [f"{name} files not restored: no PM OS folder was named" for name in plan["no_target"]]
    for name, info in plan["manifest"].get("roots", {}).items():
        lines += [f"{name}:{l['path']} was a link to {l['target']} and is not recreated" for l in info.get("links", [])]
    return lines


def apply_restore(plan: dict[str, Any], roots: dict[str, Path]) -> dict[str, Any]:
    backup = Path(plan["backup"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    problems: list[str] = []
    restored = safety = 0
    archives: dict[str, zipfile.ZipFile] = {}
    try:
        for item in plan["new"] + plan["replace_older"]:
            name, row, target = item["root"], item["row"], item["target"]
            handle = archives.get(name)
            if handle is None:
                handle = archives[name] = zipfile.ZipFile(backup / plan["manifest"]["roots"][name]["archive"])
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.restore-{os.getpid()}")
            digest = hashlib.sha256()
            try:
                with handle.open(row["p"]) as src, tmp.open("wb") as dst:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        digest.update(chunk)
                        dst.write(chunk)
                if digest.hexdigest() != row["h"]:
                    problems.append(f"{name}:{row['p']} failed its hash check; left alone")
                    continue
                merged = False
                if target.exists():
                    # Checked again right before the write: a file edited since the plan stays.
                    if target.stat().st_mtime >= row["m"] - MTIME_SLOP:
                        continue
                    copy = roots[name] / f".pre-restore-{stamp}" / PurePosixPath(row["p"])
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, copy)
                    safety += 1
                    if name == "pm-os" and row["p"] == "env":
                        tmp.write_bytes(merge_env(target.read_bytes(), tmp.read_bytes()))
                        merged = True
                os.replace(tmp, target)
                if not merged:
                    os.utime(target, (row["m"], row["m"]))
                restored += 1
            except (OSError, KeyError, zipfile.BadZipFile) as exc:
                problems.append(f"{name}:{row['p']}: {exc}")
            finally:
                tmp.unlink(missing_ok=True)
    finally:
        for handle in archives.values():
            handle.close()
    return {"restored": restored, "safety_copies": safety, "safety_folder": f".pre-restore-{stamp}" if safety else None,
            "problems": problems}


def restore_git(bundle: Path, into: Path) -> dict[str, Any]:
    """Put a bundle's refs into a folder with no .git, keeping its files untouched.

    A Dropbox copy of the repo arrives with every file and no .git, so this brings
    history back without a checkout: init, fetch all refs, point the index at HEAD."""
    if (into / ".git").exists():
        raise BackupError(f"{into} already has .git; restore into an empty folder instead: git clone {bundle} <folder>")
    into.mkdir(parents=True, exist_ok=True)
    for args in (["-C", str(into), "init", "-q", "-b", "main"],
                 ["-C", str(into), "config", "core.autocrlf", "false"],
                 ["-C", str(into), "fetch", "-q", "--update-head-ok", str(bundle), "+refs/*:refs/*"],
                 ["-C", str(into), "reset", "-q"]):
        done = run_git(args)
        if done.returncode:
            raise BackupError(f"git {' '.join(args[2:4])} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return {"into": str(into), "bundle": str(bundle)}


# ---------------------------------------------------------------- reinstall

def dropbox_roots(home: Path, env: dict[str, str]) -> list[Path]:
    roots: list[Path] = []
    for folder in (env.get("APPDATA"), env.get("LOCALAPPDATA"), str(home / ".dropbox"), str(home / "AppData/Roaming/Dropbox"),
                   str(home / "AppData/Local/Dropbox")):
        if not folder:
            continue
        info = Path(folder) / ("info.json" if Path(folder).name.lower() in (".dropbox", "dropbox") else "Dropbox/info.json")
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        roots += [Path(acct["path"]) for acct in data.values() if isinstance(acct, dict) and acct.get("path")]
    roots += [home / "Dropbox", home / "Library" / "CloudStorage" / "Dropbox"]
    return list(dict.fromkeys(roots))


def search_folders(destination: Path | None, home: Path | None = None, env: dict[str, str] | None = None) -> list[Path]:
    """Where a reinstall looks: the destination, the recorded install, then Dropbox."""
    env = os.environ if env is None else env
    base = home if home is not None else Path.home()
    folders: list[Path] = []
    if destination is not None:
        folders.append(backups_root(destination))
    if env.get("PM_OS_ROOT"):
        folders.append(backups_root(Path(env["PM_OS_ROOT"])))
    env_file = Path(env.get("PM_OS_STATE_HOME") or base / ".pm-os") / "env"
    try:
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("PM_OS_ROOT=") and line.split("=", 1)[1].strip():
                folders.append(backups_root(Path(line.split("=", 1)[1].strip())))
    except OSError:
        pass
    for root in dropbox_roots(base, env):
        folders.append(backups_root(root / "Development" / "pm-os"))
    return list(dict.fromkeys(folders))


def find_for_install(destination: Path | None, home: Path | None = None, env: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[Path]]:
    folders = search_folders(destination, home, env)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for folder in folders:
        for row in list_backups(folder):
            key = os.path.normcase(str(row["path"]))
            if key not in seen:
                seen.add(key)
                rows.append(row)
    rows.sort(key=lambda r: (r["finished_at"] or "", r["date"]), reverse=True)
    return rows, folders


def restore_command(backup: Path, script: Path | None = None, repo: Path | None = None) -> str:
    # The script may be the kit's copy, whose own folder is not the install, so the
    # command names where the owner files go.
    target = f' --repo "{repo.as_posix()}"' if repo is not None else ""
    return f'python3 "{(script or Path(__file__).resolve()).as_posix()}"{target} restore --backup "{backup.as_posix()}" --apply'


def installer_offer(destination: Path | None, answer: str = "ask", dry_run: bool = False,
                    log: Callable[[str], None] = print, stdin: Any = None, home: Path | None = None,
                    env: dict[str, str] | None = None, script: Path | None = None) -> dict[str, Any]:
    """The reinstall step, called by the kit's install.py before it writes anything.

    Finds backups, checks the best one member by member, shows what a restore would
    do, and restores only on a yes. Newer local files stay in every case."""
    rows, folders = find_for_install(destination, home, env)
    if not rows:
        log("backup: none found. Looked in: " + "; ".join(str(f) for f in folders))
        log("backup: this is a clean install with no settings to bring back. If you expected one, stop now and "
            "check that Dropbox finished syncing the folder above.")
        return {"found": False, "searched": [str(f) for f in folders]}
    chosen = None
    for row in rank(rows):
        problems = verify_dir(Path(row["path"]))
        if not problems:
            chosen = row
            break
        log(f"backup: {row['path']} failed its check ({problems[0]}); trying the next one")
    if chosen is None:
        log(f"backup: {len(rows)} found and none passed its check. Nothing restored. Newest: {rows[0]['path']}")
        return {"found": True, "valid": False}
    backup = Path(chosen["path"])
    roots = home_roots(home, env, destination)
    plan = restore_plan(backup, roots)
    counts = plan_counts(plan)
    bundle = plan["manifest"].get("git_bundle") or {}
    log(f"backup: newest usable is {chosen['host']} {chosen['date']} ({chosen['finished_at']}) at {backup}")
    log(f"backup: manifest check passed: {chosen['files']} files, {fmt_bytes(chosen['archive_bytes'])} on disk, "
        + (f"git bundle with {bundle['refs']} refs" if bundle.get("file") else "no git bundle"))
    log(f"backup: a restore adds {counts['new']} files, replaces {counts['replace_older']} older files, keeps "
        f"{counts['keep_newer']} newer local files, and skips {counts['same']} identical files.")
    for line in plan_skips(plan)[:20]:
        log(f"backup: {line}")
    if plan["other_os"]:
        log(f"backup: this copy came from {plan['manifest'].get('platform')}, so {counts['skip_other_os']} files "
            "full of that machine's paths are skipped (env, settings.json, CLAUDE.md, plugin lists).")
    command = restore_command(backup, script, destination)
    result = {"found": True, "valid": True, "backup": str(backup), "counts": counts, "restored": 0, "command": command}
    if dry_run:
        log(f"backup: nothing restored by this run. To restore: {command}")
        return result
    stream = sys.stdin if stdin is None else stdin
    if answer == "ask":
        # isatty alone is not enough: on Windows the NUL device reports True. End of
        # input with no line at all also means nobody is there to answer.
        reply = ""
        if getattr(stream, "isatty", lambda: False)() or stdin is not None:
            log("backup: restore it now? Newer local files are never overwritten. [y/N]: ")
            reply = stream.readline()
        if not reply:
            log(f"backup: no terminal to ask on, so nothing was restored. To restore: {command}")
            log("backup: an installing agent asks the owner, then reruns with --restore-backup yes.")
            return result
        answer = "yes" if reply.strip().lower() in ("y", "yes") else "no"
    if answer != "yes":
        log(f"backup: not restored. To restore later: {command}")
        return result
    done = apply_restore(plan, roots)
    result.update(restored=done["restored"], problems=done["problems"], safety_folder=done["safety_folder"])
    log(f"backup: restored {done['restored']} files" + (
        f"; {done['safety_copies']} replaced files saved under {done['safety_folder']}" if done["safety_copies"] else ""))
    for problem in done["problems"][:20]:
        log(f"backup: problem: {problem}")
    log(f"backup: what was left out and how to get each one back: {backup / 'LEFT-OUT.txt'}")
    if bundle.get("file"):
        log(f'backup: commit history, for a folder with no .git: python3 "{(script or Path(__file__).resolve()).as_posix()}" '
            f'restore-git --backup "{backup.as_posix()}" --into "<PM OS folder>"')
    return result


# ---------------------------------------------------------------- health and alerts

def read_state(repo: Path) -> dict[str, Any]:
    try:
        return json.loads((repo / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(repo: Path, state: dict[str, Any]) -> None:
    atomic_write(repo / STATE_FILE, json.dumps(state, indent=1) + "\n")


def health(repo: Path, host: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """ok, or a reason a person should act on: last run failed, too old, never ran, broken."""
    now = now or now_utc()
    host = host or host_name()
    state = read_state(repo)
    rows = [r for r in list_backups(backups_root(repo)) if r["host"] == host]
    newest = next((r for r in rows if not r.get("broken")), None)
    if state.get("last_status") == "failed":
        reason = f"the last backup run failed: {state.get('last_error') or 'no detail'}"
        out = {"status": "failed", "reason": reason}
    elif newest is None:
        since = parse_iso(state.get("first_seen_at")) or now
        hours = (now - since).total_seconds() / 3600
        out = {"status": "stale" if hours > ALERT_AGE_HOURS else "none",
               "reason": f"no backup exists yet, {hours:.0f} hours after this check first ran"}
    else:
        finished = parse_iso(newest["finished_at"])
        hours = (now - finished).total_seconds() / 3600 if finished else 1e9
        problems = verify_dir(Path(newest["path"]), quick=True)
        if problems:
            out = {"status": "broken", "reason": f"the newest backup ({newest['date']}) failed its check: {problems[0]}"}
        elif hours > ALERT_AGE_HOURS:
            out = {"status": "stale", "reason": f"the newest backup ({newest['date']}) is {hours:.0f} hours old, over the {ALERT_AGE_HOURS}-hour limit"}
        else:
            out = {"status": "ok", "reason": f"newest backup {newest['date']}, {hours:.1f} hours old"}
    out["newest"] = {k: str(v) for k, v in newest.items()} if newest else None
    out["copies"] = len(rows)
    out["bytes_on_disk"] = sum(r["archive_bytes"] for r in rows)
    return out


def send_cos_alert(repo: Path, text: str) -> bool:
    """One urgent CoS line through report-to-cos.sh, the path check-state-inventory.py uses.

    True once report-to-cos.sh has taken the line: a busy CoS gets it from the waiter, and
    a retry here would only add a second task. One task id per day, because that script
    delivers a duplicate id once."""
    if os.environ.get("PM_OS_TEST_SANDBOX") == "1":
        return False
    stamp = datetime.now().strftime("%Y%m%d")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PM_OS_WORKER")}
    env["REPORT_TO_COS_NO_HOOKS"] = "1"
    cmd = ["bash", str(repo / "system/scripts/report-to-cos.sh"), "--task-id", f"local-backup-{stamp}", "--origin", "cos",
           "--workstream", "pm-os-maintenance", "--status", "blocked", "--urgent", "--create-if-missing", text]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180, stdin=subprocess.DEVNULL,
                              creationflags=NO_WINDOW)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def alert_text(reason: str) -> str:
    return (f"PM OS backup needs a look: {reason}. "
            "Options: (a) run python3 system/scripts/local_backup.py run now and read its error; "
            "(b) wait for the 02:30 PT run. Lean: a because the next night may fail the same way.")


def alert_if_needed(repo: Path, report: dict[str, Any], send: Callable[[Path, str], bool] = send_cos_alert,
                    now: datetime | None = None) -> bool:
    """Send one CoS line per problem, repeated at most daily; an ok result clears it."""
    now = now or now_utc()
    state = read_state(repo)
    state.setdefault("first_seen_at", iso(now))
    if report["status"] in ("ok", "none"):
        state.pop("alert_status", None)
        state.pop("alert_sent_at", None)
        write_state(repo, state)
        return False
    last = parse_iso(state.get("alert_sent_at"))
    if state.get("alert_status") == report["status"] and last and (now - last) < timedelta(hours=ALERT_REPEAT_HOURS):
        write_state(repo, state)
        return False
    # Stamped before the send: the job's time limit can kill this process mid-send, and
    # a lost stamp would send it again on every tick.
    state["alert_status"] = report["status"]
    state["alert_sent_at"] = iso(now)
    write_state(repo, state)
    sent = send(repo, alert_text(report["reason"]))
    if not sent:
        state = read_state(repo)
        state.pop("alert_status", None)
        state.pop("alert_sent_at", None)
        write_state(repo, state)
    return sent


def record_run(repo: Path, result: dict[str, Any] | None, error: str | None) -> None:
    state = read_state(repo)
    state.setdefault("first_seen_at", iso(now_utc()))
    state["last_run_at"] = iso(now_utc())
    state["last_status"] = "failed" if error else "ok"
    state["last_error"] = error
    if result:
        state["last_path"] = result["path"]
        state["last_totals"] = result["totals"]
        state["last_warnings"] = result.get("warnings") or []
    write_state(repo, state)


# ---------------------------------------------------------------- CLI

def cmd_run(args: argparse.Namespace) -> int:
    repo = args.repo
    started = now_utc()
    try:
        result = run_backup(repo, home=args.home, day=args.date, host=args.host, allow_shrink=args.allow_shrink)
        error = result.get("error")
    except Exception as exc:  # every failure becomes a recorded status and a CoS line
        result, error = None, f"{type(exc).__name__}: {exc}"
    note = slept_note(started, now_utc())
    if error and note:
        error = f"{error} ({note})"
    record_run(repo, result, error)
    if error and not args.no_alert:
        alert_if_needed(repo, {"status": "failed", "reason": f"the last backup run failed: {error}"})
    print(json.dumps(result or {"status": "failed", "error": error}, indent=1))
    return 1 if error else 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = list_backups(backups_root(args.repo))
    if not rows:
        print(f"no backups under {backups_root(args.repo)}")
    for row in rows:
        mark = "BROKEN" if row.get("broken") else f"{row['files']} files, {fmt_bytes(row['bytes'])} source, {fmt_bytes(row['archive_bytes'])} on disk"
        if row.get("partial"):
            mark += ", PARTIAL (no repo.bundle)"
        print(f"{row['host']}  {row['date']}  {mark}  {row['path']}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    backup = resolve_backup(args.backup, args.repo, args.host)
    problems = verify_dir(backup, quick=args.quick, git_check=not args.quick)
    print(f"{backup}: " + ("ok" if not problems else f"{len(problems)} problem(s)"))
    for problem in problems[:50]:
        print(f"  {problem}")
    return 1 if problems else 0


def cmd_restore(args: argparse.Namespace) -> int:
    backup = resolve_backup(args.backup, args.repo, args.host)
    problems = verify_dir(backup)
    if problems:
        print(f"{backup} failed its check, nothing restored:", *problems[:20], sep="\n  ")
        return 1
    roots = home_roots(args.home, repo=args.repo)
    plan = restore_plan(backup, roots)
    counts = plan_counts(plan)
    print(f"backup: {backup}")
    print(f"into: {roots['claude']}, {roots['pm-os']} and {roots[OWNER_ROOT]}")
    print(f"adds {counts['new']}, replaces {counts['replace_older']} older, keeps {counts['keep_newer']} newer local, "
          f"{counts['same']} identical, {counts['skip_other_os']} skipped (other OS)")
    for line in plan_skips(plan)[:50]:
        print(f"  {line}")
    if plan["other_os"]:
        print(f"this copy came from {plan['manifest'].get('platform')}: machine-path files are skipped")
    if not args.apply:
        print("nothing changed. Add --apply to restore.")
        return 0
    done = apply_restore(plan, roots)
    print(f"restored {done['restored']} files" + (
        f"; {done['safety_copies']} replaced files saved under {done['safety_folder']}" if done["safety_copies"] else ""))
    for problem in done["problems"][:50]:
        print(f"  problem: {problem}")
    print(f"left out, and how to get each one back: {backup / 'LEFT-OUT.txt'}")
    return 1 if done["problems"] else 0


def cmd_restore_git(args: argparse.Namespace) -> int:
    backup = resolve_backup(args.backup, args.repo, args.host)
    bundle = backup / BUNDLE_NAME
    if not bundle.is_file():
        print(f"{backup} has no {BUNDLE_NAME}")
        return 1
    restore_git(bundle, args.into)
    print(f"history restored into {args.into} from {bundle}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    report = health(args.repo, args.host)
    sent = alert_if_needed(args.repo, report) if args.alert else False
    print(json.dumps({**report, "alert_sent": sent}, indent=1))
    # Under --alert a problem is handled by the alert, so the scheduler logs no task error.
    return 0 if args.alert or report["status"] in ("ok", "none") else 1


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=None, help="PM OS folder (default: the main checkout)")
    parser.add_argument("--home", type=Path, default=None, help="read and restore under this home instead of the real one")
    parser.add_argument("--host", default=None, help="host folder under local/backups (default: this machine, then the rest)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--date", default=None)
    run.add_argument("--no-alert", action="store_true")
    run.add_argument("--allow-shrink", action="store_true", help="publish even if a folder lost over half its files")
    run.set_defaults(func=cmd_run)
    sub.add_parser("list").set_defaults(func=cmd_list)
    verify = sub.add_parser("verify")
    verify.add_argument("--backup", default="latest", help="latest, a date, or a folder")
    verify.add_argument("--quick", action="store_true")
    verify.set_defaults(func=cmd_verify)
    restore = sub.add_parser("restore")
    restore.add_argument("--backup", default="latest", help="latest, a date, or a folder")
    restore.add_argument("--apply", action="store_true")
    restore.set_defaults(func=cmd_restore)
    git = sub.add_parser("restore-git")
    git.add_argument("--backup", default="latest")
    git.add_argument("--into", type=Path, required=True)
    git.set_defaults(func=cmd_restore_git)
    check = sub.add_parser("health")
    check.add_argument("--alert", action="store_true")
    check.set_defaults(func=cmd_health)
    args = parser.parse_args(argv)
    args.repo = args.repo or default_repo()
    if args.command == "run" and args.host is None:
        args.host = host_name()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return args.func(args)
    except BackupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
