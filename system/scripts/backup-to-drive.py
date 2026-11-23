#!/usr/bin/env python3
"""Weekly backup of ~/Development and ~/Documents to Google Drive.

The machine's managed backup may exclude both folders, so this job
puts a dated copy in the owner's My Drive every week:

    My Drive/Backups/<YYYY-MM-DD>/              (Pacific date)
        Development/<top-level entry>.tar.zst   one archive per repo or folder
        Documents/<top-level entry>.tar.zst
        Development/<repo>.unpushed.bundle      only when that repo's .git was
                                                too big to ship (see GIT_MAX_BYTES)
        manifest.json                           parts, bytes, SHA-256, exclusions
        SHA256SUMS                              `shasum -a 256 -c` format

Why one archive per top-level entry instead of one per root: ~/Development is
~265 GiB on disk, so a single archive would need that much free disk to stage
and one broken upload would redo everything. Per-entry archives are staged one
at a time (tar | zstd), uploaded with the Drive v3 resumable protocol in 64 MiB
chunks, verified against Drive's sha256Checksum, then deleted locally.

Why the Drive API and not `switchboard drive`: that CLI's `upsert`
takes file content inline as base64 in the JSON payload, which cannot carry a
multi-GiB archive. Folder creation, listing and trashing use the same API so
the job has one auth path: the refresh token the installed gdrive skill keeps
in ~/.config/gdrive-skill/credentials.json (full drive scope).

Exclusions (recorded in manifest.json each run):
  * rebuildable: node_modules, .gradle, virtualenvs, __pycache__ and friends,
    .nx/.turbo/.next caches, Xcode DerivedData, SwiftPM .build, Gradle/Maven/
    Cargo output dirs (only next to their build file), CocoaPods Pods,
    switchboard .amp/worktrees (task checkouts of main)
  * secret-bearing: people/ and notes/sensitive/ at the top of any repo, .ssh,
    .aws, .gnupg, .env files (templates kept), credential/token/key files
  * .git is INCLUDED (a real backup) unless one .git directory exceeds
    GIT_MAX_BYTES; those are large monorepo clones re-clonable from GitHub.
    For them the manifest records remote, HEAD and branches, and a
    `git bundle` of every local commit not on a remote rides along.

Retention: after a fully verified run the oldest dated folders beyond KEEP are
moved to Drive trash (30-day undo). A failed run deletes nothing. KEEP (3)
is the single source of truth; the
drive-backup-weekly description in background-tasks.yaml only restates it.

Scheduling: `--scheduled` is called daily by background-tasks.yaml
(drive-backup-weekly, 02:00-24:00 PT window). It is due when no successful
run exists on or after the most recent Sunday (PT), so it fires at the first
executor tick on Sunday after 02:00, or at the first tick after the laptop
wakes if it slept through Sunday. Because the executor caps a tick at 25
minutes, `--scheduled` launches the real run detached (own session, caffeinate,
nice) and returns at once; an flock on system/state/drive-backup.lock skips
while a run is in flight. The run posts one line to the next-up digest.

    python3 system/scripts/backup-to-drive.py --dry-run      # size + plan only
    python3 system/scripts/backup-to-drive.py --run          # back up now
    python3 system/scripts/backup-to-drive.py --scheduled    # weekly gate
    python3 system/scripts/backup-to-drive.py --status
"""
from __future__ import annotations

import argparse
import datetime as dt
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import fnmatch
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_PATH = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT_PATH.parent))
from repo_paths import main_checkout  # noqa: E402

REPO = main_checkout(SCRIPT_PATH.parents[2])
PT = ZoneInfo("America/Los_Angeles")
HOME = Path.home()

ROOTS = {"Development": HOME / "Development", "Documents": HOME / "Documents"}
DRIVE_ROOT_NAME = "Backups"
KEEP = 3  # dated folders kept in Drive
GIT_MAX_BYTES = 2 * 1024 ** 3
CHUNK_BYTES = 64 * 1024 * 1024  # resumable chunks must be multiples of 256 KiB
ZSTD_LEVEL = "3"

STATE_FILE = REPO / "system/state/drive-backup.json"
LOCK_FILE = REPO / "system/state/drive-backup.lock"
LOG_FILE = REPO / "system/logs/drive-backup.log"
STAGING_ROOT = HOME / "Library/Caches/switchboard-drive-backup"
CREDENTIALS = HOME / ".config/gdrive-skill/credentials.json"

DATE_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"

# --- exclusion policy -------------------------------------------------------

# Directory basenames pruned at any depth.
PRUNE_DIRS = {
    "node_modules", ".gradle", ".venv", "venv", ".tox", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".nx", ".turbo", ".next", ".parcel-cache", ".cache",
    "DerivedData", ".build", ".ssh", ".aws", ".gnupg",
}
# Directory basename pruned only when a sibling marker file says it is build output.
PRUNE_DIRS_WITH_MARKER = {
    "build": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
    "target": ("pom.xml", "Cargo.toml"),
    "Pods": ("Podfile",),
}
# Paths relative to a top-level unit (repo or folder) that never ship.
PRUNE_UNIT_PATHS = {"people", "notes/sensitive", ".amp/worktrees"}
# File basenames (fnmatch) that never ship.
SECRET_FILES = (
    ".env", ".env.*", "credentials.json", "client_secret*.json", "service-account*.json",
    "*.pem", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    ".netrc", ".npmrc", ".pypirc", ".git-credentials", "token.json", "tokens.json", "*.token",
    ".secrets", ".secrets.*", "secrets.yaml", "secrets.yml", "secrets.json",
)
# .env templates are harmless and useful.
SECRET_FILE_KEEP_SUFFIXES = (".example", ".sample", ".template", ".dist")

