#!/usr/bin/env python3
"""permission_case.py — which case a relayed worker permission prompt is.

A worker must not sit blocked on a prompt to delete files it made itself.

Two cases, decided here so the 🔐 callback (permission_relay.py) tells CoS which
one applies:

  own-task-files  The only destructive part deletes or overwrites files this
                  task made: untracked files in its own worktree, or files in a
                  temp folder or under ~/Downloads, ~/Documents, or ~/Desktop
                  whose creation time is after the task started. Everything
                  else in the command is a known read-only command, `mkdir`, or
                  a copy into one of those places. CoS may answer allow without
                  the owner, logs it, and tells the owner in its next reply.
  ask             Everything else. CoS shows the owner the command and waits for their
                  allow or deny, as before.

Fail closed: command substitution, heredocs, subshells, `sudo`, `eval`,
`xargs`, an unknown command, an unresolved variable, a symlink target, a tracked
file, a file older than the task, or an unreadable creation time all give
`ask`.

A `cd` or a `NAME=value` changes what later commands touch only when bash runs it
in this shell. Bash skips one after a failed `&&` and runs one in a subshell in a
pipeline or behind `&` (`false && cd /tmp/x; rm -rf *` and `cd /tmp/x | true;
rm -rf *` both delete in the first folder). So a `cd` or an assignment counts only
at the start of the command or after `;` or a newline, followed by `;`, `&&`, a
newline, or the end, with no `||` or `&` later in its `&&` chain. Anything else
gives `ask`.

The check reads the file system as it was before the command ran, so a `cd` after
any command that changes files gives `ask` (`rm -rf /tmp/x/d; cd /tmp/x/d; rm -rf *`
leaves bash in the first folder). Quoted or backslashed shell punctuation, a quoted
newline, a `$` bash keeps literal, a carriage return, a `[...]` glob (bash's `[^x]`
and `[[:alpha:]]` are not Python's), `printf -v`, `find -follow`, and a cp or mv
source ending in `/` (macOS cp copies the folder's contents) all give `ask` too.
`NAME --help` is skipped only for a program the task did not make. So do a
quoted or escaped `NAME=`
(no assignment for bash), a glob in `cd`, `tee` or `mkdir`, a number apart from
its redirect, `-h` as help, a missing mv source, a recursive cp of a link, a
quoted `~`, and an overwrite or chmod of a file with another hard link.

Windows (Git Bash): a Git Bash path (`/tmp/x`, `/c/Users/...`)
is translated to its Windows path before it is resolved, because Windows Python
reads `/tmp/x` as `C:\\tmp\\x`. A backslash path such as `C:\\Users\\x` gives `ask`,
because the POSIX tokenizer drops its backslashes. `~/Dropbox/Downloads` is a
synced Downloads folder, where a delete reaches every device, so nothing under it
is ever own task files: it always gives `ask` (AGENTS.md: never auto-sweep Downloads).

  permission_case.py classify --task-id ID --command TEXT [--tool Bash] [--cwd DIR]
      Prints the verdict JSON for a live ledger row (a manual check).

Tests: test_permission_relay.py (PermissionCaseTests).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

OWN = "own-task-files"
ASK = "ask"

# Commands that read only, as long as none of their write or run flags appears (READ_ONLY_REFUSED_FLAGS).
# rg (--pre runs a program), tree (-o writes), sips (--out writes), uniq (a second operand is its output
# file) and ImageMagick identify (an ephemeral: input is deleted) are left out: each can write or run something with an ordinary-looking argument.
READ_ONLY = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "wc", "echo", "printf", "pwd", "cd", "test", "[",
    "true", "false", ":", "stat", "file", "which", "type", "date", "basename", "dirname", "realpath",
    "readlink", "du", "df", "diff", "cmp", "shasum", "md5", "md5sum", "sha256sum", "jq", "sleep",
    "mdls",
}
# Flags that make an otherwise read-only command write, run, or change something; any of them gives ask.
READ_ONLY_REFUSED_FLAGS = {
    "date": ("-s", "--set"),
    "file": ("-C", "--compile"),
    "diff": ("--to-file", "--from-file"),
}
# Flags refused even when clustered or joined to a value (`printf -vD` sets D like `printf -v D`).
READ_ONLY_REFUSED_PREFIXES = {
    "printf": ("-v",),
}
GIT_READ = {"status", "log", "diff", "show", "rev-parse", "ls-files", "branch"}
# git arguments that write a file, run a program, or change a ref, even under a read subcommand.
GIT_REFUSED_ARGS = ("--output", "--ext-diff", "--textconv", "--exec", "-c", "--config", "-o")
# `git branch` lists only with these; anything else (-d, -D, -m, -M, -f, a new name) changes refs.
GIT_BRANCH_LIST_ARGS = {"--list", "-l", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
                        "--show-current", "--no-color", "--color"}
# Environment names that change what a later command runs or loads.
# GLOBIGNORE turns on dotglob, so `*` would match `.git` where this check's glob does not; OLDPWD and PWD
# feed `cd -` and the shell's own folder.
DANGEROUS_ENV = re.compile(r"^(?:PATH|HOME|IFS|ENV|BASH_ENV|CDPATH|NODE_OPTIONS|PERL5OPT|RUBYOPT|PYTHON\w*|LD_\w+|DYLD_\w+|GIT_\w+"
                           r"|GLOBIGNORE|BASHOPTS|SHELLOPTS|OLDPWD|PWD)$")
DELETE = {"rm", "unlink", "rmdir", "trash"}
# A bare `help` is an operand to most commands (`rm help` deletes ./help), so only flags count as help.
# `-h` is not help everywhere (Windows shutdown -h hibernates).
HELP_ARGS = {"--help", "--version"}
REFUSE = {"sudo", "eval", "exec", "xargs", "env", "bash", "sh", "zsh", "source", ".", "osascript", "nohup", "doas"}
SEPARATORS = {";", "&&", "||", "|", "&", "|&"}
# `>&` writes a file unless its target is a descriptor (2>&1) or `-`.
WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>", ">&"}
OTHER_REDIRECTS = {"<", "<&", "<<<"}
USER_ROOTS = ("Downloads", "Documents", "Desktop")
WALK_LIMIT = 5000
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


class _Refuse(Exception):
    """The command needs the owner; the message is the reason."""


def _verdict(case: str, reason: str, targets: list[str] | None = None) -> dict[str, Any]:
    return {"case": case, "reason": reason, "targets": sorted(set(targets or []))}


def parse_started(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


_GIT_BASH_DRIVE = re.compile(r"^/([A-Za-z])(?=/|$)")


def native_path(raw: str) -> str:
    """A Git Bash path in its Windows form: `/c/x` is `C:/x`, `/tmp/x` is under the temp folder.
    Anything else, and every path off Windows, is returned unchanged."""
    if os.name != "nt" or not raw.startswith("/") or raw.startswith("//"):
        return raw
    drive = _GIT_BASH_DRIVE.match(raw)
    if drive:
        return f"{drive.group(1).upper()}:/" + raw[drive.end():].lstrip("/")
    if raw == "/tmp" or raw.startswith("/tmp/"):
        return tempfile.gettempdir().replace("\\", "/") + raw[len("/tmp"):]
    return raw   # /usr, /etc and the like: C:\usr is outside every task place, so it stays ask


def _is_link(path: str) -> bool:
    """A symlink, a Windows junction, or any other reparse point."""
    if os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
        return True
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _walk(path: str) -> list[str]:
    """The path and, for a real folder, everything under it, up to WALK_LIMIT + 1 entries."""
    entries = [path]
    if os.path.isdir(path) and not _is_link(path):
        for root, dirs, files in os.walk(path):
            entries += [os.path.join(root, n) for n in dirs + files]
            if len(entries) > WALK_LIMIT:
                break
    return entries


def _hard_linked(path: str) -> str:
    """The first file at or under path that has another hard link, or ''. Writing it changes the other name
    too, wherever that sits. Too many entries to check counts as linked."""
    entries = _walk(path)
    if len(entries) > WALK_LIMIT:
        return path
    for entry in entries:
        try:
            st = os.lstat(entry)
        except OSError:
            continue
        if os.path.isfile(entry) and not _is_link(entry) and st.st_nlink > 1:
            return entry
    return ""


def _holds_link(path: str) -> str:
    """The first link or junction at or under path, or ''."""
    entries = _walk(path)
    if len(entries) > WALK_LIMIT:
        return path
    return next((e for e in entries if _is_link(e)), "")


def _norm(path: str) -> str:
    """Case-folded on a case-insensitive file system (Windows), unchanged elsewhere."""
    return os.path.normcase(path)


def _birth(st: os.stat_result) -> float | None:
    born = getattr(st, "st_birthtime", None)
    if born is None and os.name == "nt":
        born = st.st_ctime   # Windows: st_ctime is the creation time
    return born


class Checker:
    def __init__(self, *, worktree: str, started_at: float, cwd: str = "", home: str = "",
                 temp_roots: list[str] | None = None):
        self.worktree = os.path.realpath(worktree) if worktree else ""
        self.started = started_at
        self.home = os.path.realpath(home or str(Path.home()))
        roots = temp_roots if temp_roots is not None else [
            "/tmp", "/private/tmp", "/private/var/folders", tempfile.gettempdir(), os.environ.get("TMPDIR", "")]
        self.temp_roots = sorted({os.path.realpath(r) for r in roots if r})
        self.user_roots = [os.path.join(self.home, name) for name in USER_ROOTS]
        # A synced Downloads folder: never auto-swept, and a delete
        # there reaches every device, so it is never a task place (see the module docstring).
        self.never_roots = [os.path.join(self.home, "Dropbox", "Downloads")]
        self.cwd = native_path(cwd) or self.worktree or self.home
        self.cwd_known = bool(cwd)   # no cwd from the transcript: the shell may sit elsewhere

    # --- paths -------------------------------------------------------------------------

    def resolve(self, raw: str) -> str:
        """Absolute path with every parent's symlinks resolved; the last part is kept
        as written, because rm deletes a link, not what it points to."""
        if raw.startswith("~") and not (raw == "~" or raw.startswith("~/")):
            raise _Refuse(f"{raw} is a ~ form bash reads differently")   # ~+, ~-, ~user
        if os.name == "nt" and raw.startswith("/") and native_path(raw) == raw:
            raise _Refuse(f"{raw} is a Git Bash path this check cannot map")   # /usr is Git's own folder
        path = os.path.expanduser(native_path(raw))
        # `..` after a symlinked folder would be collapsed here before the link is resolved, so the
        # check and the shell would disagree on the target.
        if ".." in path.replace("\\", "/").split("/"):
            raise _Refuse(f"{raw} goes up a folder with ..")
        if not os.path.isabs(path):
            if not self.cwd_known:
                raise _Refuse(f"{raw} is relative and the shell's folder is unknown")
            path = os.path.join(self.cwd, path)
        path = os.path.normpath(path)
        parent, name = os.path.split(path)
        # Windows opens TRACKE~1.MD and `tracked.md.` as tracked.md, while git and this check read the name
        # as written. Parents are safe: realpath turns them into their long names.
        if os.name == "nt" and (re.search(r"~\d", name) or name.endswith((".", " "))):
            raise _Refuse(f"{raw} may be a Windows short or trimmed name for another file")
        return os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(path)

    def _under(self, path: str, root: str) -> bool:
        path, root = _norm(path), _norm(root)
        return bool(root) and (path == root or path.startswith(root.rstrip(os.sep) + os.sep))

    def place(self, path: str) -> str:
        """worktree, temp, user, or '' for a path outside the task's places."""
        if any(self._under(path, root) for root in self.never_roots):
            return ""
        if self.worktree and self._under(path, self.worktree):
            rel = os.path.relpath(path, self.worktree)
            return "" if rel == "." or rel.split(os.sep)[0].lower() == ".git" else "worktree"
        if any(self._under(path, root) and _norm(path) != _norm(root) for root in self.temp_roots):
            return "temp"
        for root in self.user_roots:
            if self._under(path, root) and _norm(path) != _norm(root):
                return "user"
        return ""

    def _tracked(self, path: str) -> bool:
        rel = os.path.relpath(path, self.worktree).replace(os.sep, "/")
        try:
            out = subprocess.run(["git", "-C", self.worktree, "ls-files", "--", f":(icase){rel}"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return True
        return out.returncode != 0 or bool(out.stdout.strip())

    def _all_new(self, path: str) -> str:
        """'' when the path and everything under it was created after the task started."""
        entries = [path]
        if os.path.isdir(path) and not _is_link(path):
            for root, dirs, files in os.walk(path):
                entries += [os.path.join(root, n) for n in dirs + files]
                if len(entries) > WALK_LIMIT:
                    return f"{path} holds more than {WALK_LIMIT} entries"
        for entry in entries:
            if entry != path and _is_link(entry):
                return f"{entry} is a link or junction"
            try:
                born = _birth(os.lstat(entry))
            except OSError:
                continue
            if born is None:
                return f"no creation time for {entry}"
            if born < self.started:
                return f"{entry} existed before the task started"
        return ""

    def own_existing(self, raw: str, *, writes: bool = False) -> tuple[str, str]:
        """(path, '') when an existing path is this task's to delete or overwrite, else (path, reason).
        `writes` is an overwrite or a mode change, which reaches every hard link to the same file."""
        path = self.resolve(raw)
        if not os.path.lexists(path):   # `mv gone x` after a cp made it: the check cannot see what bash will
            return path, f"{path} does not exist yet, so the check cannot say what bash will touch"
        if _is_link(path):
            return path, f"{path} is a symlink or junction"
        where = self.place(path)
        if not where:
            return path, f"{path} is outside the task's worktree, temp folders, and Downloads/Documents/Desktop"
        if writes:
            linked = _hard_linked(path)
            if linked:
                return path, f"{linked} has another hard link, which may sit outside the task's places"
        if where == "worktree":
            return path, (f"{path} is tracked in the worktree" if self._tracked(path) else "")
        return path, self._all_new(path)

    def own_place(self, raw: str) -> tuple[str, str]:
        """For a write target: an existing one must be own; a new one must sit in a task place."""
        path = self.resolve(raw)
        if os.path.lexists(path):
            return self.own_existing(raw, writes=True)
        return path, ("" if self.place(path) else
                      f"{path} is outside the task's worktree, temp folders, and Downloads/Documents/Desktop")


# --- the command ----------------------------------------------------------------------------

def _expand(token: str, env: dict[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in env:
            raise _Refuse(f"${name} is not set in the command")
        return env[name]
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", sub, token)


def _paths(token: str, checker: Checker) -> list[str]:
    """Glob-expanded targets; a pattern with no match deletes nothing."""
    if "[" in token:   # bash reads [^x] and [[:alpha:]] where Python's glob does not
        raise _Refuse(f"`{token}` uses a [...] pattern bash and this check read differently")
    if any(ch in token for ch in "*?"):
        pattern = os.path.expanduser(native_path(token))
        if not os.path.isabs(pattern):
            pattern = os.path.join(checker.cwd, pattern)
        return sorted(glob.glob(pattern))
    return [token]


def _existing_targets(raw: str, checker: Checker) -> list[str]:
    """What a delete, chmod or touch operand names, every one of which must exist. A literal path that does
    not exist gives ask, because the check may read it differently from bash (Git Bash's /usr, a value
    bash splits); a glob with no match names nothing. A glob whose literal text is also a file counts that
    file too, because bash takes a quoted `a[b]` literally."""
    if not any(ch in raw for ch in "*?["):
        if not os.path.lexists(checker.resolve(raw)):
            raise _Refuse(f"{raw} does not exist, so the check cannot say what bash will touch")
        return [raw]
    found = list(_paths(raw, checker))
    if os.path.lexists(checker.resolve(raw)):
        found.append(raw)
    return found


# Characters that are shell syntax when bare. shlex drops the quotes around them, so a quoted `;` or `<`
# would read as a separator or a redirect while bash passes it to the command as an argument
# (`rm -f a ';' ls tracked.md` deletes tracked.md).
_SHELL_SPECIAL = set(";&|<>()\n")


def _check_quoting(command: str) -> None:
    """Ask when shell punctuation or a newline is quoted or backslashed, or a `$` is one bash keeps literal."""
    quote, i = "", 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            elif ch in _SHELL_SPECIAL or ch in "$~":
                raise _Refuse(f"the command quotes {ch!r} inside '...', which this check cannot read")
        elif ch == "\\" and i + 1 < len(command):
            nxt = command[i + 1]
            if nxt in _SHELL_SPECIAL or nxt in "$~":
                raise _Refuse(f"the command escapes {nxt!r} with a backslash, which this check cannot read")
            i += 2
            continue
        elif quote == '"':
            if ch == '"':
                quote = ""
            elif ch in _SHELL_SPECIAL or ch == "~":   # bash keeps a quoted ~ literal; the check expands it
                raise _Refuse(f"the command quotes {ch!r} inside \"...\", which this check cannot read")
        elif ch in "'\"":
            quote = ch
        i += 1


def _chain(command: str) -> list[tuple[str, list[str], str]]:
    """Each simple command as (separator before, tokens, separator after); '' is the start or the end.
    A run of separators collapses to its first one when the rest are `;` (a newline after `&&`, `||` or `|`
    continues that list in bash, it does not end it); any other run gives ask."""
    if os.name == "nt" and re.search(r"[A-Za-z]:\\|\\\\", command):
        raise _Refuse("the command uses a Windows backslash path, which the POSIX parser cannot read")
    # Braces (other than ${NAME}) expand to paths this check never sees, and $'...' or $"..." to text it
    # cannot read; both give ask.
    if re.search(r"[{}]", re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "", command)):
        raise _Refuse("the command uses brace expansion")
    if "$'" in command or '$"' in command:
        raise _Refuse("the command uses ANSI-C or locale quoting")
    # shlex drops a `#` comment to the end of the joined command, which would hide every later line that
    # bash still runs; a # anywhere gives ask.
    if "#" in command:
        raise _Refuse("the command holds a # this check cannot place")
    if re.search(r"\\\r?\n", command):   # bash joins a continued line; this check would split it
        raise _Refuse("the command continues a line with a backslash")
    if "\r" in command:   # shlex reads a carriage return as a space; macOS bash keeps it in the word
        raise _Refuse("the command holds a carriage return")
    _check_quoting(command)
    # A quoted or escaped name is no assignment for bash (`"D"=/tmp/x; rm -rf $D/*` runs `rm -rf /*`), and a
    # number apart from its redirect, or quoted, is an operand (`rm -f a 2 >x` deletes ./2). shlex hides both.
    if re.search(r"(?:^|[\s;&|])[A-Za-z0-9_\"'\\]*[\"'\\][A-Za-z0-9_\"'\\]*=", command):
        raise _Refuse("a quoted or escaped NAME= is not an assignment for bash")
    if (re.search(r"(?:^|[\s;|])\d+[ \t]+(?:[<>]|&>)", command)   # not after & or >: `2>&1 >x` is fine
            or re.search(r"(?:[\"']\d+[\"']|\\\d+)\s*[<>&]", command) or re.search(r"\d&>", command)):
        raise _Refuse("a number next to a redirect that bash may read as a word")
    for bad, why in (("$(", "command substitution"), ("`", "command substitution"), ("<(", "process substitution"),
                     (">(", "process substitution"), ("<<", "a heredoc")):
        if bad in command and not (bad == "<<" and "<<<" in command and command.count("<<") == command.count("<<<")):
            raise _Refuse(f"the command uses {why}")
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""   # belt and braces: a # was refused above
    try:
        tokens = list(lexer)
    except ValueError as error:
        raise _Refuse(f"the command does not parse ({error})") from error
    if any(t in {"(", ")", "((", "))"} or t.startswith("(") for t in tokens):
        raise _Refuse("the command uses a subshell")
    known = SEPARATORS | WRITE_REDIRECTS | OTHER_REDIRECTS | {"<>"}
    for t in tokens:
        if re.fullmatch(r"[;&|<>()]+", t) and t not in known:
            raise _Refuse(f"`{t}` is shell punctuation this check cannot read")
    segs: list[list[str]] = []
    seps: list[str] = []     # seps[k] sits before segs[k]
    run: list[str] = []
    current: list[str] = []
    for token in tokens + [";"]:
        if token in SEPARATORS:
            if current:
                segs.append(current)
                current = []
            run.append(token)
            continue
        if not current:
            seps.append(_collapse(run, first=not segs))
            run = []
        current.append(token)
    if not segs:
        return []
    after = seps[1:] + [_collapse(run[:-1], first=False) if run[:-1] else ""]
    return [(seps[k] if k else "", segs[k], after[k]) for k in range(len(segs))]


def _collapse(run: list[str], *, first: bool) -> str:
    if not run:
        return ""
    if any(t != ";" for t in run[1:]) or (first and run[0] != ";"):
        raise _Refuse(f"`{' '.join(run)}` joins commands in a way this check cannot read")
    return run[0]


def _segments(command: str) -> list[list[str]]:
    return [seg for _, seg, _ in _chain(command)]


def _state_change_runs(chain: list[tuple[str, list[str], str]], k: int, what: str) -> None:
    """A `cd` or an assignment at chain[k] must run, and run in this shell, before a later command reads it."""
    before, _, after = chain[k]
    if before not in {"", ";"}:
        raise _Refuse(f"{what} follows `{before}`, so bash may skip it or run it in a subshell")
    if after not in {"", ";", "&&"}:
        raise _Refuse(f"{what} is joined by `{after}`, so bash may run it in a subshell or the next command may run without it")
    # The rest of its list, up to the next `;`: an `||` runs the next command when it failed, and an `&`
    # runs the whole list, this command too, in a background subshell (`cd x && ls | wc &`).
    for _, _, sep in chain[k:]:
        if sep in {"", ";"}:
            return
        if sep in {"||", "&"}:
            raise _Refuse(f"{what} sits in a list that goes on to `{sep}`, so the next command may run without it")


# Flags each file command may carry for the own-task-files case. Any other
# flag gives ask, because _operands drops flags and a flag with a value (cp -t DIR, cp -T, touch -r REF,
# chmod --reference) would leave the real target unchecked; cp -l/-s link to an outside file and rmdir -p
# removes parents. Short flags are listed as letters and may be clustered (-rf).
FILE_FLAGS: dict[str, tuple[str, set[str]]] = {
    "rm": ("rRfvdI", {"--recursive", "--force", "--verbose", "--dir"}),
    "unlink": ("", set()),
    "rmdir": ("v", {"--verbose"}),
    "trash": ("rRfv", set()),
    "mkdir": ("pv", {"--parents", "--verbose"}),
    "cp": ("fprRvnaiL", {"--force", "--recursive", "--verbose", "--no-clobber", "--archive", "--preserve"}),
    "mv": ("fvni", {"--force", "--verbose", "--no-clobber"}),
    "chmod": ("Rv", {"--recursive", "--verbose"}),
    "touch": ("c", {"--no-create"}),
    "tee": ("a", {"--append"}),
}
CHMOD_MODE = re.compile(r"^(?:[0-7]{3,4}|[ugoa]*[-+=][rwxXst]+(?:,[ugoa]*[-+=][rwxXst]+)*)$")


def _check_flags(name: str, args: list[str]) -> None:
    letters, longs = FILE_FLAGS.get(name, ("", set()))
    for arg in args:
        if arg == "--":
            return
        if arg == "-" or not arg.startswith("-"):
            continue
        if arg.startswith("--"):
            if arg not in longs:
                raise _Refuse(f"`{name} {arg}` is not a flag the own-task-files case reads")
        elif any(ch not in letters for ch in arg[1:]):
            raise _Refuse(f"`{name} {arg}` is not a flag the own-task-files case reads")


def _operands(args: list[str]) -> list[str]:
    out, dashes = [], False
    for arg in args:
        if not dashes and arg == "--":
            dashes = True
        elif dashes or not arg.startswith("-") or arg == "-":
            out.append(arg)
    return out


def _help_is_safe(name: str, checker: Checker) -> None:
    """`NAME --help` still runs NAME. Ask when any program bash could find under that name sits in a task
    place or is newer than the task, because the task may have written it.
    A name found nowhere only prints "command not found"."""
    exts = [""] + ([e for e in os.environ.get("PATHEXT", "").split(os.pathsep) if e] if os.name == "nt" else [])
    # The worker's bash may add ~/bin and ~/.local/bin to the PATH this process sees.
    extra = [os.path.join(checker.home, "bin"), os.path.join(checker.home, ".local", "bin")]
    for folder in os.environ.get("PATH", "").split(os.pathsep) + extra:
        if not folder:
            continue
        folder = native_path(folder)
        if not os.path.isabs(folder):
            raise _Refuse(f"`{name} --help`: PATH holds the relative folder {folder}")
        for ext in exts:
            path = os.path.join(folder, name + ext)
            if not os.path.isfile(path):
                continue
            real = os.path.realpath(path)
            try:
                born = _birth(os.stat(real))
            except OSError:
                born = None
            if checker.place(real) or born is None or born >= checker.started:
                raise _Refuse(f"`{name} --help` runs {real}, which this task may have made")


def classify_bash(command: str, checker: Checker) -> dict[str, Any]:
    env = {"HOME": checker.home, "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir())}
    targets: list[str] = []
    destructive = False

    def need_own(raw: str, writes: bool = False) -> None:
        path, why = checker.own_existing(raw, writes=writes)
        if why:
            raise _Refuse(why)
        targets.append(path)

    def need_place(raw: str) -> bool:
        """True when the write overwrites an existing own file."""
        path, why = checker.own_place(raw)
        if why:
            raise _Refuse(why)
        if os.path.lexists(path):
            targets.append(path)
            return True
        return False

    mutated = False   # an earlier command changed files, so the check's view of the folders may be stale

    try:
        chain = _chain(command)
        for k, (_, seg, _) in enumerate(chain):
            words: list[str] = []
            i = 0
            while i < len(seg):
                token = seg[i]
                # A descriptor number; _chain refused a number bash would read as a word (`2 >x`, `"2">x`).
                if token.isdigit() and i + 1 < len(seg) and seg[i + 1] in (WRITE_REDIRECTS | OTHER_REDIRECTS) - {"&>", "&>>"}:
                    i += 1
                    continue
                if token == "<>":
                    raise _Refuse("`<>` opens a file for writing")
                if token in WRITE_REDIRECTS | OTHER_REDIRECTS:
                    target = _expand(seg[i + 1], env) if i + 1 < len(seg) else ""
                    if "$" in target:
                        raise _Refuse(f"`{target}` holds a shell variable this check cannot resolve")
                    descriptor = token in {">&", "<&"} and (target.isdigit() or target == "-")
                    if token in WRITE_REDIRECTS and target not in {"/dev/null", ""} and not descriptor:
                        mutated = True
                        for path in _paths(target, checker) or [target]:
                            destructive |= need_place(path)
                    i += 2
                    continue
                words.append(token)
                i += 1
            assigned = []
            while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
                name, value = words.pop(0).split("=", 1)
                if DANGEROUS_ENV.match(name):
                    raise _Refuse(f"`{name}=` changes what a command runs")
                if re.search(r"[\s*?\[]", value):
                    raise _Refuse(f"`{name}` holds spaces or glob characters bash would split or expand")
                assigned.append(name)
                env[name] = os.path.expanduser(_expand(value, env))
            if not words:
                if assigned:
                    _state_change_runs(chain, k, f"`{assigned[0]}=`")
                continue
            if assigned:   # VAR=x cmd: the value reaches the command's environment, not just this check
                raise _Refuse(f"`{assigned[0]}=` is set for the command itself")
            words = [_expand(w, env) for w in words]
            if any("$" in w for w in words):   # $1, $@, $$ and the like expand to what this check never sees
                raise _Refuse("the command holds a shell variable this check cannot resolve")
            command = words[0][1:] if words[0].startswith("\\") else words[0]   # `\rm` skips an alias only
            if "/" in command or "\\" in command:
                raise _Refuse(f"`{command}` is a path, not a known command")
            name, args = command, words[1:]
            if name in REFUSE:
                raise _Refuse(f"`{name}` needs the owner")
            if name == "cd":   # before the help shortcut: `cd help` goes into ./help
                _state_change_runs(chain, k, "`cd`")
                if mutated:   # `rm -rf d; cd d; rm -rf *`: the folder the check sees may be gone for bash
                    raise _Refuse("`cd` comes after a command that changes files, so the folder may not be there")
                if len(args) > 1 or (args and args[0].startswith("-")):
                    raise _Refuse(f"`cd {' '.join(args)}` is not a plain cd to one folder")
                if args and any(ch in args[0] for ch in "*?["):   # bash expands it; two matches leave it put
                    raise _Refuse(f"`cd {args[0]}` is a pattern bash expands")
                if args and not (os.path.isabs(native_path(args[0])) or args[0] in {".", "~"}
                                 or args[0].startswith(("./", "~/"))):
                    raise _Refuse(f"`cd {args[0]}` is relative, and bash may look it up in CDPATH")
                target = os.path.realpath(checker.resolve(args[0])) if args else checker.home
                if not os.path.isdir(target) or not os.access(target, os.X_OK):
                    raise _Refuse(f"`cd {args[0] if args else ''}` goes to no folder, so the shell stays put")
                checker.cwd, checker.cwd_known = target, True
                continue
            if args and all(a in HELP_ARGS for a in args):
                _help_is_safe(name, checker)
                continue
            if name in READ_ONLY:
                bad = [a for a in args for f in READ_ONLY_REFUSED_FLAGS.get(name, ()) if a == f or a.startswith(f + "=")]
                bad += [a for a in args for f in READ_ONLY_REFUSED_PREFIXES.get(name, ()) if a.startswith(f)]
                if bad:
                    raise _Refuse(f"`{name} {bad[0]}` writes or changes something")
                continue
            mutated = True   # every command below may change files
            if name == "git":
                if not args or args[0] not in GIT_READ:
                    raise _Refuse("`git` that is not a plain read")
                bad = [a for a in args[1:] if any(a == f or a.startswith(f + "=") for f in GIT_REFUSED_ARGS)]
                if bad:
                    raise _Refuse(f"`git {args[0]} {bad[0]}` writes a file or runs a program")
                if args[0] == "branch" and any(a not in GIT_BRANCH_LIST_ARGS for a in args[1:]):
                    raise _Refuse("`git branch` that does more than list")
                continue
            if name == "find":
                if args and args[0].startswith("-"):   # -H, -L, -P, -D, -O come before the roots
                    raise _Refuse("`find` with options before its starting folder")
                if {"-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"} & set(args):
                    raise _Refuse("`find` runs or writes something")
                if "-follow" in args:   # with -delete it reaches through links out of the task's places
                    raise _Refuse("`find -follow` follows links")
                if "-delete" in args:
                    roots = [a for a in args[: next((k for k, a in enumerate(args) if a.startswith(("-", "!", "("))), len(args))]]
                    for root in roots or ["."]:
                        for path in _paths(root, checker):
                            need_own(path)
                    destructive = True
                continue
            if name == "mkdir":
                _check_flags(name, args)
                for raw in _operands(args):
                    if any(ch in raw for ch in "*?["):
                        raise _Refuse(f"`mkdir {raw}` is a pattern bash expands")
                    if not checker.place(checker.resolve(raw)):
                        raise _Refuse(f"mkdir {raw} is outside the task's places")
                continue
            if name in DELETE:
                _check_flags(name, args)
                for raw in _operands(args):
                    for path in _existing_targets(raw, checker):
                        need_own(path)
                        destructive = True
                continue
            if name in {"chmod", "touch"}:
                _check_flags(name, args)
                if name == "chmod" and not (_operands(args) and CHMOD_MODE.match(_operands(args)[0])):
                    raise _Refuse("`chmod` without a plain mode")
                ops = _operands(args)[1:] if name == "chmod" else _operands(args)
                for raw in ops:
                    if name == "touch" and not any(ch in raw for ch in "*?[") and not os.path.lexists(checker.resolve(raw)):
                        need_place(raw)   # touch creates it: a new file must sit in a task place
                        continue
                    for path in _existing_targets(raw, checker):
                        need_own(path, writes=True)   # a mode change reaches every hard link
                continue
            if name in {"cp", "mv"}:
                _check_flags(name, args)
                ops = _operands(args)
                if len(ops) < 2 or any(ch in ops[-1] for ch in "*?["):
                    raise _Refuse(f"`{name}` with no clear destination")
                # `cp -R src/ dest` copies src's contents into dest on macOS, and `src/.` does everywhere.
                for raw in ops[:-1]:
                    if raw.endswith("/") or os.path.basename(raw) in {".", ".."}:
                        raise _Refuse(f"`{name} {raw}` ends in a slash or a dot, so what lands in the destination differs by system")
                # A source pattern with no match copies or moves nothing.
                sources, dest = [p for raw in ops[:-1] for p in _paths(raw, checker)], ops[-1]
                # macOS cp -R and any cp -a copy a link as a link, so a later write through the copy leaves
                # the task's places.
                recursive = any(a in {"--recursive", "--archive"} or (a.startswith("-") and not a.startswith("--")
                                and set(a[1:]) & set("rRa")) for a in args)
                if recursive:
                    for src in sources:
                        linked = _holds_link(checker.resolve(src)) if os.path.lexists(checker.resolve(src)) else ""
                        if linked:
                            raise _Refuse(f"`{name}` copies {linked}, a link or junction, which may stay a link")
                if name == "mv":
                    for src in sources:
                        need_own(src)
                    destructive = True
                dest_path = checker.resolve(dest)
                if os.path.isdir(dest_path) and not os.path.islink(dest_path):
                    if not checker.place(dest_path):
                        raise _Refuse(f"{dest_path} is outside the task's places")
                    for src in sources:
                        destructive |= need_place(os.path.join(dest_path, os.path.basename(src.rstrip("/"))))
                else:
                    destructive |= need_place(dest)
                continue
            if name == "tee":
                _check_flags(name, args)
                for raw in _operands(args):
                    if any(ch in raw for ch in "*?["):   # bash expands it onto existing files
                        raise _Refuse(f"`tee {raw}` is a pattern bash expands")
                    destructive |= need_place(raw)
                continue
            raise _Refuse(f"`{name}` is not a delete, an overwrite, or a known read-only command")
    except _Refuse as why:
        return _verdict(ASK, str(why))
    if not destructive:
        return _verdict(ASK, "it deletes or overwrites no task files, so the own-task-files case does not apply")
    return _verdict(OWN, "the only destructive part deletes or overwrites files this task made", targets)


def classify(tool: str, command: str, *, row: dict[str, Any], cwd: str = "", home: str = "",
             temp_roots: list[str] | None = None) -> dict[str, Any]:
    """The case for one relayed prompt. `row` is the task's ledger row (worktree, created_at)."""
    started = parse_started(row.get("created_at") or row.get("started_at"))
    if not row or started is None:
        return _verdict(ASK, "no task row with a start time, so no file can be shown to be the task's")
    if not command.strip():
        return _verdict(ASK, "the command is unknown")
    checker = Checker(worktree=str(row.get("worktree") or ""), started_at=started, cwd=cwd, home=home,
                      temp_roots=temp_roots)
    if tool in EDIT_TOOLS:
        try:
            data = json.loads(command)
        except ValueError:
            return _verdict(ASK, "the edit's input does not parse")
        raw = str(data.get("file_path") or data.get("notebook_path") or "")
        try:
            target = checker.resolve(raw) if raw else ""
        except _Refuse as why:
            return _verdict(ASK, str(why))
        if not raw or not os.path.lexists(target):
            return _verdict(ASK, "it creates a new file, so the own-task-files case does not apply")
        path, why = checker.own_existing(raw, writes=True)
        return _verdict(ASK, why) if why else _verdict(OWN, "it overwrites only a file this task made", [path])
    if tool not in {"Bash", ""}:
        return _verdict(ASK, f"a {tool} call is not a file delete or overwrite")
    return classify_bash(command, checker)


def summary(verdict: dict[str, Any], limit: int = 3) -> str:
    targets = list(verdict.get("targets") or [])
    home = str(Path.home())
    shown = [t.replace(home, "~", 1) for t in targets[:limit]]
    more = f" and {len(targets) - limit} more" if len(targets) > limit else ""
    return ", ".join(shown) + more


def main() -> int:
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import permission_relay
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify")
    c.add_argument("--task-id", required=True)
    c.add_argument("--command", required=True)
    c.add_argument("--tool", default="Bash")
    c.add_argument("--cwd", default="")
    args = parser.parse_args()
    row = next((r for r in permission_relay._ledger_rows() if r.get("id") == args.task_id), {})
    print(json.dumps(classify(args.tool, args.command, row=row, cwd=args.cwd), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
