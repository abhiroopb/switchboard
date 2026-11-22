#!/usr/bin/env python3
"""Cheap guards for Claude in Chrome (the Claude extension in the owner's main Brave).

Claude in Chrome stays in the main Brave profile and is the signed-in path. The extension
talks to Claude Code over native messaging, so none of owner_browser.py's controls (the
lease, the signedInNeed log, the cookie-script refusal) sit in its path. This file puts
the cheap ones back as a Claude Code PreToolUse hook on `mcp__claude-in-chrome__*`
(wired in system/claude-marketplace/plugins/pm-os/hooks/hooks.json):

  - logs every call (tool, decision, host names only, the stated need) to
    <main>/.state/claude-in-chrome.jsonl; never a full URL, page text, or tab title
  - refuses scheduled jobs and PM_OS_TEST_SANDBOX runs
  - refuses a worker whose brief carried no `Browser: signed-in <reason>` line
  - refuses bank, email, payment, password-manager, private-topic, and Claude
    account hosts unless the brief carries `Browser: owner-named <host> ...: <owner's
    words>`; in an owner tab (no worker env) the same hosts become a permission prompt
  - refuses browser-internal pages (brave://, chrome://, chrome-extension://, file://)
  - refuses page scripts that touch cookies, web storage, IndexedDB, or credentials
  - fails closed: an error in the guard refuses the call

Limits: it sees only what a tool call names. A click that follows a link to a bank, or
a page the owner already had open, carries no URL to check. Sites are matched by host
suffix, so a custom domain a bank uses for sign-in needs adding to
local/claude-in-chrome-sensitive-domains.txt (one host per line, # comments).

Launch side: dispatch.py launch_command_for adds `--chrome` to a Claude worker only when
PM_OS_CLAUDE_IN_CHROME=on for the machine and the brief carries the signed-in line, so
every other worker starts without the extension (the docs warn about context cost and
Windows named-pipe conflicts between sessions).

  python3 system/scripts/claude_in_chrome_guard.py pre-tool-use < hook.json
  python3 system/scripts/claude_in_chrome_guard.py status
  python3 system/scripts/claude_in_chrome_guard.py set on|off
  python3 system/scripts/claude_in_chrome_guard.py check-url <url>
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

TOOL_PREFIX = "mcp__claude-in-chrome__"
KEY = "PM_OS_CLAUDE_IN_CHROME"
NEED_ENV = "PM_OS_CHROME_NEED"
NAMED_ENV = "PM_OS_CHROME_OWNER_NAMED"
WORDS_ENV = "PM_OS_CHROME_OWNER_WORDS"
NATIVE_HOST = "com.anthropic.claude_code_browser_extension"
BRAVE_HOSTS_KEY = r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts"
LOCAL_DOMAINS_FILE = "claude-in-chrome-sensitive-domains.txt"

# Host suffixes an agent never reaches without the owner naming the task. Generic on
# purpose: the owner's own institutions go in local/ (gitignored), never in this file.
SENSITIVE_DOMAINS: dict[str, tuple[str, ...]] = {
    "email": (
        "mail.google.com", "gmail.com", "inbox.google.com", "outlook.live.com", "outlook.office.com",
        "outlook.office365.com", "outlook.com", "hotmail.com", "live.com", "mail.yahoo.com",
        "proton.me", "protonmail.com", "icloud.com", "mail.aol.com", "fastmail.com", "hey.com",
    ),
    "account security": (
        "accounts.google.com", "myaccount.google.com", "passwords.google.com", "pay.google.com",
        "payments.google.com", "account.microsoft.com", "login.live.com", "login.microsoftonline.com",
        "appleid.apple.com", "account.apple.com", "account.brave.com",
    ),
    "password manager": (
        "1password.com", "1password.eu", "bitwarden.com", "lastpass.com", "dashlane.com",
        "keepersecurity.com", "nordpass.com",
    ),
    "claude account": (
        "claude.ai", "claude.com", "console.anthropic.com", "platform.claude.com",
    ),
    "bank or money": (
        "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com", "citibank.com",
        "capitalone.com", "usbank.com", "pnc.com", "truist.com", "tdbank.com", "ally.com",
        "discover.com", "americanexpress.com", "barclaysus.com", "synchrony.com",
        "schwab.com", "fidelity.com", "vanguard.com", "etrade.com", "morganstanley.com",
        "merrilledge.com", "ml.com", "robinhood.com", "sofi.com", "marcus.com", "chime.com",
        "coinbase.com", "kraken.com", "paypal.com", "venmo.com", "zellepay.com", "wise.com",
        "stripe.com", "revolut.com", "monzo.com",
        "empower.com", "personalcapital.com", "monarchmoney.com",
        "irs.gov", "ssa.gov", "treasurydirect.gov", "turbotax.intuit.com", "creditkarma.com",
        "experian.com", "equifax.com", "transunion.com",
    ),
}
INTERNAL_SCHEMES = ("brave", "chrome", "chrome-extension", "chrome-untrusted", "devtools", "edge",
                    "about", "file", "view-source", "javascript", "data")
# Page script only (javascript_tool, browser_batch). Broad on purpose:
# `document?.cookie`, a property descriptor, or `'local'+'Storage'` all still name cookie or
# Storage. A cookie banner is dismissed with a click instead.
SECRET_RE = re.compile(r"cookie|Storage\b|indexedDB|credentials|PasswordCredential", re.I)
SCRIPT_TOOLS = ("javascript_tool", "browser_batch")
TEXT_KEYS = ("url", "text", "value", "query")
DOMAIN_TOKEN_RE = re.compile(r"(?<![@\w.-])[a-z0-9-]+(?:\.[a-z0-9-]+)+")
# Browsers read any run of / or \ after a special scheme as the start of the host, and even
# none (`https:chase.com`), so the slashes are optional here.
URL_RE = re.compile(r"\b([a-z][a-z0-9+.-]*):([\\/]*)([^\s/'\"<>?#\\)]*)", re.I)
SPECIAL_SCHEMES = ("http", "https", "ws", "wss", "ftp")
# Only in the brief's header block (the lines before the first blank line), flush left, so a
# quoted email or pasted page further down cannot grant access.
BRIEF_SIGNED_IN_RE = re.compile(r"^Browser:[ \t]*signed-in\b[ \t:,.(\-]*(?P<need>.*?)\)?[ \t]*$", re.I | re.M)
BRIEF_NAMED_RE = re.compile(r"^Browser:[ \t]*owner-named[ \t]+(?P<hosts>[^:\n]+?)[ \t]*:[ \t]*(?P<words>.+?)[ \t]*$", re.I | re.M)
HOST_TOKEN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


# ── Machine switch ─────────────────────────────────────────────────────

def _env_file(env: Mapping[str, str]) -> Path:
    from comms_triage_setting import env_file
    return env_file(env)


def _local_dir(env: Mapping[str, str]) -> Path:
    from comms_triage_setting import local_dir
    return local_dir(env)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    value = env.get(KEY)
    if value is None:
        try:
            for line in _env_file(env).read_text(encoding="utf-8-sig").splitlines():
                name, _, raw = line.partition("=")
                if name.strip() == KEY:
                    value = raw.strip().strip("\"'")
        except (OSError, UnicodeDecodeError):
            value = None
    return (value or "").strip().lower() == "on"


def set_switch(value: str, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    path = _env_file(env)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    lines = [line for line in lines if line.partition("=")[0].strip() != KEY]
    if value == "on":
        lines.append(f"{KEY}=on")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def native_host(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Whether `claude --chrome` has registered its bridge for Brave, and what the manifest allows."""
    env = os.environ if env is None else env
    if env.get("PM_OS_CHROME_NATIVE_HOST_MANIFEST"):  # tests
        manifest_path = env["PM_OS_CHROME_NATIVE_HOST_MANIFEST"]
    elif os.name != "nt":
        return {"registered": False, "reason": "not Windows"}
    else:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BRAVE_HOSTS_KEY + "\\" + NATIVE_HOST) as key:
                manifest_path = str(winreg.QueryValueEx(key, "")[0])
        except OSError:
            return {"registered": False, "reason": "no Brave native messaging host"}
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"registered": True, "manifest": manifest_path, "manifest_readable": False}
    return {"registered": True, "manifest": manifest_path, "manifest_readable": True,
            "allowed_origins": manifest.get("allowed_origins", []), "path": manifest.get("path", "")}