def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            result = os.replace(src, dst)
            if os.name == "nt":   # refresh worktree hard links of a shared file (lib/winlink.py)
                try:
                    from lib import winlink as _winlink
                    _winlink.relink_shared(dst)
                except Exception:  # noqa: BLE001
                    pass
            return result
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)

def is_secret_file(name: str) -> bool:
    if name.startswith(".env") and name.endswith(SECRET_FILE_KEEP_SUFFIXES):
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_FILES)


def prune_dir(parent: Path, name: str, unit_rel: str) -> str | None:
    """Reason a directory is pruned, or None to descend."""
    if name in PRUNE_DIRS:
        return "rebuildable" if name not in (".ssh", ".aws", ".gnupg") else "secret"
    markers = PRUNE_DIRS_WITH_MARKER.get(name)
    if markers and any((parent / marker).exists() for marker in markers):
        return "rebuildable"
    if (parent / name / "pyvenv.cfg").is_file():  # any virtualenv, whatever its name
        return "rebuildable"
    if unit_rel in PRUNE_UNIT_PATHS:
        return "secret" if unit_rel != ".amp/worktrees" else "rebuildable"
    return None


# --- planning ---------------------------------------------------------------

class Entry:
    __slots__ = ("rel", "bytes", "is_dir")

    def __init__(self, rel: str, size: int, is_dir: bool):
        self.rel, self.bytes, self.is_dir = rel, size, is_dir


class Unit:
    """One top-level entry of a root: becomes one archive."""

    def __init__(self, root_name: str, root: Path, name: str, paths: list[Path]):
        self.root_name = root_name
        self.root = root
        self.name = name  # archive stem; "_files" bundles loose top-level files
        self.paths = paths  # absolute top-level paths included in this unit
        self.entries: list[Entry] = []
        self.excluded: list[dict] = []  # {path, bytes, reason}
        self.skipped = False  # already verified in Drive this run-date; not walked
        self.git_excluded: list[dict] = []  # {path, bytes}

    @property
    def files(self) -> int:
        return sum(1 for e in self.entries if not e.is_dir)

    @property
    def bytes(self) -> int:
        return sum(e.bytes for e in self.entries)

    @property
    def archive_name(self) -> str:
        return f"{self.name}.tar.zst"


def walk_tree(top: Path, root: Path, unit_top: Path, unit: Unit) -> tuple[list[Entry], int]:
    """Collect entries below `top` (inclusive) with the exclusion policy applied.
    Returns (entries, excluded_bytes)."""
    entries: list[Entry] = []
    excluded_bytes = 0
    stack = [top]
    while stack:
        current = stack.pop()
        try:
            st = current.lstat()
        except OSError:
            continue
        rel = current.relative_to(root).as_posix()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            entries.append(Entry(rel, st.st_size if stat.S_ISREG(st.st_mode) else 0, False))
            continue
        entries.append(Entry(rel, 0, True))
        try:
            children = sorted(os.scandir(current), key=lambda d: d.name)
        except OSError:
            continue
        for child in children:
            try:
                cst = child.stat(follow_symlinks=False)
            except OSError:
                continue
            path = Path(child.path)
            unit_rel = path.relative_to(unit_top).as_posix() if path != unit_top else ""
            if stat.S_ISDIR(cst.st_mode) and not stat.S_ISLNK(cst.st_mode):
                if child.name == ".git":
                    git_entries, _ = walk_tree(path, root, unit_top, unit)
                    size = sum(e.bytes for e in git_entries)
                    if size > GIT_MAX_BYTES:
                        unit.git_excluded.append({"path": path.relative_to(root).as_posix(), "bytes": size})
                        excluded_bytes += size
                    else:
                        entries.extend(git_entries)
                    continue
                reason = prune_dir(current, child.name, unit_rel)
                if reason:
                    size = du_bytes(path)
                    unit.excluded.append({"path": path.relative_to(root).as_posix(), "bytes": size, "reason": reason})
                    excluded_bytes += size
                    continue
                stack.append(path)
            elif stat.S_ISREG(cst.st_mode) or stat.S_ISLNK(cst.st_mode):
                if stat.S_ISREG(cst.st_mode) and is_secret_file(child.name):
                    unit.excluded.append({"path": path.relative_to(root).as_posix(), "bytes": cst.st_size, "reason": "secret"})
                    excluded_bytes += cst.st_size
                    continue
                entries.append(Entry(path.relative_to(root).as_posix(),
                                     cst.st_size if stat.S_ISREG(cst.st_mode) else 0, False))
            # sockets, fifos, devices: skipped silently
    return entries, excluded_bytes


