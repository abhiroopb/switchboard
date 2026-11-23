#!/usr/bin/env python3
"""Who may send email through `switchboard mail send`, and when.

The owner can turn on direct Gmail sends for a machine. switchboard then sends
an email only after the owner says send for that exact text in the current conversation.
switchboard-shim.py asks this module before every send and sends nothing on any refusal. Every check
fails closed:

  flag      SWITCHBOARD_GMAIL_SEND=on in local/machines/<MACHINE>.env on main's checkout. Default
            off, so the kit and other machines stay draft-only. The process environment can
            only turn it off, and it cannot move the file.
  surface   An attended Claude Code session in the CoS or Launchpad tab. A worker session or
            checkout, a scheduled job, a headless run, or any other tab is refused.
  owner     The newest owner turn in this session's transcript says send, in the owner's own
            words, and nothing the owner queued after it is unread. Lines the mux typed into
            the pane (worker reports, questions, digests) look like human turns in the
            transcript, so they are excluded by shape and by the ledger of injected lines
            cmux-shim.py keeps. Pasted text, quotes and accepted suggestions do not count. So
            an email or web page an agent read cannot approve a send.
  shown     An assistant message since the owner's previous turn shows the draft's recipients,
            subject, body and link targets, so what the owner approved is what goes out.
  once      One send per owner turn. "Send it" never approves a second draft.

What this does not do: it is a gate on the sanctioned path, not a sandbox. It stops a worker,
a scheduled job, or an injected instruction that uses the tools as they are meant to be used.
An agent runs as the owner, so one that sets out to defeat it is outside it: typing into the CoS
pane with raw wmux, forging the session id and a transcript file, spoofing CMUX_WORKSPACE_ID in a
process with no pane, pointing SWITCHBOARD_STATE_HOME at an empty ledger, or writing its own HTTP call
with the owner's token. The Claude hook denies the obvious forms
(.claude/hooks/switchboard-guardrails.py).

  gmail_send_guard.py              print on or off
  gmail_send_guard.py --check      say whether this surface may send; sends nothing
  gmail_send_guard.py --set on     write the key to this machine's file
  gmail_send_guard.py --set off    remove the key

Tests: system/scripts/test_gmail_send_guard.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

KEY = "SWITCHBOARD_GMAIL_SEND"
ON_VALUES = {"on", "1", "true", "yes", "enabled"}
SEND_SURFACES = {"cos", "launchpad"}
OWNER_PROMPT_SOURCES = {"typed", "queued"}
INJECTED_WINDOW_SECONDS = 24 * 3600
UNKNOWN_SETTLE_SECONDS = 600
MAX_APPROVAL_CHARS = 1000  # a long turn is a paste with a stray "send" in it, not a ruling

_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{16,64}$")
_PASTED_RE = re.compile(r"<pasted_content\b.*?</pasted_content>", re.I | re.S)
_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.S)
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_LEAD = (r"(?:(?:\d+|[a-z])[.)]\s*)?"
         r"(?:(?:ok(?:ay)?(?: to)?|fine to|good to|you can|yes|yep|yeah|sure|approved|looks good|sounds good|lgtm|great|good|"
         r"please|pls|go ahead(?: and)?|then|and)\b[\s,\-–—]*)*")
# Only what may follow "send" in a ruling. "Send it to Sam instead" and "send nothing" are not one.
_SEND_RE = re.compile(rf"^{_LEAD}send(?:\s+(?:it|this|that|email|now|please|out|as\s+(?:is|written)|"
                      r"the\s+(?:email|mail|draft|message|reply|note)))*(?:[\s,]+(?:thanks|thank you))?\s*[.!]*$", re.I)
# A sentence that may sit beside the ruling. Anything else ("Change the subject. Send it.") asks for
# a change, so the unedited text must not go out.
_ACK_RE = re.compile(r"^(?:\d+[.)]|ok(?:ay)?|yes|yep|yeah|sure|approved|looks good|sounds good|lgtm|great|good|perfect|nice|"
                     r"thanks|thank you)[.!,]*$", re.I)
_HOLD_RE = re.compile(r"\b(?:don'?t|do not|never|not yet|hold|wait|instead|actually|but|unless|until|before|after|"
                      r"later|tomorrow|tonight)\b", re.I)


# ------------------------------------------------------------------------ paths
# Fixed on purpose: SWITCHBOARD_LOCAL_DIR, SWITCHBOARD_MACHINE and CLAUDE_CONFIG_DIR must not let one command
# turn the flag on or move the guard to another transcript folder. Tests patch these functions.

def main_root() -> Path:
    from local_backup import main_checkout  # reads the .git file, so no git process
    return main_checkout(REPO)


def machine_name() -> str:
    return socket.gethostname().split(".")[0].upper()


def claude_home() -> Path:
    return Path.home() / ".claude"


def env_file() -> Path:
    return main_root() / "local" / "machines" / f"{machine_name()}.env"


def log_path() -> Path:
    """Gitignored, on main's checkout: time, recipients, subject, ids, hashes. Never a body."""
    return main_root() / ".state" / "gmail-sends.jsonl"