# ── Brief parsing and the launch line ──────────────────────────────────

def _clean(text: str, limit: int) -> str:
    # The Windows launch line is typed through the wmux shim, which reads \r \n \t as keys,
    # so the value is printable ASCII with no backslash.
    import unicodedata
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[\x00-\x1f\x7f\\]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def header_block(prompt: str) -> str:
    """The brief's leading lines, up to the first blank line."""
    return re.split(r"\r?\n[ \t]*\r?\n", (prompt or "").lstrip("\r\n"), 1)[0]


def parse_brief(prompt: str) -> dict[str, Any]:
    need, hosts, words = "", [], ""
    head = header_block(prompt)
    match = BRIEF_SIGNED_IN_RE.search(head)
    if match:
        need = _clean(match.group("need"), 160) or "signed-in (no reason given)"
    named = BRIEF_NAMED_RE.search(head)
    if named:
        for token in re.split(r"[\s,]+", named.group("hosts").lower()):
            token = token.strip().strip(".")
            if HOST_TOKEN_RE.match(token):
                hosts.append(token)
        words = _clean(named.group("words"), 200) if hosts else ""
    return {"need": need, "owner_named": hosts, "owner_words": words}


def launch_parts(row: Mapping[str, Any], env: Mapping[str, str] | None = None) -> tuple[dict[str, str], str]:
    """Env additions and the chrome flag for a Claude worker launch. Every worker that does not
    get `--chrome` gets `--no-chrome`, so turning on /chrome "Enabled by default" never loads
    the extension into all workers."""
    env = os.environ if env is None else env
    if row.get("delivery") == "digest" or not enabled(env):
        return {}, " --no-chrome"
    brief = parse_brief(str(row.get("prompt") or ""))
    if not brief["need"]:
        return {}, " --no-chrome"
    extra = {NEED_ENV: brief["need"]}
    if brief["owner_named"]:
        extra[NAMED_ENV] = ",".join(brief["owner_named"])
        extra[WORDS_ENV] = brief["owner_words"]
    return extra, " --chrome"