def du_bytes(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def plan_root(root_name: str, root: Path, skip: set[str] = frozenset()) -> list[Unit]:
    units: list[Unit] = []
    loose: list[Path] = []
    try:
        children = sorted(os.scandir(root), key=lambda d: d.name)
    except OSError as exc:
        raise SystemExit(f"cannot read {root}: {exc}")
    for child in children:
        if child.name in (".DS_Store", ".localized"):
            continue
        path = Path(child.path)
        if child.is_dir(follow_symlinks=False):
            unit = Unit(root_name, root, child.name, [path])
            if child.name in skip:
                unit.skipped = True
            else:
                unit.entries, _ = walk_tree(path, root, path, unit)
            units.append(unit)
        else:
            loose.append(path)
    if loose:
        unit = Unit(root_name, root, "_files", loose)
        for path in loose:
            try:
                st = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and is_secret_file(path.name):
                unit.excluded.append({"path": path.name, "bytes": st.st_size, "reason": "secret"})
                continue
            unit.entries.append(Entry(path.name, st.st_size if stat.S_ISREG(st.st_mode) else 0, False))
        units.append(unit)
    return units


def plan(roots: dict[str, Path], skip: dict[str, set[str]] | None = None) -> list[Unit]:
    """`skip` maps root name -> unit names already verified in Drive (same-day resume);
    those units are listed but not walked."""
    units: list[Unit] = []
    for root_name, root in roots.items():
        if not root.is_dir():
            log(f"root missing, skipped: {root}")
            continue
        units.extend(plan_root(root_name, root, (skip or {}).get(root_name, set())))
    return units


# --- git info for excluded .git ----------------------------------------------

def git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)


def git_info(repo: Path) -> dict:
    info = {"repo": str(repo)}
    for key, args in (("head", ("rev-parse", "HEAD")), ("branch", ("rev-parse", "--abbrev-ref", "HEAD")),
                      ("remotes", ("remote", "-v")),
                      ("branches", ("for-each-ref", "--format=%(refname:short) %(objectname:short)", "refs/heads"))):
        result = git(repo, *args)
        info[key] = result.stdout.strip() if result.returncode == 0 else f"error: {result.stderr.strip()[:200]}"
    status = git(repo, "status", "--porcelain", "--ignore-submodules=all", timeout=600)
    info["dirty_files"] = len(status.stdout.splitlines()) if status.returncode == 0 else None
    return info