# ---------------------------------------------------------------------- the flag

def file_value() -> str:
    try:
        lines = env_file().read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    value = ""
    for line in lines:
        key, sep, val = line.strip().partition("=")
        if sep and key.strip() == KEY:
            value = val.strip().strip("\"'")
    return value


def enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    if env.get(KEY, "").strip() and env[KEY].strip().lower() not in ON_VALUES:
        return False
    return file_value().lower() in ON_VALUES


def write_setting(state: str) -> Path:
    path = env_file()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    except UnicodeDecodeError:
        lines = []
    keep = [ln for ln in lines if ln.partition("=")[0].strip() != KEY]
    if state == "on":
        keep.append(f"{KEY}=on")
    if keep:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
    else:
        path.unlink(missing_ok=True)
    return path


# ------------------------------------------------------------------- the surface

def in_worktree(cwd: str) -> bool:
    """A linked git worktree has a .git file where the main checkout has a directory."""
    path = Path(cwd or ".").resolve()
    for folder in (path, *path.parents):
        dot_git = folder / ".git"
        if dot_git.exists():
            return dot_git.is_file()
    return False


def list_workspaces() -> list[dict[str, Any]]:
    from owner_tabs import cmux_workspaces
    return cmux_workspaces()


def surface_workspace_id() -> str:
    """wmux exports only WMUX_SURFACE_ID, so a CoS tab has no CMUX_WORKSPACE_ID: ask the mux."""
    lib = str(HERE / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from mux import current_workspace_id
    saved = os.environ.pop("CMUX_WORKSPACE_ID", None)  # the pane decides, not a variable a command can set
    try:
        return current_workspace_id()
    finally:
        if saved is not None:
            os.environ["CMUX_WORKSPACE_ID"] = saved


def own_workspace_id(env: Mapping[str, str]) -> str:
    if env.get("WMUX_SURFACE_ID"):
        return surface_workspace_id()
    # cmux (macOS) exports the workspace itself. No pane at all: Task Scheduler, launchd, a plain terminal.
    return str(env.get("CMUX_WORKSPACE_ID") or "").strip()


def tab_name(ws: Mapping[str, Any]) -> str:
    title = str(ws.get("custom_title") or ws.get("title") or "")
    return re.sub(r"^\W+", "", title).strip().casefold()


def surface_refusal(env: Mapping[str, str] | None = None, cwd: str | None = None) -> str | None:
    """Why this process may not send, or None. Touches no Google API."""
    env = os.environ if env is None else env
    cwd = os.getcwd() if cwd is None else cwd
    if not enabled(env):
        return f"sending is off on this machine ({KEY} is not on in {env_file().name}); this machine stays draft-only"
    if any(value and (key.startswith("SWITCHBOARD_WORKER") or key == "SWITCHBOARD_WORKTREE") for key, value in env.items()):
        return "this is a worker session; workers park the text as a Gmail draft and show it verbatim in their report"
    if in_worktree(cwd):
        return "this runs inside a task worktree; workers park the text as a Gmail draft"
    if env.get("CLAUDE_CODE_ENTRYPOINT") != "cli" or not env.get("CLAUDE_CODE_SESSION_ID"):
        return "no attended Claude Code session; a scheduled job or headless run never sends email"
    try:
        own_id = own_workspace_id(env)
    except Exception as error:
        return f"this pane's tab could not be resolved ({type(error).__name__}), so this surface is unproven"
    if not own_id:
        return "no interactive surface; email sends only from the CoS or Launchpad tab"
    try:
        tabs = list_workspaces()
    except Exception as error:  # the tab list decides, so an unreadable one refuses
        return f"the tab list is unreadable ({type(error).__name__}), so this surface is unproven"
    own = next((w for w in tabs if str(w.get("id") or "").upper() == own_id.upper()), None)
    if own is None:
        return "this tab is not in the tab list, so this surface is unproven"
    if str(own.get("description") or "").startswith("switchboard worker "):
        return "this tab is a worker tab; workers park the text as a Gmail draft"
    if tab_name(own) not in SEND_SURFACES:
        return (f"this tab ({tab_name(own) or 'untitled'}) {TAB_TITLE_REFUSAL}. If the owner runs CoS in this tab, "
                "register it with: bash system/scripts/cos-phase0.sh chrome (it refuses while another tab is CoS)")
    return None


# One refusal names every step the next send needs, so a "send it" is never refused
# several times in a row, each refusal naming only its own cause.
SAME_TURN_RETRY = ('If you fix that before the owner types anything else, retry now: their "send it" still counts. ')
NEXT_SEND_STEPS = ('Once the owner has typed anything since "send it", the next send needs both, in order: print the '
                   'draft again verbatim from show (To, Cc, Bcc, Subject, the body and any links) in one '
                   'message, then the owner says "send it" in a new message. The guard reads only the latest '
                   'owner message and needs the draft shown right before it.')


TAB_TITLE_REFUSAL = "is not CoS or Launchpad"
# Refusals a fresh show-then-send fixes. The rest (transcript or send log unreadable, a
# worktree conversation, a worker) are not about the owner's word, so the steps would mislead.
OWNER_WORD_REFUSALS = ("does not say send", "was not shown just before", "already went to a send",
                       "newer message waiting in the queue")


def next_send_steps(refusal: str) -> str:
    """What the next attempt needs, appended to a send refusal, or "" when
    nothing about the owner's word would help. Only the tab-title refusal gets the
    same-turn retry line: it comes before the owner's word is read and nothing is
    logged, so while the owner types nothing new their "send it" still counts.
    Sending off, a worker, or a worktree is never something to "fix and retry"."""
    if TAB_TITLE_REFUSAL in refusal:
        return SAME_TURN_RETRY + NEXT_SEND_STEPS
    if any(marker in refusal for marker in OWNER_WORD_REFUSALS):
        return NEXT_SEND_STEPS
    return ""


# --------------------------------------------------------- the owner's word, shown

def transcript_path(env: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if env is None else env
    session = str(env.get("CLAUDE_CODE_SESSION_ID") or "")
    if not _SESSION_ID_RE.match(session):
        return None
    matches = sorted((claude_home() / "projects").glob(f"*/{session}.jsonl"))
    return matches[0] if matches else None


def read_entries(path: Path) -> list[dict[str, Any]]:
    entries = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                entries.append(row)
    return entries


def injected_key(text: str) -> str:
    """The hash cmux-shim.py records for a line it typed; ` ⏎ ` and whitespace do not count."""
    return hashlib.sha256(re.sub(r"[\s⏎]+", "", text).encode("utf-8")).hexdigest()


def injected_times(env: Mapping[str, str] | None = None) -> dict[str, list[float]]:
    """Hash to times, for lines typed into this pane. A "send it" steer to a worker's pane says
    nothing about what the owner typed in CoS. An entry with no known pane counts everywhere."""
    env = os.environ if env is None else env
    own = env.get("WMUX_SURFACE_ID") or env.get("CMUX_SURFACE_ID") or ""
    path = Path(env.get("SWITCHBOARD_STATE_HOME") or Path.home() / ".switchboard") / "injected-prompts.jsonl"
    times: dict[str, list[float]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return times
    for line in lines:
        try:
            row = json.loads(line)
            if own and row.get("surface") and row["surface"] != own:
                continue
            times.setdefault(str(row["sha"]), []).append(float(row["at"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return times


def turn_text(entry: Mapping[str, Any]) -> str | None:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _turn_epoch(entry: Mapping[str, Any]) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_callback(text: str) -> bool:
    # Every callback the mux types opens with a bracket (`[ROUTER REPORT]`, `[WORKER QUESTION]`,
    # `[<emoji> <Name> · rt-…] done:`) and harness text opens with a tag. A spilled report's
    # pointer line says so in its tail.
    return text.lstrip()[:1] in ("[", "<") or "[full message, " in text


def is_owner_turn(entry: Mapping[str, Any], injected: Mapping[str, list[float]]) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    if (entry.get("origin") or {}).get("kind") != "human":
        return False
    if entry.get("promptSource") not in OWNER_PROMPT_SOURCES:  # an accepted suggestion is the model's words
        return False
    text = turn_text(entry)
    if not text or not text.strip() or _is_callback(text):
        return False
    when = _turn_epoch(entry)
    for at in injected.get(injected_key(text), []):
        if when is None or -60 <= when - at <= INJECTED_WINDOW_SECONDS:
            return False
    return True


def says_send(text: str) -> bool:
    """The owner's own imperative: "send it", "yes, send", "ok send the email", "2. send". A
    question, a condition, a change of recipient, or any other request in the same message is no
    ruling: a message that also says "change the subject" must not send the unchanged text."""
    if len(text) > MAX_APPROVAL_CHARS:
        return False
    own = _FENCE_RE.sub(" ", _PASTED_RE.sub(" ", text))
    own = "\n".join(line for line in own.splitlines() if not line.lstrip().startswith(">"))
    if _HOLD_RE.search(own):
        return False
    sentences = [s.strip() for s in _SENTENCE_RE.split(own) if s and s.strip()]
    if not any(_SEND_RE.match(s) for s in sentences):
        return False
    return all(_SEND_RE.match(s) or _ACK_RE.match(s) for s in sentences)


def _squeeze(text: str) -> str:
    return re.sub(r"[\s⏎]+", "", text)


def _assistant_blocks(entry: Mapping[str, Any]) -> list[str]:
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return []
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"]


def was_shown(entries: list[dict[str, Any]], shown: Mapping[str, Any]) -> bool:
    """One assistant message holds every recipient, the subject, the body in each form the
    recipient may read, and every link target. Whitespace does not count: a code block and a
    wrapped line are the same text."""
    items = [*shown.get("addresses", []), shown.get("subject", ""), shown.get("body", ""),
             shown.get("html_body", ""), *shown.get("links", [])]
    wanted = [_squeeze(str(item)).casefold() for item in items if item]
    for entry in entries:
        for block in _assistant_blocks(entry):
            squeezed = _squeeze(block).casefold()
            if all(item in squeezed for item in wanted):
                return True
    return False


def _queued_owner_message(entries: list[dict[str, Any]], since: int, approving: str) -> bool:
    """A message the owner typed while the agent was busy ("don't send") sits in the queue before it
    becomes a turn, and can be queued before the "send it" turn is written. So the whole file is
    walked, and whatever is still queued counts when it was queued since the owner's previous turn.
    A callback in the queue is not the owner's, and neither is the approving message itself."""
    pending: list[tuple[int, str]] = []
    for index, entry in enumerate(entries):
        if entry.get("type") != "queue-operation":
            continue
        text = str(entry.get("content") or "")
        if entry.get("operation") == "enqueue":
            pending.append((index, text))
        elif entry.get("operation") in ("dequeue", "remove") and pending:
            match = next((i for i, (_, queued) in enumerate(pending) if text and queued == text), 0)
            pending.pop(match)
    return any(index >= since and not _is_callback(text) and _squeeze(text) != _squeeze(approving) for index, text in pending)


def turn_used(turn: str) -> bool:
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    return any(turn in line and _row(line).get("turn") == turn for line in lines)


def _row(line: str) -> dict[str, Any]:
    try:
        row = json.loads(line)
    except ValueError:
        return {}
    return row if isinstance(row, dict) else {}


def approval(shown: Mapping[str, Any], env: Mapping[str, str] | None = None) -> tuple[str | None, str]:
    """(why the owner has not approved this exact text, or None; the approving turn's id)."""
    env = os.environ if env is None else env
    path = transcript_path(env)
    if path is None:
        return "this session's transcript was not found, so the owner's word is unproven", ""
    try:
        entries = read_entries(path)
        injected = injected_times(env)
    except OSError as error:
        return f"the transcript or the injected-line ledger is unreadable ({type(error).__name__})", ""
    owner = [i for i, entry in enumerate(entries) if is_owner_turn(entry, injected)]
    if not owner:
        return "no owner turn in this conversation", ""
    last = owner[-1]
    turn = str(entries[last].get("uuid") or "")
    if not says_send(turn_text(entries[last]) or ""):
        return ('the owner\'s latest message does not say send. Show the draft, then wait for the owner to say '
                '"send it" in their own message'), ""
    if not turn:
        return "the owner's turn has no id, so one send per turn is unproven", ""
    cwd = str(entries[last].get("cwd") or "")
    if cwd and in_worktree(cwd):
        return "this conversation runs in a task worktree; workers park the text as a Gmail draft", ""
    since = owner[-2] + 1 if len(owner) > 1 else 0
    if _queued_owner_message(entries, since, turn_text(entries[last]) or ""):
        return "the owner has a newer message waiting in the queue. Read it before sending", ""
    if not was_shown(entries[since:last], shown):
        return ("this exact text was not shown just before the owner said send. Print To, Cc, Bcc, Subject, the body "
                "and any links verbatim from show in one message, then wait for the owner to say send"), ""
    try:
        if turn_used(turn):
            return ('that "send" already went to a send. Each email needs its own word from the owner: show this draft '
                    'and wait for them to say "send it"'), ""
    except OSError as error:
        return f"the send log is unreadable ({type(error).__name__}), so one send per turn is unproven", ""
    return None, turn


# ------------------------------------------------------------------- the send log

def log_send(row: Mapping[str, Any]) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"at": stamp, **row}, ensure_ascii=False) + "\n")


def unresolved_attempt(draft_id: str, send_check: str = "") -> dict[str, Any] | None:
    """The newest row for this draft or this exact text when it has no known outcome: a process
    died mid-send or the send timed out. Nothing goes out until the Sent folder is checked, even
    from a new draft with the same text."""
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    newest = None
    for line in lines:
        row = _row(line)
        if row.get("draft_id") == draft_id or (send_check and row.get("send_check") == send_check):
            newest = row
    return newest if newest and newest.get("event") in ("attempt", "unknown") else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", choices=("on", "off"), dest="state")
    ap.add_argument("--check", action="store_true", help="say whether this surface may send; sends nothing")
    args = ap.parse_args(argv)
    if args.state:
        write_setting(args.state)
    if args.check:
        reason = surface_refusal()
        print(f"refused: {reason}" if reason else "this surface may send once the owner says send")
        return 1 if reason else 0
    print("on" if enabled() else "off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