# ── The hook ───────────────────────────────────────────────────────────

def local_domains(env: Mapping[str, str]) -> list[str]:
    try:
        text = (_local_dir(env) / LOCAL_DOMAINS_FILE).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    return [line.split("#", 1)[0].strip().lower() for line in text.splitlines() if line.split("#", 1)[0].strip()]


def sensitive_kind(host: str, env: Mapping[str, str]) -> str:
    host = host.lower().rstrip(".")
    for kind, domains in SENSITIVE_DOMAINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return kind
    if any(host == d or host.endswith("." + d) for d in local_domains(env)):
        return "owner-listed"
    return ""


def _strings(value: Any, key: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, str(k))
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, key)


def strip_controls(text: str) -> str:
    """Browsers drop tabs, newlines, and other C0 characters from a URL before parsing it."""
    return re.sub(r"[\x00-\x1f\x7f]", "", text)


def normalize_host(authority: str) -> str:
    """The host a browser would reach: percent-escapes decoded, userinfo, port, brackets, and
    trailing dots dropped, unicode full stops folded, IDNA-encoded, lowercased."""
    import unicodedata
    from urllib.parse import unquote
    host = re.split(r"[\\/?#]", strip_controls(unquote(authority)), 1)[0]
    host = host.rsplit("@", 1)[-1]
    host = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":", 1)[0]
    host = unicodedata.normalize("NFKC", host)
    host = re.sub("[。．｡]", ".", host).strip().rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii") if host else host
    except UnicodeError:
        pass  # a host IDNA cannot encode is not one of the listed ASCII domains
    return host