def unpushed_bundle(repo: Path, output: Path) -> dict:
    """Bundle every local ref's commits that no remote-tracking ref reaches."""
    refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags", "refs/stash")
    names = refs.stdout.split()
    if refs.returncode != 0 or not names:
        return {"bundle": None, "reason": "no local refs"}
    # Monorepo clones carry thousands of tags: pass refs on stdin, not argv.
    result = subprocess.run(["git", "-C", str(repo), "bundle", "create", str(output), "--stdin", "--not", "--remotes"],
                            input="\n".join(names) + "\n", capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        output.unlink(missing_ok=True)
        if "empty bundle" in result.stderr.lower():
            return {"bundle": None, "reason": "nothing unpushed"}
        return {"bundle": None, "reason": f"error: {result.stderr.strip()[:300]}"}
    return {"bundle": output.name, "bytes": output.stat().st_size}


# --- archive ------------------------------------------------------------------

def write_file_list(unit: Unit, target: Path) -> None:
    with target.open("wb") as handle:
        for entry in unit.entries:
            handle.write(entry.rel.encode("utf-8", "surrogateescape") + b"\0")


def stage_archive(unit: Unit, staging: Path) -> tuple[Path, list[str]]:
    """tar (from an explicit NUL-separated member list) | zstd -> staging/<unit>.tar.zst.
    Returns (archive, warnings). bsdtar exit 1 means warnings (file changed while
    reading); anything above 1 is an error."""
    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / unit.archive_name
    listing = staging / f"{unit.name}.list"
    write_file_list(unit, listing)
    # bsdtar spends most of its time in setup_mac_metadata (per-file copyfile
    # of xattrs, resource forks, ACLs); skipping them made staging 2.5x faster
    # in a 20k-file sample. Finder tags and quarantine flags are not backed up.
    tar_cmd = ["tar", "--no-mac-metadata", "--no-xattrs", "--no-acls", "--no-fflags",
               "-cf", "-", "-C", str(unit.root), "--no-recursion", "--null", "-T", str(listing)]
    zstd_cmd = ["zstd", "-T0", f"-{ZSTD_LEVEL}", "-q", "-f", "-o", str(archive)]
    with archive.open("wb"):
        pass
    tar = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    zstd = subprocess.Popen(zstd_cmd, stdin=tar.stdout, stderr=subprocess.PIPE)
    tar.stdout.close()
    _, zstd_err = zstd.communicate()
    tar_err = tar.stderr.read().decode("utf-8", "replace")
    tar.stderr.close()
    tar.wait()
    listing.unlink(missing_ok=True)
    if zstd.returncode != 0:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"zstd failed ({zstd.returncode}): {zstd_err.decode('utf-8', 'replace').strip()[-400:]}")
    if tar.returncode > 1:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"tar failed ({tar.returncode}): {tar_err.strip()[-400:]}")
    warnings = [line for line in tar_err.splitlines() if line.strip()]
    return archive, warnings


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def free_bytes(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return usage.free


# --- Drive client -------------------------------------------------------------

class DriveError(Exception):
    pass


class Drive:
    """Minimal Drive v3 client over urllib with token refresh and retries."""

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, credentials: Path = CREDENTIALS, opener=None):
        self.credentials_path = credentials
        self._token = None
        self._token_expires = 0.0
        self._open = opener or urllib.request.urlopen

    # auth
    def token(self) -> str:
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        creds = json.loads(self.credentials_path.read_text())
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": creds["refresh_token"],
            "client_id": creds["client_id"], "client_secret": creds["client_secret"],
        }).encode()
        request = urllib.request.Request(creds["token_uri"], data=data,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self._open(request, timeout=30) as response:
            payload = json.loads(response.read())
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    # low-level
    def request(self, method: str, url: str, *, data: bytes | None = None, headers: dict | None = None,
                timeout: int = 120, retries: int = 6, ok=(200, 201)) -> tuple[int, dict, bytes]:
        attempt = 0
        while True:
            attempt += 1
            hdrs = {"Authorization": f"Bearer {self.token()}"}
            hdrs.update(headers or {})
            request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with self._open(request, timeout=timeout) as response:
                    return response.status, dict(response.headers), response.read()
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if exc.code in ok:
                    return exc.code, dict(exc.headers), body
                if exc.code == 308:
                    return 308, dict(exc.headers), body
                if exc.code == 401 and attempt <= retries:
                    self._token = None
                    continue
                if exc.code in self.RETRY_STATUS and attempt <= retries:
                    time.sleep(min(60, 2 ** attempt))
                    continue
                raise DriveError(f"{method} {url} -> {exc.code}: {body[:300]!r}") from None
            except (urllib.error.URLError, socket.timeout, http.client.HTTPException, ConnectionError, OSError) as exc:
                if attempt <= retries:
                    time.sleep(min(60, 2 ** attempt))
                    continue
                raise DriveError(f"{method} {url} -> {exc}") from None

    def api(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        url = f"{DRIVE_API}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        status, _, payload = self.request(method, url, data=data, headers=headers, ok=(200, 201, 204))
        return json.loads(payload) if payload else {}

    # files/folders
    def list_children(self, parent_id: str, *, mime: str | None = None, name: str | None = None,
                      fields: str = "id,name,mimeType,size,sha256Checksum,createdTime") -> list[dict]:
        clauses = [f"'{parent_id}' in parents", "trashed = false"]
        if mime:
            clauses.append(f"mimeType = '{mime}'")
        if name:
            clauses.append("name = '" + name.replace("\\", "\\\\").replace("'", "\\'") + "'")
        items, token = [], None
        while True:
            params = {"q": " and ".join(clauses), "fields": f"nextPageToken,files({fields})", "pageSize": 200,
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "false"}
            if token:
                params["pageToken"] = token
            page = self.api("GET", "files", params=params)
            items.extend(page.get("files", []))
            token = page.get("nextPageToken")
            if not token:
                return items

    def ensure_folder(self, name: str, parent_id: str) -> dict:
        found = self.list_children(parent_id, mime=FOLDER_MIME, name=name, fields="id,name,webViewLink,createdTime")
        if found:
            return sorted(found, key=lambda f: f.get("createdTime", ""))[0]
        return self.api("POST", "files", params={"fields": "id,name,webViewLink,createdTime", "supportsAllDrives": "true"},
                        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]})

    def get(self, file_id: str, fields: str = "id,name,size,sha256Checksum,webViewLink,trashed") -> dict:
        return self.api("GET", f"files/{file_id}", params={"fields": fields, "supportsAllDrives": "true"})

    def trash(self, file_id: str) -> dict:
        return self.api("PATCH", f"files/{file_id}", params={"fields": "id,trashed", "supportsAllDrives": "true"},
                        body={"trashed": True})

    def upload_text(self, name: str, parent_id: str, text: str, mime: str = "text/plain") -> dict:
        boundary = "switchboard-drive-backup-boundary"
        meta = json.dumps({"name": name, "parents": [parent_id]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n{text}\r\n--{boundary}--").encode("utf-8")
        status, _, payload = self.request(
            "POST", f"{UPLOAD_API}?uploadType=multipart&fields=id,name,size,webViewLink&supportsAllDrives=true",
            data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return json.loads(payload)

    # resumable upload
    def upload_file(self, path: Path, name: str, parent_id: str, *, chunk_bytes: int = CHUNK_BYTES,
                    progress=None, max_restarts: int = 2) -> dict:
        total = path.stat().st_size
        restarts = 0
        while True:
            session = self._start_session(name, parent_id, total)
            try:
                return self._send(session, path, total, chunk_bytes, progress)
            except SessionLost:
                restarts += 1
                if restarts > max_restarts:
                    raise DriveError(f"upload session lost {restarts} times for {name}")
                log(f"upload session lost for {name}; restarting ({restarts}/{max_restarts})")

    def _start_session(self, name: str, parent_id: str, total: int) -> str:
        meta = json.dumps({"name": name, "parents": [parent_id]}).encode()
        status, headers, _ = self.request(
            "POST", f"{UPLOAD_API}?uploadType=resumable&supportsAllDrives=true",
            data=meta, headers={"Content-Type": "application/json; charset=UTF-8",
                                "X-Upload-Content-Type": "application/octet-stream",
                                "X-Upload-Content-Length": str(total)})
        location = {k.lower(): v for k, v in headers.items()}.get("location")
        if not location:
            raise DriveError("resumable session: no Location header")
        return location

    def _send(self, session: str, path: Path, total: int, chunk_bytes: int, progress) -> dict:
        offset = 0
        fields = "id,name,size,sha256Checksum,md5Checksum,webViewLink"
        session_url = session + ("&" if "?" in session else "?") + "fields=" + fields
        with path.open("rb") as handle:
            while offset < total:
                handle.seek(offset)
                chunk = handle.read(chunk_bytes)
                end = offset + len(chunk) - 1
                try:
                    status, headers, payload = self.request(
                        "PUT", session_url, data=chunk,
                        headers={"Content-Type": "application/octet-stream",
                                 "Content-Range": f"bytes {offset}-{end}/{total}"},
                        timeout=600, retries=0, ok=(200, 201))
                except DriveError as exc:
                    if " 404" in str(exc) or " 410" in str(exc):
                        raise SessionLost from None
                    log(f"chunk {offset}-{end} failed ({str(exc)[:160]}); querying resume offset")
                    offset = self._resume_offset(session_url, total)
                    if offset is None:
                        raise SessionLost from None
                    if isinstance(offset, dict):
                        return offset
                    continue
                if status == 308:
                    offset = self._next_offset(headers, end + 1)
                else:
                    if progress:
                        progress(total, total)
                    return json.loads(payload)
                if progress:
                    progress(offset, total)
        # zero-byte file or loop exit without completion: ask the server.
        result = self._resume_offset(session_url, total)
        if isinstance(result, dict):
            return result
        raise DriveError("upload ended without a completed response")

    @staticmethod
    def _next_offset(headers: dict, fallback: int) -> int:
        rng = {k.lower(): v for k, v in headers.items()}.get("range")
        if rng and "-" in rng:
            return int(rng.split("-")[-1]) + 1
        return fallback if not rng else 0

    def _resume_offset(self, session_url: str, total: int):
        """308 -> next byte offset (int); 200/201 -> completed metadata (dict); None -> session gone."""
        for attempt in range(1, 7):
            try:
                status, headers, payload = self.request(
                    "PUT", session_url, data=b"", headers={"Content-Range": f"bytes */{total}"},
                    timeout=120, retries=0, ok=(200, 201))
            except DriveError as exc:
                if " 404" in str(exc) or " 410" in str(exc):
                    return None
                time.sleep(min(120, 5 * 2 ** attempt))
                continue
            if status == 308:
                return self._next_offset(headers, 0)
            return json.loads(payload)
        return None


class SessionLost(Exception):
    pass


# --- state, lock, log -----------------------------------------------------------

def log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG_FILE.open("a") as handle:
        handle.write(f"[{stamp}] {message}\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except ValueError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    _os_replace(tmp, STATE_FILE)


def now_pt() -> dt.datetime:
    return dt.datetime.now(PT)


def most_recent_sunday(when: dt.datetime) -> dt.date:
    """The Sunday on or before `when` (PT date)."""
    today = when.date()
    return today - dt.timedelta(days=(today.weekday() + 1) % 7)


def is_due(state: dict, when: dt.datetime, start_hour: int = 2) -> tuple[bool, str]:
    """Weekly gate: due when no successful run exists on or after the most recent
    Sunday (PT). On Sunday itself wait until start_hour so the run lands in the
    quiet window; a laptop asleep through Sunday catches up on its next tick."""
    last = state.get("last_success_date")
    sunday = most_recent_sunday(when)
    if when.date() == sunday and when.hour < start_hour:
        return False, f"Sunday before {start_hour:02d}:00 PT"
    if last and dt.date.fromisoformat(last) >= sunday:
        return False, f"already backed up {last} (week of {sunday})"
    return True, f"no backup since Sunday {sunday}" if last else "never backed up"


def human(num: int | float) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def duration_text(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def digest_line(date: str, status: str, totals: dict, duration: float, link: str | None, note: str = "") -> str:
    parts = []
    for root_name in ROOTS:
        info = totals.get(root_name)
        if info:
            parts.append(f"{root_name} {human(info['archive_bytes'])} in {info['parts']} parts")
    body = ", ".join(parts) if parts else "no archives"
    line = f"drive backup {date}: {status}, {body}, {duration_text(duration)}"
    if link:
        line += f", {link}"
    if note:
        line += f". {note}"
    return line


def send_digest(line: str, date: str, status: str) -> dict:
    script = REPO / "system/scripts/report-to-cos.sh"
    env = dict(os.environ, REPORT_TO_COS_NO_HOOKS="1")
    cmd = ["bash", str(script), "--task-id", f"drive-backup-{date.replace('-', '')}", "--origin", "cos",
           "--workstream", "switchboard-maintenance", "--status", "done" if status == "ok" else "failed",
           "--delivery", "digest", "--create-if-missing", line]
    try:
        result = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"sent": False, "error": str(exc)}
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"sent": False, "returncode": result.returncode, "stderr": result.stderr.strip()[-400:]}


# --- retention ----------------------------------------------------------------------

def retention_candidates(folders: list[dict], keep: int, today: str) -> list[dict]:
    """Dated folders to trash: everything older than the newest `keep`, never
    today's folder, never anything that is not a YYYY-MM-DD name."""
    dated = sorted((f for f in folders if DATE_FOLDER.match(f.get("name", ""))), key=lambda f: f["name"], reverse=True)
    return [f for f in dated[keep:] if f["name"] != today]


# --- the run ----------------------------------------------------------------------------

def run_backup(*, date: str, roots: dict[str, Path], keep: int, drive: Drive | None, dry_run: bool,
               staging_root: Path, progress_path: Path | None = None) -> dict:
    started = time.time()
    report = {"date": date, "dry_run": dry_run, "status": "ok", "roots": {k: str(v) for k, v in roots.items()},
              "parts": [], "errors": [], "warnings": [], "git_excluded": [], "excluded_bytes": 0,
              "exclusion_policy": {"prune_dirs": sorted(PRUNE_DIRS), "prune_dirs_with_marker": PRUNE_DIRS_WITH_MARKER,
                                   "prune_unit_paths": sorted(PRUNE_UNIT_PATHS), "secret_files": list(SECRET_FILES),
                                   "git_max_bytes": GIT_MAX_BYTES, "keep": keep}}
    log(f"plan start ({'dry run' if dry_run else 'live'}) roots={list(roots)}")
    progress = {}
    if progress_path and progress_path.exists():
        try:
            progress = json.loads(progress_path.read_text())
        except ValueError:
            progress = {}
    skip: dict[str, set[str]] = {}
    for key, done in progress.items():
        root_name, archive = key.split("/", 1)
        if done.get("verified") and archive.endswith(".tar.zst"):
            skip.setdefault(root_name, set()).add(archive[: -len(".tar.zst")])
    all_units = plan(roots, skip)
    excluded = [dict(e, unit=u.name, root=u.root_name) for u in all_units for e in u.excluded]
    report["excluded"] = sorted(excluded, key=lambda e: -e["bytes"])
    report["excluded_bytes"] = sum(e["bytes"] for e in excluded) + sum(g["bytes"] for u in all_units for g in u.git_excluded)
    units = [u for u in all_units if u.files > 0 or u.skipped]
    report["source_files"] = sum(u.files for u in units)
    report["source_bytes"] = sum(u.bytes for u in units)
    log(f"plan: {len(units)} units, {report['source_files']} files, {human(report['source_bytes'])} selected, "
        f"{human(report['excluded_bytes'])} excluded")
    for unit in units:
        report["parts"].append({"root": unit.root_name, "unit": unit.name, "name": unit.archive_name,
                                "files": unit.files, "bytes": unit.bytes})
        for g in unit.git_excluded:
            report["git_excluded"].append(dict(g, root=unit.root_name, unit=unit.name))
    if dry_run:
        report["duration_seconds"] = time.time() - started
        return report

    assert drive is not None

    root_folder = drive.ensure_folder(DRIVE_ROOT_NAME, "root")
    dated = drive.ensure_folder(date, root_folder["id"])
    report["drive_folder"] = {"id": dated["id"], "link": dated.get("webViewLink") or f"https://drive.google.com/drive/folders/{dated['id']}"}
    log(f"drive folder {DRIVE_ROOT_NAME}/{date} = {dated['id']}")
    root_folders = {name: drive.ensure_folder(name, dated["id"]) for name in roots if any(u.root_name == name for u in units)}
    staging = staging_root / date
    staging.mkdir(parents=True, exist_ok=True)

    for unit, part in zip(units, report["parts"]):
        key = f"{unit.root_name}/{unit.archive_name}"
        done = progress.get(key)
        if done and done.get("drive_id"):
            try:
                meta = drive.get(done["drive_id"])
                if not meta.get("trashed") and meta.get("sha256Checksum") == done.get("sha256"):
                    part.update(done, resumed=True)
                    log(f"{key}: already verified in Drive, skipped")
                    continue
            except DriveError as exc:
                log(f"{key}: progress check failed ({exc}); re-uploading")
        if unit.skipped:  # Drive copy gone or changed: walk it now and redo
            unit.skipped = False
            unit.entries, _ = walk_tree(unit.paths[0], unit.root, unit.paths[0], unit)
            part.update(files=unit.files, bytes=unit.bytes)
        try:
            needed = unit.bytes + 2 * 1024 ** 3
            if free_bytes(staging) < needed:
                raise RuntimeError(f"insufficient free disk for staging: need ~{human(needed)}, have {human(free_bytes(staging))}")
            t0 = time.time()
            archive, warnings = stage_archive(unit, staging)
            part["archive_bytes"] = archive.stat().st_size
            part["tar_warnings"] = len(warnings)
            if warnings:
                report["warnings"].append({"part": key, "tar": warnings[:20]})
            part["sha256"] = sha256_file(archive)
            part["stage_seconds"] = round(time.time() - t0, 1)
            log(f"{key}: staged {human(part['archive_bytes'])} (from {human(unit.bytes)}, {unit.files} files, "
                f"{part['stage_seconds']}s, {len(warnings)} tar warnings) sha256 {part['sha256'][:12]}")
            t1 = time.time()
            meta = drive.upload_file(archive, unit.archive_name, root_folders[unit.root_name]["id"],
                                     progress=lambda sent, total, k=key: log(f"{k}: {human(sent)}/{human(total)}")
                                     if sent % (CHUNK_BYTES * 16) == 0 or sent == total else None)
            part["upload_seconds"] = round(time.time() - t1, 1)
            part["drive_id"] = meta["id"]
            part["link"] = meta.get("webViewLink")
            # Drive computes checksums for big files asynchronously; poll up to ~10 min.
            # The final PUT response omits it, so fetch metadata right away and
            # only then back off.
            remote_sha = meta.get("sha256Checksum") or drive.get(meta["id"]).get("sha256Checksum")
            for _ in range(60):
                if remote_sha:
                    break
                time.sleep(10)
                remote_sha = drive.get(meta["id"]).get("sha256Checksum")
            part["drive_sha256"] = remote_sha
            if remote_sha != part["sha256"]:
                raise RuntimeError(f"SHA-256 mismatch after upload: local {part['sha256'][:12]} drive {str(remote_sha)[:12]}")
            part["verified"] = True
            log(f"{key}: uploaded in {part['upload_seconds']}s, verified")
            archive.unlink(missing_ok=True)
            progress[key] = {k: part[k] for k in ("files", "bytes", "archive_bytes", "sha256", "drive_id", "link", "verified")
                             if k in part}
            if progress_path:
                progress_path.write_text(json.dumps(progress, indent=2))
        except (RuntimeError, DriveError, OSError) as exc:
            part["error"] = str(exc)[:500]
            report["errors"].append({"part": key, "error": str(exc)[:500]})
            log(f"{key}: FAILED {exc}")
            (staging / unit.archive_name).unlink(missing_ok=True)

        for g in [g for g in report["git_excluded"] if g["unit"] == unit.name and g["root"] == unit.root_name and "info" not in g]:
            repo = unit.root / Path(g["path"]).parent
            g["info"] = git_info(repo)
            bundle_path = staging / f"{unit.name}.unpushed.bundle"
            try:
                g["unpushed"] = unpushed_bundle(repo, bundle_path)
                if g["unpushed"].get("bundle"):
                    g["unpushed"]["sha256"] = sha256_file(bundle_path)
                    meta = drive.upload_file(bundle_path, bundle_path.name, root_folders[unit.root_name]["id"])
                    g["unpushed"]["drive_id"] = meta["id"]
                    g["unpushed"]["link"] = meta.get("webViewLink")
                    log(f"{key}: unpushed bundle {human(g['unpushed']['bytes'])} uploaded")
            except (RuntimeError, DriveError, OSError, subprocess.SubprocessError) as exc:
                g["unpushed"] = {"bundle": None, "reason": f"error: {str(exc)[:300]}"}
                report["warnings"].append({"part": key, "bundle": str(exc)[:300]})
            finally:
                bundle_path.unlink(missing_ok=True)

    report["status"] = "ok" if not report["errors"] else "failed"
    report["duration_seconds"] = time.time() - started
    report["totals"] = totals(report)
    report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    manifest = {k: v for k, v in report.items() if k not in ("dry_run",)}
    try:
        drive.upload_text("manifest.json", dated["id"], json.dumps(manifest, indent=2, default=str), "application/json")
        sums = "".join(f"{p['sha256']}  {p['root']}/{p['name']}\n" for p in report["parts"] if p.get("sha256"))
        sums += "".join(f"{g['unpushed']['sha256']}  {g['root']}/{g['unpushed']['bundle']}\n"
                        for g in report["git_excluded"] if g.get("unpushed", {}).get("sha256"))
        drive.upload_text("SHA256SUMS", dated["id"], sums)
    except DriveError as exc:
        report["warnings"].append({"manifest": str(exc)[:300]})
        log(f"manifest upload failed: {exc}")

    if report["status"] == "ok":
        try:
            folders = drive.list_children(root_folder["id"], mime=FOLDER_MIME, fields="id,name,createdTime")
            trashed = []
            for folder in retention_candidates(folders, keep, date):
                drive.trash(folder["id"])
                trashed.append(folder["name"])
                log(f"retention: trashed {DRIVE_ROOT_NAME}/{folder['name']}")
            report["retention_trashed"] = trashed
        except DriveError as exc:
            report["warnings"].append({"retention": str(exc)[:300]})
        if progress_path:
            progress_path.unlink(missing_ok=True)
    shutil.rmtree(staging, ignore_errors=True)
    return report


def totals(report: dict) -> dict:
    out = {}
    for part in report["parts"]:
        info = out.setdefault(part["root"], {"parts": 0, "files": 0, "bytes": 0, "archive_bytes": 0})
        if part.get("verified"):
            info["parts"] += 1
            info["files"] += part["files"]
            info["bytes"] += part["bytes"]
            info["archive_bytes"] += part.get("archive_bytes", 0)
    return out


# --- entry points ---------------------------------------------------------------------------

def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def do_run(args) -> int:
    lock = acquire_lock()
    if lock is None:
        print(json.dumps({"status": "skipped", "reason": "another backup run holds the lock"}))
        return 0
    when = now_pt()
    date = args.date or when.strftime("%Y-%m-%d")
    state = load_state()
    state.update(in_flight={"pid": os.getpid(), "date": date, "started_at": when.isoformat(timespec="seconds")})
    save_state(state)
    roots = dict(ROOTS)
    if args.root:
        roots = {name: Path(path).expanduser() for name, path in (r.split("=", 1) for r in args.root)}
    drive = Drive()
    progress_path = STATE_FILE.with_name(f"drive-backup-progress-{date}.json")
    try:
        report = run_backup(date=date, roots=roots, keep=args.keep, drive=drive, dry_run=False,
                            staging_root=STAGING_ROOT, progress_path=progress_path)
    except Exception as exc:  # noqa: BLE001 - the digest line must still go out
        log(f"run crashed: {exc!r}")
        report = {"date": date, "status": "failed", "errors": [{"error": repr(exc)[:500]}], "parts": [],
                  "duration_seconds": 0, "totals": {}}
    state = load_state()
    state.pop("in_flight", None)
    state["last_run"] = {"date": date, "status": report["status"], "finished_at": report.get("finished_at"),
                         "duration_seconds": round(report.get("duration_seconds", 0)),
                         "drive_folder": report.get("drive_folder"), "totals": report.get("totals"),
                         "errors": report.get("errors", [])[:5]}
    if report["status"] == "ok":
        state["last_success_date"] = date
        state["last_success"] = state["last_run"]
    save_state(state)
    note = ""
    if report["errors"]:
        first = report["errors"][0]
        note = f"{len(report['errors'])} part(s) failed; first: {first.get('part', '')} {first['error'][:120]}"
    line = digest_line(date, report["status"], report.get("totals", {}), report.get("duration_seconds", 0),
                       (report.get("drive_folder") or {}).get("link"), note)
    report["digest_line"] = line
    log(line)
    if not args.no_digest:
        report["digest"] = send_digest(line, date, report["status"])
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    print(json.dumps({k: v for k, v in report.items() if k not in ("excluded", "exclusion_policy")}, indent=2, default=str))
    return 0 if report["status"] == "ok" else 1


def do_scheduled(args) -> int:
    when = now_pt()
    state = load_state()
    due, reason = is_due(state, when, start_hour=args.start_hour)
    if not due:
        print(json.dumps({"status": "skipped", "reason": reason, "now_pt": when.isoformat(timespec="minutes")}))
        return 0
    lock = acquire_lock()
    if lock is None:
        print(json.dumps({"status": "skipped", "reason": "a backup run is in flight", "in_flight": state.get("in_flight")}))
        return 0
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(SCRIPT_PATH), "--run", "--keep", str(args.keep)]
    if shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + cmd
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10"] + cmd
    with LOG_FILE.open("a") as out:
        child = subprocess.Popen(cmd, cwd=str(REPO), stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                 **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}))
    log(f"scheduled: launched detached run pid {child.pid} ({reason})")
    print(json.dumps({"status": "launched", "pid": child.pid, "reason": reason, "now_pt": when.isoformat(timespec="minutes")}))
    return 0