def targets(tool_input: Any) -> tuple[list[str], list[str]]:
    """(hosts, refused schemes) named anywhere in the tool input. A `url` value whose scheme
    the guard cannot map to a host counts as refused, so the call fails closed."""
    import unicodedata
    hosts, internal = [], []
    for key, text in _strings(tool_input):
        is_url = key.lower() == "url"
        if is_url:
            text = unicodedata.normalize("NFKC", strip_controls(text))
            if text.strip().lower() == "about:blank":  # a harmless reset target
                continue
        if key.lower() in TEXT_KEYS:
            # Backstop: a domain typed into the address bar or written
            # oddly still counts. An email address (`me@<mail host>`) is not a destination.
            folded = re.sub("[。．｡]", ".", unicodedata.normalize("NFKC", strip_controls(text))).lower()
            hosts.extend(DOMAIN_TOKEN_RE.findall(folded))
        for match in URL_RE.finditer(text):
            scheme, slashes, authority = match.group(1).lower(), match.group(2), match.group(3)
            at_start = match.start() == len(text) - len(text.lstrip())
            if scheme in SPECIAL_SCHEMES:
                hosts.append(authority)
            # about:, javascript:, and data: count only as a navigation target, never inside page script.
            elif scheme in INTERNAL_SCHEMES and (slashes or (is_url and at_start)):
                internal.append(scheme)
        if not is_url:
            continue
        raw = text.strip()
        scheme_match = re.match(r"^([a-z][a-z0-9+.-]*):(.*)$", raw, re.I | re.S)
        if raw[:1] in ("/", "\\"):  # protocol-relative //host
            hosts.append(raw.lstrip("/\\"))
        elif scheme_match and re.match(r"\d", scheme_match.group(2)):  # host:port with no scheme
            hosts.append(raw)
        elif scheme_match:
            scheme = scheme_match.group(1).lower()
            if scheme not in SPECIAL_SCHEMES and scheme not in INTERNAL_SCHEMES:
                internal.append(scheme)  # blob:, intent:, mailto:, and the rest have no host to check
        elif raw:
            hosts.append(raw)  # a bare host
    cleaned = []
    for authority in hosts:
        host = normalize_host(authority)
        if host and host not in cleaned:
            cleaned.append(host)
    return cleaned, sorted(set(internal))


def is_scheduled(env: Mapping[str, str], cwd: str) -> bool:
    if env.get("PM_OS_SCHEDULED_JOB") == "1" or env.get("CLAUDE_CODE_SESSION_ATTENDED") == "0":
        return True
    if env.get("PM_OS_WORKSTREAM_SLUG") == "scheduled-jobs":
        return True
    return "routines/scheduled-jobs" in cwd.replace("\\", "/")


def decide(event: Mapping[str, Any], env: Mapping[str, str]) -> tuple[str, str, dict[str, Any]]:
    """("allow" | "ask" | "deny", reason, log fields)."""
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") or {}
    worker = bool(env.get("PM_OS_WORKER_TASK_ID"))
    need = env.get(NEED_ENV, "")
    fields: dict[str, Any] = {"tool": tool[len(TOOL_PREFIX):], "worker": worker,
                              "task": env.get("PM_OS_WORKER_TASK_ID", ""), "workstream": env.get("PM_OS_WORKSTREAM_SLUG", ""),
                              "session": str(event.get("session_id") or "")}
    if env.get("PM_OS_TEST_SANDBOX") == "1":
        return "deny", "PM_OS_TEST_SANDBOX is set, so no test reaches the owner's Brave.", fields
    if is_scheduled(env, str(event.get("cwd") or "")):
        return "deny", ("scheduled jobs never use the owner's Brave, because nobody may be watching. "
                        "Use mcp__chrome-devtools-headless__* instead."), fields
    if worker and not need:
        return "deny", ("this worker's brief has no `Browser: signed-in <reason>` line, so it has no stated need for "
                        "the owner's signed-in Brave. Use mcp__chrome-devtools__* (agent profile), or ask CoS with "
                        "ask-cos.sh for a signed-in relaunch."), fields
    fields["need"] = need[:160] if worker else "owner tab"
    hosts, internal = targets(tool_input)
    fields["hosts"] = hosts
    if internal:
        return "deny", f"browser-internal or unchecked pages ({', '.join(internal)}) are off limits in the owner's Brave; use an http or https URL.", fields
    if fields["tool"] in SCRIPT_TOOLS:
        for _, text in _strings(tool_input):
            if SECRET_RE.search(text):
                return "deny", ("page scripts may not name cookies, web storage, IndexedDB, or saved credentials "
                                "in the owner's Brave; dismiss a cookie banner with a click instead."), fields
    named = [h for h in (env.get(NAMED_ENV, "").lower().split(",")) if h]
    for host in hosts:
        kind = sensitive_kind(host, env)
        if not kind:
            continue
        fields["sensitive"] = kind
        if worker and any(host == n or host.endswith("." + n) for n in named):
            fields["owner_words"] = env.get(WORDS_ENV, "")[:200]
            continue
        if worker:
            return "deny", (f"{host} is a {kind} site, which agents reach only when the owner names the task. CoS "
                            "adds `Browser: owner-named <host>: <the owner's words>` to the brief after the owner says so; "
                            "ask through ask-cos.sh."), fields
        return "ask", f"{host} is a {kind} site. Allow Claude in Chrome to act there for this call?", fields
    return "allow", "", fields


def log(env: Mapping[str, str], decision: str, fields: Mapping[str, Any]) -> None:
    try:
        path = _local_dir(env).parent / ".state" / "claude-in-chrome.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "decision": decision, **fields}) + "\n")
    except Exception:  # logging never decides a call
        pass


def hook(stdin: str, env: Mapping[str, str]) -> dict[str, Any] | None:
    try:
        event = json.loads(stdin or "{}")
    except ValueError:  # only Claude in Chrome calls reach this hook, so an unreadable one is refused
        event = {"tool_name": TOOL_PREFIX + "unknown", "__unreadable": True}
    if not isinstance(event, dict):
        event = {"tool_name": TOOL_PREFIX + "unknown", "__unreadable": True}
    tool = str(event.get("tool_name") or "")
    if not tool.startswith(TOOL_PREFIX):
        return None
    if event.get("__unreadable"):
        log(env, "deny", {"tool": "unknown", "error": "unreadable hook input"})
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "claude-in-chrome guard: unreadable hook input, so the call is refused."}}
    try:
        decision, reason, fields = decide(event, env)
    except Exception as exc:  # fail closed
        decision, reason, fields = "deny", f"the guard failed ({type(exc).__name__}), so it refuses the call.", {"tool": tool}
    log(env, decision, fields)
    if decision == "allow":
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": f"claude-in-chrome guard: {reason}"}}


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "pre-tool-use":
        try:
            out = hook(sys.stdin.buffer.read().decode("utf-8", "replace"), os.environ)
            if out:
                print(json.dumps(out))
            return 0
        except BaseException as exc:  # exit 2 blocks the call even if stdout is lost
            print(f"claude-in-chrome guard failed ({type(exc).__name__}), so the call is refused.", file=sys.stderr)
            return 2
    if command == "status":
        print(json.dumps({"switch": "on" if enabled() else "off", "native_host": native_host()}, indent=1))
        return 0
    if command == "set" and len(argv) == 3 and argv[2] in ("on", "off"):
        if argv[2] == "on" and not native_host().get("registered"):
            print("refused: no Brave native messaging host yet. The owner runs `claude --chrome` once, accepts its "
                  "dialog, and restarts Brave first.", file=sys.stderr)
            return 2
        print(set_switch(argv[2]))
        return 0
    if command == "check-url" and len(argv) == 3:
        hosts, internal = targets({"url": argv[2]})
        print(json.dumps({"hosts": hosts, "internal": internal,
                          "sensitive": {h: sensitive_kind(h, os.environ) for h in hosts}}))
        return 0
    print("usage: claude_in_chrome_guard.py pre-tool-use|status|set on|off|check-url <url>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