def do_dry_run(args) -> int:
    when = now_pt()
    roots = dict(ROOTS)
    if args.root:
        roots = {name: Path(path).expanduser() for name, path in (r.split("=", 1) for r in args.root)}
    report = run_backup(date=args.date or when.strftime("%Y-%m-%d"), roots=roots, keep=args.keep, drive=None,
                        dry_run=True, staging_root=STAGING_ROOT)
    report["excluded"] = report["excluded"][:40]
    print(json.dumps(report, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Walk the roots, print the plan and sizes; touch nothing")
    mode.add_argument("--run", action="store_true", help="Back up now (ignores the weekly gate)")
    mode.add_argument("--scheduled", action="store_true", help="Weekly gate; launches --run detached when due")
    mode.add_argument("--status", action="store_true", help="Print the state file")
    parser.add_argument("--keep", type=int, default=KEEP, help=f"Dated folders to keep (default {KEEP})")
    parser.add_argument("--date", help="Override the dated folder name (YYYY-MM-DD, tests)")
    parser.add_argument("--root", action="append", metavar="NAME=PATH", help="Override roots (tests), repeatable")
    parser.add_argument("--no-digest", action="store_true", help="Skip the next-up digest line")
    parser.add_argument("--start-hour", type=int, default=2, help="Sunday window start for --scheduled (PT hour)")
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(load_state(), indent=2))
        return 0
    if args.dry_run:
        return do_dry_run(args)
    if args.scheduled:
        return do_scheduled(args)
    return do_run(args)


if __name__ == "__main__":
    try:  # a console-less start must not open a window per child process (no_window.py)
        import no_window
        no_window.install()
    except ImportError:
        pass
    sys.exit(main())
