#!/usr/bin/env python3
"""pmos-shim: the PM OS connector CLI, which talks to Google, Slack and Dropbox
directly.

PM OS scripts and skills (todo_cli.py, reply_check.py, gmail_search.py,
daemon/slack-send.sh, the gmail/gcal/gdrive/slack skills) reach Gmail,
Calendar, Drive and Slack through one command line:
`pmos <tool> <subcommand> [--flag value ...]`. This program is that
command. It serves a documented set of subcommands from the Gmail, Calendar,
Drive, Sheets, Slack Web and Dropbox REST APIs with the owner's own OAuth
client and tokens, and prints output in the shapes the repo parsers read
(system/connectors/README.md lists them). Any other tool or verb exits 2 with
a one-line "not available" message, so a caller falls through the same way it
does for any missing verb.

Standard library only (urllib, http.server, json, hashlib, secrets, base64,
email). Runs on Windows (native Python 3.11+), macOS and Linux.

Shim-only verbs
  pmos login google [--client-file PATH] [--scopes readonly|full]
  pmos login slack            token from SLACK_USER_TOKEN or a prompt
  pmos login dropbox --app-key KEY [--no-browser]
  pmos status
  pmos doctor [dropbox]
  pmos logout <google|slack|dropbox>

Environment
  PM_OS_CONNECTORS_DIR   auth directory (default ~/.pm-os/connectors)
  PM_OS_CONNECTOR_TIMEOUT         HTTP timeout in seconds (default 60)
  SLACK_USER_TOKEN       read once by `pmos login slack`
  PM_OS_SHIM_TRACE          "1" prints each HTTP method and URL to stderr
                         (never query strings or bodies that hold tokens)

Exit codes: 0 ok, 1 API or runtime error, 2 usage or unsupported verb,
3 tool not configured, 4 mail send refused (nothing sent), 5 mail
send outcome unknown (never retried, Sent is checked first).
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import email
import email.message
import email.policy
import getpass
import email.utils
import hashlib
import http.client
import http.server
import io
import json
import mimetypes
import os
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

SHIM_VERSION = "0.1.0"
SUPPORTED_TOOLS = ("mail", "calendar", "drive", "slack", "dropbox")
# Tools other connector CLIs serve. The shim names them in its error instead of guessing.
OTHER_TOOLS = ("linear", "jira", "airtable", "figma")
DEFAULT_TIMEOUT = 60.0

GOOGLE_SCOPES = {
    "full": [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/calendar",
    ],
    "readonly": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    ],
}
DROPBOX_AUTH_URI = "https://www.dropbox.com/oauth2/authorize"
DROPBOX_TOKEN_URI = "https://api.dropboxapi.com/oauth2/token"
DROPBOX_API = "https://api.dropboxapi.com/2"
# Shared links only (account_info.read is on for every Dropbox app and serves the doctor probe).
DROPBOX_SCOPES = ["account_info.read", "sharing.read", "sharing.write"]
# Dropbox matches redirect URIs exactly, so this port is fixed and registered on the app.
DROPBOX_REDIRECT_PORT = 7473
DROPBOX_AUTH_WAIT_SECONDS = 900  # the owner may be reading the ask in another tab
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SLACK_API = "https://slack.com/api"

MIME_DOC = "application/vnd.google-apps.document"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"
MIME_SLIDES = "application/vnd.google-apps.presentation"
MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_SHORTCUT = "application/vnd.google-apps.shortcut"
DRIVE_FILE_FIELDS = ("id,name,mimeType,createdTime,modifiedTime,viewedByMeTime,owners,parents,size,"
                     "starred,trashed,webViewLink,thumbnailLink,exportLinks,shortcutDetails")

def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)

class ShimError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def not_available(verb: str) -> ShimError:
    return ShimError(
        f"pmos-shim: '{verb}' is not available in the connector shim (it covers mail, "
        f"calendar, drive, slack, dropbox). See system/connectors/README.md.", 2)


def out(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def out_json(payload: Any) -> None:
    out(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def trace_enabled() -> bool:
    return os.environ.get("PMOS_SHIM_TRACE") == "1"


# ----------------------------------------------------------------------- args

class Flag:
    """One command-line option. kind: str | int | bool | list | json."""

    def __init__(self, name: str, kind: str = "str", help: str = "", required: bool = False,
                 default: Any = None, choices: tuple[str, ...] | None = None):
        self.name, self.kind, self.help, self.required, self.default, self.choices = (
            name, kind, help, required, default, choices)

    @property
    def key(self) -> str:
        return self.name.replace("-", "_")


def parse_flags(argv: list[str], spec: list[Flag]) -> dict[str, Any]:
    """Parse `--flag value`, `--flag=value`, `--list a b c`, `--bool [true|false]`,
    `--no-bool`, `--json '{...}'`. `--raw`, `--header` and `--timeout` are accepted
    and ignored here (timeout is read globally). Unknown flags exit 2."""
    by_name = {f.name: f for f in spec}
    args: dict[str, Any] = {f.key: f.default for f in spec}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            raise ShimError(f"error: unexpected argument '{arg}' found", 2)
        inline = None
        if "=" in arg:
            arg, inline = arg.split("=", 1)
        name = arg[2:]
        if name in ("raw",):
            i += 1
            continue
        if name in ("header", "timeout"):
            i += 2 if inline is None else 1
            continue
        if name == "json":
            raw = inline if inline is not None else (argv[i + 1] if i + 1 < len(argv) else "")
            try:
                payload = json.loads(raw)
            except ValueError:
                raise ShimError("error: --json must be a JSON object", 2)
            if not isinstance(payload, dict):
                raise ShimError("error: --json must be a JSON object", 2)
            for key, value in payload.items():
                args[key.replace("-", "_")] = value
            i += 1 if inline is not None else 2
            continue
        if name.startswith("no-") and name[3:] in by_name and by_name[name[3:]].kind == "bool":
            args[by_name[name[3:]].key] = False
            i += 1
            continue
        flag = by_name.get(name)
        if flag is None:
            raise ShimError(f"error: unexpected argument '--{name}' found", 2)
        if flag.kind == "bool":
            if inline is not None:
                args[flag.key] = inline.lower() == "true"
                i += 1
            elif i + 1 < len(argv) and argv[i + 1].lower() in ("true", "false"):
                args[flag.key] = argv[i + 1].lower() == "true"
                i += 2
            else:
                args[flag.key] = True
                i += 1
            continue
        if flag.kind == "list":
            values: list[str] = []
            if inline is not None:
                values.append(inline)
                i += 1
            else:
                i += 1
                while i < len(argv) and not argv[i].startswith("--"):
                    values.append(argv[i])
                    i += 1
            args[flag.key] = (args.get(flag.key) or []) + values if isinstance(args.get(flag.key), list) else values
            continue
        if inline is None:
            if i + 1 >= len(argv):
                raise ShimError(f"error: a value is required for '--{name} <{flag.kind.upper()}>' but none was supplied", 2)
            value = argv[i + 1]
            i += 2
        else:
            value = inline
            i += 1
        if flag.kind == "int":
            try:
                args[flag.key] = int(value)
            except ValueError:
                raise ShimError(f"error: invalid value '{value}' for '--{name} <INTEGER>'", 2)
        elif flag.kind == "json":
            try:
                args[flag.key] = json.loads(value)
            except ValueError:
                raise ShimError(f"error: invalid JSON for '--{name}'", 2)
        else:
            if flag.choices and value not in flag.choices:
                raise ShimError(f"error: invalid value '{value}' for '--{name} <TEXT>'\n  [possible values: "
                                + ", ".join(flag.choices) + "]", 2)
            args[flag.key] = value
    missing = [f for f in spec if f.required and args.get(f.key) in (None, "", [])]
    if missing:
        lines = "\n".join(f"  --{f.name} <{f.kind.upper() if f.kind != 'str' else 'TEXT'}>" for f in missing)
        raise ShimError("error: the following required arguments were not provided:\n" + lines, 2)
    return args


def render_help(tool: str, sub: str, doc: str, spec: list[Flag]) -> str:
    lines = [(doc or "").strip(), "", f"Usage: pmos {tool} {sub} [OPTIONS]", "", "Options:",
             "      --timeout <SECONDS>  Seconds to wait for each HTTP call. [env: PM_OS_CONNECTOR_TIMEOUT=]",
             "  -h, --help               Show this help", "", "Tool options:"]
    for f in spec:
        typ = {"str": "TEXT", "int": "INTEGER", "json": "JSON", "list": "TEXT...", "bool": "[<BOOL>]"}[f.kind]
        head = f"      --{f.name} {typ}" if f.kind != "bool" else f"      --{f.name} {typ} / --no-{f.name}"
        desc = ("Required. " if f.required else (f"Default: {f.default}. " if f.default not in (None, "") else "")) + f.help
        if f.choices:
            desc += " [possible values: " + ", ".join(f.choices) + "]"
        lines.append(head)
        lines.append("          " + desc.strip())
    lines.append("")
    lines.append("Use `--json '{...}'` for nested object or array payloads.")
    return "\n".join(lines)


# ----------------------------------------------------------------------- time

def tzinfo_for(name: str | None) -> dt.tzinfo:
    """IANA zone when the platform has tz data, else the local zone."""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def tz_name(tz: dt.tzinfo) -> str:
    return getattr(tz, "key", None) or (dt.datetime.now(tz).tzname() or "UTC")


def parse_rfc3339(value: str) -> dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def parse_iso_duration(value: str) -> dt.timedelta:
    """ISO 8601 duration such as P7D, PT12H, P1W, P1DT6H."""
    m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip().upper())
    if not m or not any(m.groups()):
        raise ShimError(f"error: invalid ISO 8601 duration '{value}'", 2)
    w, d, h, mi, s = (int(g or 0) for g in m.groups())
    return dt.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse_day(value: str, tz: dt.tzinfo, end: bool = False) -> dt.datetime:
    text = value.strip().lower()
    today = dt.datetime.now(tz).date()
    if text == "today":
        day = today
    elif text == "yesterday":
        day = today - dt.timedelta(days=1)
    elif text == "tomorrow":
        day = today + dt.timedelta(days=1)
    else:
        day = dt.date.fromisoformat(text[:10])
    base = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    return base + dt.timedelta(days=1) if end else base


# ----------------------------------------------------------------------- http

class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status, self.headers, self.body = status, headers, body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body.strip():
            return {}
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except ValueError:
            raise HttpError(self.status, "response was not JSON: " + self.text[:200])


class Http:
    """The one network seam. Tests monkeypatch `send`."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def send(self, method: str, url: str, headers: dict[str, str], data: bytes | None) -> Response:
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return Response(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read())
        except urllib.error.URLError as exc:
            raise HttpError(0, f"network error: {exc.reason}")
        except TimeoutError:
            raise HttpError(0, f"timed out after {self.timeout}s")

    def call(self, method: str, url: str, *, params: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None, json_body: Any = None, data: bytes | None = None,
             content_type: str | None = None, ok_statuses: tuple[int, ...] = (200, 201, 204)) -> Response:
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "", [])}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
        hdrs = {"Accept": "application/json", "User-Agent": f"pmos-shim/{SHIM_VERSION}"}
        hdrs.update(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json; charset=utf-8"
        elif content_type:
            hdrs["Content-Type"] = content_type
        if trace_enabled():
            print(f"[pmos-shim] {method} {url.split('?', 1)[0]}", file=sys.stderr)
        response = self.send(method, url, hdrs, data)
        if response.status not in ok_statuses:
            raise HttpError(response.status, api_error_message(response))
        return response


def api_error_message(response: Response) -> str:
    try:
        payload = json.loads(response.text)
    except ValueError:
        return response.text.strip()[:300] or "request failed"
    if isinstance(payload, dict):
        if payload.get("error_summary"):  # Dropbox
            return str(payload["error_summary"]).rstrip("/.")
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or err)
        if isinstance(err, str):
            desc = payload.get("error_description")
            return f"{err}: {desc}" if desc else err
    return json.dumps(payload)[:300]


# ----------------------------------------------------------------------- auth

def connectors_dir() -> Path:
    return Path(os.environ.get("PM_OS_CONNECTORS_DIR") or Path.home() / ".pm-os" / "connectors")


def write_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    _os_replace(tmp, path)


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class GoogleAuth:
    """OAuth client file plus token file under the connectors directory.
    `token()` refreshes through the token endpoint when the access token is
    within 60 seconds of expiry. `login()` runs the installed-app loopback
    flow with PKCE."""

    CLIENT_FILE = "google-oauth-client.json"
    TOKEN_FILE = "google-token.json"

    def __init__(self, http: Http, directory: Path | None = None):
        self.http = http
        self.dir = directory or connectors_dir()
        self.client_path = self.dir / self.CLIENT_FILE
        self.token_path = self.dir / self.TOKEN_FILE

    # -- files
    def client(self, override: str | None = None) -> dict[str, str]:
        path = Path(override) if override else self.client_path
        payload = read_json_file(path)
        if not payload:
            raise ShimError(f"error: google: OAuth client file not found or unreadable: {path}\n"
                            f"Download a Desktop app OAuth client JSON from Google Cloud Console to that path.", 3)
        inner = payload.get("installed") or payload.get("web") or payload
        if not inner.get("client_id") or not inner.get("client_secret"):
            raise ShimError(f"error: google: {path} has no client_id/client_secret", 3)
        return {"client_id": inner["client_id"], "client_secret": inner["client_secret"],
                "auth_uri": inner.get("auth_uri") or GOOGLE_AUTH_URI,
                "token_uri": inner.get("token_uri") or GOOGLE_TOKEN_URI}

    def stored(self) -> dict[str, Any] | None:
        return read_json_file(self.token_path)

    def configured(self) -> bool:
        tok = self.stored()
        return bool(tok and tok.get("refresh_token"))

    def expires_at(self) -> float:
        tok = self.stored() or {}
        try:
            return float(tok.get("expires_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    # -- tokens
    def token(self, tool: str = "google") -> str:
        tok = self.stored()
        if not tok or not tok.get("refresh_token"):
            raise ShimError(f"error: {tool}: not configured. Run: pmos login google", 3)
        if tok.get("access_token") and self.expires_at() - time.time() > 60:
            return str(tok["access_token"])
        return self.refresh(tok)

    def refresh(self, tok: dict[str, Any]) -> str:
        form = urllib.parse.urlencode({
            "client_id": tok["client_id"], "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}).encode()
        try:
            resp = self.http.call("POST", tok.get("token_uri") or GOOGLE_TOKEN_URI, data=form,
                                  content_type="application/x-www-form-urlencoded")
        except HttpError as exc:
            raise ShimError(f"error: google: token refresh failed: HTTP {exc.status} {exc}. "
                            f"Run: pmos login google", 3)
        payload = resp.json()
        tok["access_token"] = payload["access_token"]
        tok["expires_at"] = time.time() + float(payload.get("expires_in") or 3600)
        write_private(self.token_path, tok)
        return str(tok["access_token"])

    def headers(self, tool: str = "google") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(tool)}"}

    # -- login
    def login(self, client_file: str | None, scopes_mode: str,
              open_browser: Callable[[str], Any] = webbrowser.open) -> dict[str, Any]:
        client = self.client(client_file)
        scopes = GOOGLE_SCOPES.get(scopes_mode)
        if not scopes:
            raise ShimError("error: --scopes must be readonly or full", 2)
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        server = http.server.HTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        server.timeout = 300
        redirect = f"http://127.0.0.1:{server.server_port}/"
        url = client["auth_uri"] + "?" + urllib.parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
        print("Open this URL in a browser to authorize the shim:\n  " + url, file=sys.stderr)
        try:
            open_browser(url)
        except Exception:
            pass
        _LoopbackHandler.result = None
        while _LoopbackHandler.result is None:
            server.handle_request()
            if _LoopbackHandler.result is None:
                server.server_close()
                raise ShimError("error: google: no authorization response within 300 seconds", 1)
        server.server_close()
        result = _LoopbackHandler.result
        if result.get("state") != state:
            raise ShimError("error: google: state mismatch in authorization response", 1)
        if result.get("error"):
            raise ShimError(f"error: google: authorization denied: {result['error']}", 1)
        return self.exchange(client, result["code"], verifier, redirect, scopes)

    def exchange(self, client: dict[str, str], code: str, verifier: str, redirect: str,
                 scopes: list[str]) -> dict[str, Any]:
        form = urllib.parse.urlencode({
            "client_id": client["client_id"], "client_secret": client["client_secret"], "code": code,
            "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect}).encode()
        try:
            payload = self.http.call("POST", client["token_uri"], data=form,
                                     content_type="application/x-www-form-urlencoded").json()
        except HttpError as exc:
            raise ShimError(f"error: google: token exchange failed: HTTP {exc.status} {exc}", 1)
        if not payload.get("refresh_token"):
            raise ShimError("error: google: no refresh token returned. Remove the app's access at "
                            "https://myaccount.google.com/permissions and run auth again.", 1)
        tok = {"access_token": payload.get("access_token"), "refresh_token": payload["refresh_token"],
               "expires_at": time.time() + float(payload.get("expires_in") or 3600),
               "scopes": scopes, "client_id": client["client_id"], "client_secret": client["client_secret"],
               "token_uri": client["token_uri"], "saved_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        write_private(self.token_path, tok)
        return tok

    def logout(self) -> bool:
        if self.token_path.exists():
            self.token_path.unlink()
            return True
        return False


class _ExclusiveHTTPServer(http.server.HTTPServer):
    """On Windows SO_REUSEADDR lets a second listener share the port, so the older login would take the redirect."""
    allow_reuse_address = os.name != "nt"


class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] | None = None
    timeout = 10  # a client that connects and sends nothing must not hold the login
    expected_state: str | None = None  # set by a login that ignores stray requests

    def do_GET(self) -> None:  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _LoopbackHandler.result = {k: v[0] for k, v in query.items()}
        stale = _LoopbackHandler.expected_state and _LoopbackHandler.result.get("state") != _LoopbackHandler.expected_state
        body = (b"<html><body><p>pmos-shim: this sign-in link is out of date. Use the newest link.</p></body></html>" if stale
                else b"<html><body><p>pmos-shim: authorization received. You can close this tab.</p></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        return


class SlackAuth:
    """`slack-token.json` holding a user (`xoxp-`) or bot (`xoxb-`) token."""

    TOKEN_FILE = "slack-token.json"

    def __init__(self, http: Http, directory: Path | None = None):
        self.http = http
        self.dir = directory or connectors_dir()
        self.token_path = self.dir / self.TOKEN_FILE

    def stored(self) -> dict[str, Any] | None:
        return read_json_file(self.token_path)

    def configured(self) -> bool:
        return bool((self.stored() or {}).get("token"))

    def token(self) -> str:
        tok = self.stored()
        if not tok or not tok.get("token"):
            raise ShimError("error: slack: not configured. Run: pmos login slack", 3)
        return str(tok["token"])

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def login(self, prompt: Callable[[str], str] = getpass.getpass) -> dict[str, Any]:
        token = os.environ.get("SLACK_USER_TOKEN", "").strip()
        if not token:
            token = prompt("Slack token (xoxp- or xoxb-, input hidden): ").strip()
        if not token.startswith(("xoxp-", "xoxb-")):
            raise ShimError("error: slack: token must start with xoxp- or xoxb-", 2)
        try:
            check = self.http.call("POST", f"{SLACK_API}/auth.test", headers={"Authorization": f"Bearer {token}"},
                                   data=b"", content_type="application/x-www-form-urlencoded").json()
        except HttpError as exc:
            raise ShimError(f"error: slack: auth.test failed: HTTP {exc.status} {exc}", 1)
        if not check.get("ok"):
            raise ShimError(f"error: slack: auth.test failed: {check.get('error')}", 1)
        tok = {"token": token, "type": "user" if token.startswith("xoxp-") else "bot",
               "team": check.get("team"), "user_id": check.get("user_id"), "user": check.get("user"),
               "saved_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        write_private(self.token_path, tok)
        return tok

    def logout(self) -> bool:
        if self.token_path.exists():
            self.token_path.unlink()
            return True
        return False


class DropboxAuth:
    """`dropbox-token.json`: a scoped refresh token from the owner's own Dropbox app.
    PKCE public client, so there is no app secret to store."""

    TOKEN_FILE = "dropbox-token.json"

    def __init__(self, http: Http, directory: Path | None = None):
        self.http = http
        self.dir = directory or connectors_dir()
        self.token_path = self.dir / self.TOKEN_FILE

    def stored(self) -> dict[str, Any] | None:
        return read_json_file(self.token_path)

    def configured(self) -> bool:
        tok = self.stored()
        return bool(tok and tok.get("refresh_token") and tok.get("app_key"))

    def token(self) -> str:
        tok = self.stored()
        if not tok or not tok.get("refresh_token") or not tok.get("app_key"):
            raise ShimError("error: dropbox: not configured. Run: pmos login dropbox --app-key <KEY>", 3)
        try:
            if tok.get("access_token") and float(tok.get("expires_at") or 0) - time.time() > 60:
                return str(tok["access_token"])
        except (TypeError, ValueError):
            pass
        return self.refresh(tok)

    def refresh(self, tok: dict[str, Any]) -> str:
        form = urllib.parse.urlencode({"client_id": tok["app_key"], "refresh_token": tok["refresh_token"],
                                       "grant_type": "refresh_token"}).encode()
        try:
            resp = self.http.call("POST", DROPBOX_TOKEN_URI, data=form,
                                  content_type="application/x-www-form-urlencoded")
        except HttpError as exc:
            if exc.status in (400, 401):
                raise ShimError(f"error: dropbox: token refresh failed: HTTP {exc.status} {exc}. "
                                f"Run: pmos login dropbox", 3)
            raise ShimError(f"error: dropbox: token refresh failed: HTTP {exc.status} {exc}", 1)
        payload = resp.json()
        tok["access_token"] = payload["access_token"]
        tok["expires_at"] = time.time() + float(payload.get("expires_in") or 14400)
        write_private(self.token_path, tok)
        return str(tok["access_token"])

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def login(self, app_key: str | None, open_browser: Callable[[str], Any] | None = webbrowser.open) -> dict[str, Any]:
        app_key = (app_key or (self.stored() or {}).get("app_key") or "").strip()
        if not app_key:
            raise ShimError("error: dropbox: no app key. Run: pmos login dropbox --app-key <KEY> "
                            "(system/connectors/README.md, Setup Dropbox)", 2)
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        try:
            server = _ExclusiveHTTPServer(("127.0.0.1", DROPBOX_REDIRECT_PORT), _LoopbackHandler)
        except OSError as exc:
            raise ShimError(f"error: dropbox: port {DROPBOX_REDIRECT_PORT} is busy, close what holds it and retry ({exc})", 1)
        redirect = f"http://localhost:{DROPBOX_REDIRECT_PORT}/"  # registered on the app, character for character
        url = DROPBOX_AUTH_URI + "?" + urllib.parse.urlencode({
            "client_id": app_key, "redirect_uri": redirect, "response_type": "code",
            "token_access_type": "offline", "scope": " ".join(DROPBOX_SCOPES),
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
        print("Open this URL in a browser to authorize the shim:\n  " + url, file=sys.stderr, flush=True)
        if open_browser:
            try:
                opened = open_browser(url)
            except Exception:
                opened = False
            if not opened:
                print("The browser did not open. Paste the URL above into a browser.", file=sys.stderr, flush=True)
        result = None
        deadline = time.time() + DROPBOX_AUTH_WAIT_SECONDS
        _LoopbackHandler.expected_state = state
        try:
            while result is None and time.time() < deadline:
                server.timeout = max(1.0, deadline - time.time())
                _LoopbackHandler.result = None
                server.handle_request()
                got = _LoopbackHandler.result
                if got and got.get("state") == state:  # anything else is a stray request or a stale tab
                    result = got
        finally:
            server.server_close()
        if result is None:
            raise ShimError(f"error: dropbox: no authorization response within {DROPBOX_AUTH_WAIT_SECONDS} seconds", 1)
        if result.get("error"):
            raise ShimError(f"error: dropbox: authorization denied: {result['error']}", 1)
        return self.exchange(app_key, result.get("code", ""), verifier, redirect)

    def exchange(self, app_key: str, code: str, verifier: str, redirect: str) -> dict[str, Any]:
        form = urllib.parse.urlencode({"client_id": app_key, "code": code, "code_verifier": verifier,
                                       "grant_type": "authorization_code", "redirect_uri": redirect}).encode()
        try:
            payload = self.http.call("POST", DROPBOX_TOKEN_URI, data=form,
                                     content_type="application/x-www-form-urlencoded").json()
        except HttpError as exc:
            raise ShimError(f"error: dropbox: token exchange failed: HTTP {exc.status} {exc}", 1)
        if not payload.get("refresh_token"):
            raise ShimError("error: dropbox: no refresh token returned. Run auth again.", 1)
        granted = str(payload.get("scope") or "").split()
        missing = [s for s in DROPBOX_SCOPES if granted and s not in granted]
        if missing:
            raise ShimError(f"error: dropbox: the app lacks {', '.join(missing)}. Tick them on the app's "
                            f"Permissions tab, then run auth again.", 1)
        tok = {"access_token": payload.get("access_token"), "refresh_token": payload["refresh_token"],
               "expires_at": time.time() + float(payload.get("expires_in") or 14400), "app_key": app_key,
               "scopes": granted or DROPBOX_SCOPES, "account_id": payload.get("account_id"),
               "saved_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        write_private(self.token_path, tok)
        return tok

    def logout(self) -> bool:
        if self.token_path.exists():
            self.token_path.unlink()
            return True
        return False


# -------------------------------------------------------------- google helpers

class GoogleClient:
    def __init__(self, http: Http, auth: GoogleAuth, tool: str):
        self.http, self.auth, self.tool = http, auth, tool

    def get(self, url: str, **params: Any) -> Any:
        return self.http.call("GET", url, params=params, headers=self.auth.headers(self.tool)).json()

    def get_raw(self, url: str, **params: Any) -> Response:
        return self.http.call("GET", url, params=params, headers=self.auth.headers(self.tool))

    def send(self, method: str, url: str, body: Any = None, params: dict[str, Any] | None = None) -> Any:
        resp = self.http.call(method, url, params=params, headers=self.auth.headers(self.tool), json_body=body)
        return resp.json() if resp.body.strip() else {}

    def upload(self, method: str, url: str, metadata: dict[str, Any], content: bytes, content_type: str,
               params: dict[str, Any] | None = None) -> Any:
        boundary = "sqshim" + secrets.token_hex(12)
        parts = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                 f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {content_type}\r\n\r\n").encode()
        body = parts + content + f"\r\n--{boundary}--".encode()
        resp = self.http.call(method, url, params=params, headers=self.auth.headers(self.tool), data=body,
                              content_type=f"multipart/related; boundary={boundary}")
        return resp.json()


def b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode())


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ----------------------------------------------------------------------- gmail

def gmail_headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in (payload.get("headers") or [])}


def gmail_body(payload: dict[str, Any]) -> str:
    """Decoded text/plain body, else text/html stripped to text."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime.startswith("text/plain"):
            plain.append(b64url_decode(data).decode("utf-8", errors="replace"))
        elif data and mime.startswith("text/html"):
            html.append(b64url_decode(data).decode("utf-8", errors="replace"))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        return html_to_text("\n".join(html))
    return ""


def gmail_message_block(msg: dict[str, Any], *, snippet: bool, body_chars: int | None) -> str:
    """The `Key: value` block gmail_search.parse_search_text and todo_cli
    parse_gmail_search_blob read, in the order and with the labels they expect."""
    hdr = gmail_headers(msg.get("payload") or {})
    lines = [f"Subject: {hdr.get('subject', '')}", f"From: {hdr.get('from', '')}", f"To: {hdr.get('to', '')}"]
    if hdr.get("cc"):
        lines.append(f"Cc: {hdr['cc']}")
    lines += [f"Date: {hdr.get('date', '')}", f"ID: {msg.get('id', '')}", f"Thread ID: {msg.get('threadId', '')}",
              f"Labels: {', '.join(msg.get('labelIds') or [])}"]
    if snippet:
        lines.append(f"Snippet: {msg.get('snippet', '')}")
    if body_chars is not None:
        lines.append("Body:")
        lines.append(gmail_bounded_body(gmail_body(msg.get("payload") or {}), body_chars))
    return "\n".join(lines)


def gmail_bounded_body(body: str, limit: int) -> str:
    if limit <= 0:
        return f"(Body omitted because max_body_chars is 0; original length: {len(body)} characters.)"
    if len(body) > limit:
        return body[:limit] + f"\n[Truncated to {limit} characters; original length: {len(body)} characters.]"
    return body


def gmail_resolve_labels(client: GoogleClient, names: list[str]) -> list[str]:
    if not names:
        return []
    listing = client.get(f"{GMAIL_API}/labels").get("labels") or []
    by_name = {str(lbl.get("name", "")).lower(): lbl["id"] for lbl in listing}
    ids = {lbl["id"] for lbl in listing}
    resolved = []
    for name in names:
        if name in ids:
            resolved.append(name)
        elif name.lower() in by_name:
            resolved.append(by_name[name.lower()])
        elif name.upper() in ids:
            resolved.append(name.upper())
        else:
            raise ShimError(f"error: mail: label '{name}' not found", 1)
    return resolved


def gmail_build_query(a: dict[str, Any]) -> str:
    parts = []
    if a.get("query"):
        parts.append(str(a["query"]))
    if a.get("body_terms"):
        parts.append(str(a["body_terms"]))
    if a.get("subject"):
        parts.append(f"subject:({a['subject']})")
    if a.get("from_sender"):
        parts.append(f"from:{a['from_sender']}")
    if a.get("to"):
        parts.append(f"to:{a['to']}")
    if a.get("newer_than_days") is not None:
        parts.append(f"newer_than:{int(a['newer_than_days'])}d")
    if a.get("older_than_days") is not None:
        parts.append(f"older_than:{int(a['older_than_days'])}d")
    if a.get("after"):
        parts.append("after:" + str(a["after"]).replace("-", "/"))
    if a.get("before"):
        parts.append("before:" + str(a["before"]).replace("-", "/"))
    if a.get("only_is_read"):
        parts.append("is:read")
    if a.get("only_is_unread"):
        parts.append("is:unread")
    return " ".join(parts)


MAIL_SEARCH_FLAGS = [
    Flag("query", help="Gmail search syntax, ANDed with any of the filter flags below."),
    Flag("body-terms", help="Words that must appear in the message body or headers."),
    Flag("subject", help="Words that must appear in the subject line."),
    Flag("from-sender", help="Sender address to match."),
    Flag("to", help="Recipient address to match."),
    Flag("label", default="Inbox", help="Gmail label to search in; 'all' searches every folder."),
    Flag("max-results", "int", default=20, help="How many results to return, 1 to 100."),
    Flag("newer-than-days", "int", help="Keep messages from the last N days."),
    Flag("older-than-days", "int", help="Keep messages older than N days."),
    Flag("after", help="Keep messages dated after YYYY-MM-DD."),
    Flag("before", help="Keep messages dated before YYYY-MM-DD."),
    Flag("only-is-read", "bool", help="Return read messages only."),
    Flag("only-is-unread", "bool", help="Return unread messages only."),
    Flag("include-spam-trash", "bool", help="Also search the Spam and Trash folders."),
    Flag("include-body", "bool", help="Add each message's decoded body, cut to --max-body-chars, in place of the snippet."),
    Flag("max-body-chars", "int", default=2000, help="Body length cap per message when --include-body is on."),
    Flag("include-next-page-token", "bool", help="Print the token for the next page when there is one."),
    Flag("page-token", help="Token from an earlier search, to continue from that page."),
    Flag("fetch-all", "bool", default=True, help="Keep paging until --max-results is reached or Gmail runs out."),
    Flag("search-mode", default="messages", choices=("messages", "threads"), help="Match individual messages or whole threads."),
    Flag("include-drafts", "bool", help="Ignored here: drafts are never merged into results."),
    Flag("draft-page-token", help="Ignored here."),
    Flag("timezone", help="IANA zone name. Ignored here."),
]


def cmd_mail_search(client: GoogleClient, a: dict[str, Any]) -> Any:
    """mail search / search: --query --body-terms --subject
    --from-sender --to --label --max-results --newer-than-days --older-than-days
    --after --before --only-is-read --only-is-unread --include-spam-trash
    --include-body --max-body-chars --include-next-page-token --page-token
    --fetch-all --search-mode. Prints {"result": "<text block>"} in the layout
    gmail_search.parse_search_text reads."""
    max_results = max(1, min(100, int(a.get("max_results") or 20)))
    label = str(a.get("label") or "Inbox")
    label_ids = [] if label.lower() == "all" else gmail_resolve_labels(client, [label])
    q = gmail_build_query(a)
    mode = a.get("search_mode") or "messages"
    endpoint = f"{GMAIL_API}/threads" if mode == "threads" else f"{GMAIL_API}/messages"
    items: list[dict[str, Any]] = []
    token = a.get("page_token") or None
    estimate = 0
    while True:
        page = client.get(endpoint, q=q or None, labelIds=label_ids or None, maxResults=max_results - len(items),
                          pageToken=token, includeSpamTrash="true" if a.get("include_spam_trash") else None)
        items.extend(page.get("threads" if mode == "threads" else "messages") or [])
        estimate = page.get("resultSizeEstimate") or estimate
        token = page.get("nextPageToken")
        if not token or len(items) >= max_results or not a.get("fetch_all", True):
            break
    include_body = bool(a.get("include_body"))
    fmt = "full" if include_body else "metadata"
    blocks = []
    for item in items[:max_results]:
        if mode == "threads":
            thread = client.get(f"{GMAIL_API}/threads/{item['id']}", format=fmt,
                                metadataHeaders=None if include_body else ["Subject", "From", "To", "Cc", "Date"])
            blocks.append(gmail_thread_block(thread, a.get("max_body_chars") if include_body else None))
        else:
            msg = client.get(f"{GMAIL_API}/messages/{item['id']}", format=fmt,
                             metadataHeaders=None if include_body else ["Subject", "From", "To", "Cc", "Date"])
            blocks.append(gmail_message_block(msg, snippet=not include_body,
                                              body_chars=int(a.get("max_body_chars") or 2000) if include_body else None))
    head = ["Search summary:", f"Mode: {mode}", f"Returned count: {len(blocks)}", f"Result size estimate: {estimate}"]
    if a.get("include_next_page_token") and token:
        head.append(f"Next page token: {token}")
    text = "\n".join(head) + "\n\n" + "\n\n".join(blocks)
    notes = ["Note:", f"Only up to {max_results} results were returned due to performance constraints.",
             "You can increase `max_results` up to 100. To retrieve more, set",
             "`include_next_page_token=true` and pass the returned next page token as `page_token`."]
    if not blocks and label.lower() != "all":
        notes.insert(0, f"Warning: this search covered only the {label} label and found nothing. The mail may still "
                        "exist outside it. Retry with --label all before saying it is absent.")
    if not include_body:
        notes.append("Only snippets are returned for each result. Snippets are useful for quickly scanning through "
                     "your emails. If you need the full email, use the `read_message` tool.")
    notes.append("Use page_token with the next page token to continue paginated searches.")
    return {"result": text + ("\n" if blocks else "") + "\n".join(notes)}


def gmail_thread_block(thread: dict[str, Any], body_chars: int | None) -> str:
    msgs = list(thread.get("messages") or [])
    newest_first = list(reversed(msgs))
    latest = newest_first[0] if newest_first else {}
    hdr = gmail_headers(latest.get("payload") or {})
    labels = sorted({lbl for m in msgs for lbl in (m.get("labelIds") or [])})
    lines = [f"Thread ID: {thread.get('id', '')}", f"Message count: {len(msgs)}",
             f"Latest message ID: {latest.get('id', '')}", f"From: {hdr.get('from', '')}",
             f"Subject: {hdr.get('subject', '')}", f"To: {hdr.get('to', '')}", f"Date: {hdr.get('date', '')}",
             "Message IDs: " + ", ".join(m.get("id", "") for m in newest_first), "Labels: " + ", ".join(labels)]
    if body_chars is not None:
        lines.append("Bodies:")
        for m in newest_first:
            lines.append(f"Message {m.get('id', '')} body:")
            lines.append(gmail_bounded_body(gmail_body(m.get("payload") or {}), body_chars))
    else:
        lines.append(f"Snippet: {latest.get('snippet', '')}")
    return "\n".join(lines)


MAIL_READ_FLAGS = [
    Flag("message-ids", "list", help="Gmail message IDs to print."),
    Flag("thread-ids", "list", help="Gmail thread IDs to print."),
    Flag("draft-ids", "list", help="Gmail draft IDs to print."),
    Flag("max-body-chars", "int", default=12000, help="Body length cap per message; 0 leaves bodies out."),
    Flag("timezone", help="IANA zone name. Ignored here."),
]


def cmd_mail_read(client: GoogleClient, a: dict[str, Any]) -> Any:
    """mail read / read: --message-ids --thread-ids
    --draft-ids --max-body-chars. Message blocks end with `Body:`; thread
    blocks carry `Message IDs:` newest first, then `Bodies:` (reply_check.py,
    gmail_search.read_message)."""
    ids = (a.get("message_ids") or []) + (a.get("thread_ids") or []) + (a.get("draft_ids") or [])
    if not ids:
        raise ShimError("error: mail read: provide --message-ids, --thread-ids or --draft-ids", 2)
    if len(ids) > 100:
        raise ShimError("error: mail read: at most 100 IDs per call", 2)
    body_chars = int(a.get("max_body_chars") if a.get("max_body_chars") is not None else 12000)
    blocks = []
    for mid in a.get("message_ids") or []:
        msg = client.get(f"{GMAIL_API}/messages/{mid}", format="full")
        blocks.append(gmail_message_block(msg, snippet=False, body_chars=body_chars))
    for tid in a.get("thread_ids") or []:
        thread = client.get(f"{GMAIL_API}/threads/{tid}", format="full")
        blocks.append(gmail_thread_block(thread, body_chars))
    for did in a.get("draft_ids") or []:
        draft = client.get(f"{GMAIL_API}/drafts/{did}", format="full")
        blocks.append(f"Draft ID: {draft.get('id', '')}\n"
                      + gmail_message_block(draft.get("message") or {}, snippet=False, body_chars=body_chars))
    return {"result": "\n\n".join(blocks)}


MAIL_DRAFT_FLAGS = [
    Flag("body", required=True, help="Draft text. Plain text unless --html is set."),
    Flag("to-recipient", help="Recipient address. Needed for common and forward drafts, unused for replies."),
    Flag("cc", help="Cc addresses."),
    Flag("bcc", help="Bcc addresses."),
    Flag("subject", help="Subject for a common draft. Replies and forwards keep the original subject."),
    Flag("draft-type", default="common", choices=("common", "reply", "forward"), help="New message, reply, or forward."),
    Flag("reference-message-id", help="The message a new reply or forward answers."),
    Flag("draft-metadata", "json", help="The draft_metadata object an earlier call printed, to edit that draft."),
    Flag("html", "bool", help="Treat --body as HTML."),
]


def cmd_mail_draft(client: GoogleClient, a: dict[str, Any]) -> Any:
    """mail draft / draft: --body --to-recipient --cc --bcc
    --subject --draft-type common|reply|forward --reference-message-id
    --draft-metadata --html. Creates or updates a draft; never sends."""
    draft_type = a.get("draft_type") or "common"
    meta = a.get("draft_metadata") if isinstance(a.get("draft_metadata"), dict) else {}
    ref_id = a.get("reference_message_id") or meta.get("reference_message_id")
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    thread_id = meta.get("thread_id")
    subject = a.get("subject") or ""
    to = a.get("to_recipient") or ""
    if draft_type in ("reply", "forward"):
        if not ref_id:
            raise ShimError(f"error: mail draft: --reference-message-id is required for '{draft_type}'", 2)
        ref = client.get(f"{GMAIL_API}/messages/{ref_id}", format="full")
        hdr = gmail_headers(ref.get("payload") or {})
        original_subject = re.sub(r"^\s*(re|fwd?)\s*:\s*", "", hdr.get("subject", ""), flags=re.I)
        if draft_type == "reply":
            thread_id = ref.get("threadId")
            to = hdr.get("reply-to") or hdr.get("from", "")
            subject = "Re: " + original_subject
            if hdr.get("message-id"):
                msg["In-Reply-To"] = hdr["message-id"]
                msg["References"] = (hdr.get("references", "") + " " + hdr["message-id"]).strip()
        else:
            subject = "Fwd: " + original_subject
            quoted = ("\n\n---------- Forwarded message ---------\n"
                      f"From: {hdr.get('from', '')}\nDate: {hdr.get('date', '')}\nSubject: {hdr.get('subject', '')}\n"
                      f"To: {hdr.get('to', '')}\n\n" + gmail_body(ref.get("payload") or {}))
            a["body"] = str(a["body"]) + (quoted if not a.get("html") else html_escape_block(quoted))
    if draft_type != "reply" and not to:
        raise ShimError("error: mail draft: --to-recipient is required for 'common' and 'forward' drafts", 2)
    msg["To"] = to
    if a.get("cc"):
        msg["Cc"] = a["cc"]
    if a.get("bcc"):
        msg["Bcc"] = a["bcc"]
    msg["Subject"] = subject
    msg.set_content(str(a["body"]), subtype="html" if a.get("html") else "plain")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload: dict[str, Any] = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id
    draft_id = meta.get("draft_id")
    if draft_id:
        result = client.send("PUT", f"{GMAIL_API}/drafts/{draft_id}", payload)
        action = "updated"
    else:
        result = client.send("POST", f"{GMAIL_API}/drafts", payload)
        action = "created"
    message = result.get("message") or {}
    new_meta = {"draft_id": result.get("id"), "message_id": message.get("id"), "thread_id": message.get("threadId"),
                "draft_type": draft_type, "reference_message_id": ref_id}
    text = (f"Draft {action}. It is saved in Gmail and was not sent.\nDraft ID: {result.get('id', '')}\n"
            f"Message ID: {message.get('id', '')}\nThread ID: {message.get('threadId', '')}\nTo: {to}\n"
            f"Subject: {subject}\ndraft_metadata: {json.dumps(new_meta)}")
    return {"result": text, "draft_metadata": new_meta}


def html_escape_block(text: str) -> str:
    import html
    return "<br>" + html.escape(text).replace("\n", "<br>")


# Exit codes for a send. 4: refused, nothing went out. 5: outcome unknown, do not retry by hand.
SEND_REFUSED = 4
SEND_UNKNOWN = 5


def gmail_guard() -> Any:
    """system/scripts/gmail_send_guard.py decides who may send. No guard, no send."""
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import gmail_send_guard
    except Exception as exc:
        raise ShimError(f"refused: the send guard could not load ({type(exc).__name__}); nothing was sent", SEND_REFUSED)
    return gmail_send_guard


def gmail_body_sha(body: str) -> str:
    return hashlib.sha256("".join(body.split()).encode("utf-8")).hexdigest()[:16]


def gmail_draft_view(draft: dict[str, Any]) -> dict[str, Any]:
    """What the owner must see: recipients, subject, the body in each form a recipient may read,
    link targets, and a check over all of it plus every MIME part, so an edit changes the check."""
    message = draft.get("message") or {}
    payload = message.get("payload") or {}
    hdr = gmail_headers(payload)
    digest = hashlib.sha256()
    for name in ("from", "to", "cc", "bcc", "subject", "in-reply-to"):
        digest.update(f"{name}:{hdr.get(name, '')}\n".encode("utf-8"))
    attachments: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        body = part.get("body") or {}
        filename = str(part.get("filename") or "")
        if filename or body.get("attachmentId"):
            attachments.append(filename or "(inline)")
        digest.update(f"{part.get('mimeType', '')}|{filename}|{body.get('size', 0)}\n".encode("utf-8"))
        if body.get("data"):
            raw = b64url_decode(body["data"])
            digest.update(raw)
            if str(part.get("mimeType", "")).startswith("text/html"):
                html_parts.append(raw.decode("utf-8", errors="replace"))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    # Only the present headers: strict parsing returns nothing at all when a field is empty.
    fields = [hdr[name] for name in ("to", "cc", "bcc") if hdr.get(name)]
    addresses = [addr for _, addr in email.utils.getaddresses(fields) if addr]
    body = gmail_body(payload)
    html_text = html_to_text("\n".join(html_parts)) if html_parts else ""
    links = sorted(set(re.findall(r"""(?:href|src)\s*=\s*["']?([^"'\s>]+)""", "\n".join(html_parts), re.I)))
    return {"to": hdr.get("to", ""), "cc": hdr.get("cc", ""), "bcc": hdr.get("bcc", ""), "subject": hdr.get("subject", ""),
            "from": hdr.get("from", ""), "body": body, "html_body": html_text if gmail_body_sha(html_text) != gmail_body_sha(body) else "",
            "links": links, "attachments": attachments, "addresses": addresses, "body_sha": gmail_body_sha(body),
            "thread_id": message.get("threadId"), "send_check": digest.hexdigest()[:16]}


MAIL_SHOW_FLAGS = [Flag("draft-id", required=True, help="ID of the saved Gmail draft.")]


def cmd_mail_show(client: GoogleClient, a: dict[str, Any]) -> Any:
    """mail show: --draft-id. Prints the saved draft and the send check that send needs. Reads only."""
    view = gmail_draft_view(client.get(f"{GMAIL_API}/drafts/{a['draft_id']}", format="full"))
    lines = [f"Draft ID: {a['draft_id']}", f"To: {view['to']}"]
    lines += [f"{label}: {view[key]}" for label, key in (("Cc", "cc"), ("Bcc", "bcc")) if view[key]]
    lines.append(f"Subject: {view['subject']}")
    if view["attachments"]:
        lines.append("Attachments: " + ", ".join(view["attachments"]) + " (send refuses a draft with attachments)")
    if view["links"]:
        lines.append("Links: " + " ".join(view["links"]))
    lines += [f"Send check: {view['send_check']}", "Body:", view["body"]]
    if view["html_body"]:
        lines += ["Body (HTML part, differs from the plain part):", view["html_body"]]
    return {"result": "\n".join(lines), "send_check": view["send_check"], "to": view["to"], "cc": view["cc"], "bcc": view["bcc"],
            "subject": view["subject"], "body": view["body"], "html_body": view["html_body"], "links": view["links"],
            "attachments": view["attachments"]}


def gmail_sent_lookup(client: GoogleClient, want: dict[str, Any], since: float) -> str | None:
    """The id of a Sent message from `since` on with this subject, these recipients and this body,
    else None. Subject and recipients alone would match an earlier reply in the same thread."""
    subject = re.sub(r'["\\]', " ", str(want.get("subject") or "")).strip()
    query = f"in:sent after:{int(since) - 120}" + (f' subject:"{subject}"' if subject else "")
    listed = client.get(f"{GMAIL_API}/messages", q=query, maxResults=20).get("messages") or []
    for item in listed:
        msg = client.get(f"{GMAIL_API}/messages/{item.get('id')}", format="full")
        hdr = gmail_headers(msg.get("payload") or {})
        if int(msg.get("internalDate") or 0) < (since - 30) * 1000:
            continue
        if (hdr.get("subject", "") == want.get("subject") and hdr.get("to", "") == want.get("to")
                and gmail_body_sha(gmail_body(msg.get("payload") or {})) == want.get("body_sha")):
            return str(item.get("id"))
    return None


def gmail_draft_exists(client: GoogleClient, draft_id: str) -> bool:
    try:
        client.get(f"{GMAIL_API}/drafts/{draft_id}", format="minimal")
    except HttpError as exc:
        if exc.status == 404:
            return False
        raise
    return True


def gmail_log_row(guard: Any, row: dict[str, Any]) -> bool:
    """Dropbox can hold the file for a moment. Never lets a log failure hide a send's outcome."""
    for attempt in range(5):
        try:
            guard.log_send(row)
            return True
        except OSError:
            if attempt < 4:
                time.sleep(0.2)
    return False


MAIL_SEND_FLAGS = [
    Flag("draft-id", required=True, help="ID of the saved Gmail draft."),
    Flag("expect-hash", required=True, help="The Send check value show printed when the owner reviewed the draft."),
]


def cmd_mail_send(client: GoogleClient, a: dict[str, Any]) -> Any:
    """mail send: --draft-id --expect-hash. Sends a saved draft from the owner's account after the owner said send for that exact text. Only CoS or Launchpad, only with PM_OS_GMAIL_SEND=on. Exit 4 refused, 5 outcome unknown."""
    guard = gmail_guard()
    refusal = guard.surface_refusal()
    if refusal:
        steps = guard.next_send_steps(refusal) if hasattr(guard, "next_send_steps") else ""
        raise ShimError(f"refused: {refusal}. Nothing was sent. {steps}".rstrip(), SEND_REFUSED)
    draft_id = a["draft_id"]

    def outcome(state: str, want: dict[str, Any], message_id: str, logged: bool = True) -> dict[str, Any]:
        lines = [state, f"To: {want.get('to', '')}"] + ([f"Cc: {want['cc']}"] if want.get("cc") else [])
        lines += [f"Subject: {want.get('subject', '')}", f"Message ID: {message_id}"]
        if not logged:
            lines.append("The send log could not be written.")
        return {"result": "\n".join(lines), "message_id": message_id, "to": want.get("to", ""), "cc": want.get("cc", ""),
                "subject": want.get("subject", "")}

    def settle_earlier(prior: dict[str, Any], want: dict[str, Any]) -> dict[str, Any] | None:
        """An earlier send has no known outcome. Sent answers before anything else goes out."""
        began = float(prior.get("epoch") or 0)
        found = gmail_sent_lookup(client, want, began)
        if found:
            logged = gmail_log_row(guard, {**{k: want.get(k) for k in ("to", "cc", "subject", "send_check", "body_sha")},
                                           "draft_id": prior.get("draft_id"), "event": "sent-confirmed", "message_id": found})
            return outcome("Already sent by an earlier attempt, found in Sent. Nothing new was sent.", want, found, logged)
        if time.time() - began < guard.UNKNOWN_SETTLE_SECONDS:
            raise ShimError("unknown: an earlier send of this text has no known outcome and is not in Sent yet. "
                            "It was not retried. Check again in a few minutes.", SEND_UNKNOWN)
        return None

    try:
        draft = client.get(f"{GMAIL_API}/drafts/{draft_id}", format="full")
    except HttpError as exc:
        if exc.status != 404:
            raise
        prior = guard.unresolved_attempt(draft_id)
        if prior:  # a send that timed out can consume the draft, so a missing draft proves nothing
            settled = settle_earlier(prior, prior)
            if settled:
                return settled
            raise ShimError(f"unknown: draft {draft_id} is gone after a send with no known outcome, and the message is not "
                            "in Sent. Check Sent yourself. This command sent nothing.", SEND_UNKNOWN)
        raise ShimError(f"refused: draft {draft_id} does not exist, so it was already sent or deleted. This command sent nothing.", SEND_REFUSED)
    view = gmail_draft_view(draft)
    # Before any refusal: "nothing was sent" is only true once Sent has answered for an earlier attempt.
    prior = guard.unresolved_attempt(draft_id, view["send_check"])
    if prior:
        settled = settle_earlier(prior, prior)
        if settled:
            return settled
    if not view["addresses"]:
        raise ShimError(f"refused: draft {draft_id} has no recipients. Nothing was sent.", SEND_REFUSED)
    if view["send_check"] != a["expect_hash"]:
        raise ShimError(f"refused: draft {draft_id} changed since it was shown (send check is now {view['send_check']}). "
                        "Show it again and wait for the owner to say send. Nothing was sent.", SEND_REFUSED)
    if view["attachments"]:  # their content is not in the check, so this path cannot promise what the owner saw
        raise ShimError(f"refused: draft {draft_id} has attachments ({', '.join(view['attachments'])}). "
                        "Send it from Gmail. Nothing was sent.", SEND_REFUSED)
    refusal, turn = guard.approval(view)
    if refusal:
        steps = guard.next_send_steps(refusal) if hasattr(guard, "next_send_steps") else ""
        raise ShimError(f"refused: {refusal}. Nothing was sent. {steps}".rstrip(), SEND_REFUSED)
    row = {"draft_id": draft_id, "to": view["to"], "cc": view["cc"], "subject": view["subject"],
           "send_check": view["send_check"], "body_sha": view["body_sha"]}
    began = time.time()
    if not gmail_log_row(guard, {**row, "event": "attempt", "epoch": began, "turn": turn}):
        raise ShimError("refused: the send log is not writable. Nothing was sent.", SEND_REFUSED)
    try:
        sent = client.send("POST", f"{GMAIL_API}/drafts/send", {"id": draft_id})
        message_id = str(sent.get("id") or "")
        if not message_id:
            raise HttpError(200, "the reply carried no message id")
    except (HttpError, OSError, http.client.HTTPException) as exc:
        status = exc.status if isinstance(exc, HttpError) else 0
        if 400 <= status < 500:  # Google answered and refused, so nothing went out
            gmail_log_row(guard, {**row, "event": "failed", "status": status})
            raise ShimError(f"error: mail send: HTTP {status} {exc}. Nothing was sent.", 1)
        # A timeout, a dropped connection, a 5xx or an unreadable reply: the mail may have gone out.
        gmail_log_row(guard, {**row, "event": "unknown", "epoch": began, "reason": type(exc).__name__, "status": status})
        try:
            found = gmail_sent_lookup(client, view, began)
            consumed = not gmail_draft_exists(client, draft_id)
        except (HttpError, ShimError, OSError, http.client.HTTPException):
            found, consumed = None, False
        if found:
            logged = gmail_log_row(guard, {**row, "event": "sent-confirmed", "message_id": found})
            return outcome("Sent. The reply was lost, and the message is in Sent.", view, found, logged)
        where = ("The draft is gone but the message is not in Sent yet." if consumed
                 else "The message is not in Sent and the draft still exists.")
        raise ShimError(f"unknown: the send of draft {draft_id} did not report an outcome ({type(exc).__name__}). {where} "
                        "It was not retried. Check Sent before anything else; running send again checks Sent first.", SEND_UNKNOWN)
    logged = gmail_log_row(guard, {**row, "event": "sent", "message_id": message_id})
    return outcome("Sent.", view, message_id, logged)


MAIL_MODIFY_FLAGS = [
    Flag("message-ids", "list", required=True, help="Gmail message IDs to relabel."),
    Flag("labels-to-add", "list", help="Label names or IDs to apply."),
    Flag("labels-to-remove", "list", help="Label names or IDs to take off."),
]


def cmd_mail_modify(client: GoogleClient, a: dict[str, Any]) -> Any:
    """mail modify: --message-ids --labels-to-add --labels-to-remove."""
    add = gmail_resolve_labels(client, a.get("labels_to_add") or [])
    remove = gmail_resolve_labels(client, a.get("labels_to_remove") or [])
    if not add and not remove:
        raise ShimError("error: mail modify: provide --labels-to-add or --labels-to-remove", 2)
    ids = a["message_ids"]
    client.send("POST", f"{GMAIL_API}/messages/batchModify",
                {"ids": ids, "addLabelIds": add or None, "removeLabelIds": remove or None})
    text = (f"Modified {len(ids)} message(s): {', '.join(ids)}\nAdded labels: {', '.join(add) or '(none)'}\n"
            f"Removed labels: {', '.join(remove) or '(none)'}")
    return {"result": text}


MAIL_LABELS_FLAGS = [
    Flag("operation", required=True, choices=("create", "list_labels", "get"), help=""),
    Flag("label-id", help="Label ID, used by get."),
    Flag("label-name", help="Label name, used by create and get."),
]


def cmd_mail_labels(client: GoogleClient, a: dict[str, Any]) -> Any:
    """mail labels: --operation create|list_labels|get --label-id --label-name."""
    op = a["operation"]
    if op == "list_labels":
        labels = client.get(f"{GMAIL_API}/labels").get("labels") or []
        lines = [f"Labels ({len(labels)}):"] + [f"{lbl.get('name')} (ID: {lbl.get('id')}, type: {lbl.get('type')})"
                                               for lbl in sorted(labels, key=lambda l: str(l.get("name", "")).lower())]
        return {"result": "\n".join(lines), "labels": labels}
    if op == "create":
        if not a.get("label_name"):
            raise ShimError("error: mail labels: --label-name is required for create", 2)
        lbl = client.send("POST", f"{GMAIL_API}/labels", {"name": a["label_name"], "labelListVisibility": "labelShow",
                                                           "messageListVisibility": "show"})
        return {"result": f"Created label {lbl.get('name')} (ID: {lbl.get('id')})", "label": lbl}
    label_id = a.get("label_id")
    if not label_id:
        if not a.get("label_name"):
            raise ShimError("error: mail labels: --label-id or --label-name is required for get", 2)
        label_id = gmail_resolve_labels(client, [a["label_name"]])[0]
    lbl = client.get(f"{GMAIL_API}/labels/{label_id}")
    text = "\n".join(f"{k}: {v}" for k, v in sorted(lbl.items()) if not isinstance(v, (dict, list)))
    return {"result": text, "label": lbl}


# -------------------------------------------------------------------- calendar

CAL_PRESETS = ("yesterday", "today", "tomorrow", "this_week", "next_week")


def calendar_range(period: Any, tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    """`--time-period` is either a quoted preset ("today") or
    a {"time_min": ..., "time_max": ...} object. The wrapped form
    {"time_range": {"time_min", "time_max"}} (the shape the chief-of-staff
    skill documents) and {"preset": "today"} unwrap to those."""
    if isinstance(period, dict) and isinstance(period.get("time_range"), dict):
        period = period["time_range"]
    elif isinstance(period, dict) and isinstance(period.get("preset"), str) and not period.get("time_min"):
        period = period["preset"]
    if isinstance(period, dict):
        if not period.get("time_min") or not period.get("time_max"):
            raise ShimError("error: --time-period needs a preset string or {\"time_min\",\"time_max\"}", 2)
        lo, hi = parse_rfc3339(str(period["time_min"])), parse_rfc3339(str(period["time_max"]))
        if lo.tzinfo is None:
            lo = lo.replace(tzinfo=tz)
        if hi.tzinfo is None:
            hi = hi.replace(tzinfo=tz)
        return lo, hi
    if isinstance(period, str):
        name = period.strip().strip('"').lower()
        if name in CAL_PRESETS:
            today = dt.datetime.now(tz).date()
            if name == "today":
                start = today
            elif name == "yesterday":
                start = today - dt.timedelta(days=1)
            elif name == "tomorrow":
                start = today + dt.timedelta(days=1)
            elif name == "this_week":
                start = today - dt.timedelta(days=today.weekday())
            else:
                start = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7)
            days = 7 if name.endswith("week") else 1
            lo = dt.datetime.combine(start, dt.time.min, tzinfo=tz)
            return lo, lo + dt.timedelta(days=days)
    raise ShimError("error: --time-period must be one of " + ", ".join(f'"{p}"' for p in CAL_PRESETS)
                    + " or a JSON object with time_min and time_max", 2)


def event_local(value: dict[str, Any] | None, tz: dt.tzinfo) -> tuple[str | None, str | None]:
    """(time, date) for an event start or end."""
    if not value:
        return None, None
    if value.get("dateTime"):
        return parse_rfc3339(value["dateTime"]).astimezone(tz).replace(tzinfo=None).isoformat(), None
    return None, value.get("date")


def meeting_state(start: dict[str, Any] | None, end: dict[str, Any] | None, now: dt.datetime) -> str:
    try:
        s = parse_rfc3339(start["dateTime"]) if start and start.get("dateTime") else dt.datetime.combine(
            dt.date.fromisoformat(start["date"]), dt.time.min, tzinfo=now.tzinfo)
        e = parse_rfc3339(end["dateTime"]) if end and end.get("dateTime") else dt.datetime.combine(
            dt.date.fromisoformat(end["date"]), dt.time.min, tzinfo=now.tzinfo)
    except (KeyError, TypeError, ValueError):
        return "upcoming"
    if e <= now:
        return "completed"
    if s <= now < e:
        return "in progress"
    return "upcoming"


def event_row(ev: dict[str, Any], calendar_id: str, tz: dt.tzinfo, now: dt.datetime,
              attendees: bool = False) -> dict[str, Any]:
    start_time, start_date = event_local(ev.get("start"), tz)
    end_time, end_date = event_local(ev.get("end"), tz)
    row = {"attachments": [{"title": x.get("title"), "file_url": x.get("fileUrl")} for x in ev["attachments"]]
           if ev.get("attachments") else None,
           "calendar_id": calendar_id, "creator_email": (ev.get("creator") or {}).get("email"),
           "description": ev.get("description"), "end_date": end_date, "end_time": end_time,
           "hangout_link": ev.get("hangoutLink"), "html_link": ev.get("htmlLink"), "id": ev.get("id"),
           "meeting_state": meeting_state(ev.get("start"), ev.get("end"), now),
           "organizer_email": (ev.get("organizer") or {}).get("email"), "start_date": start_date,
           "start_time": start_time, "status": ev.get("status"), "summary": ev.get("summary")}
    if attendees:
        row["attendees"] = [{"email": x.get("email"), "display_name": x.get("displayName"),
                             "response_status": x.get("responseStatus"), "optional": bool(x.get("optional")),
                             "organizer": bool(x.get("organizer")), "self": bool(x.get("self"))}
                            for x in ev.get("attendees") or []]
        row["location"] = ev.get("location")
        row["recurring_event_id"] = ev.get("recurringEventId")
    return row


def md_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False) if value else ""
    return str(value).replace("|", "\\|")


def md_table(columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> str:
    """`| a | b |` header, `|-----|` separator, one line per row (list
    and events default output)."""
    head = "| " + " | ".join(label for label, _ in columns) + " |"
    sep = "|" + "|".join("-----" for _ in columns) + "|"
    body = ["| " + " | ".join(md_cell(row.get(key)) for _, key in columns) + " |" for row in rows]
    return "\n".join([head, sep, *body]) + "\n"


def pretty_time(value: str | None) -> str:
    if not value:
        return ""
    parsed = dt.datetime.fromisoformat(value)
    return parsed.strftime("%A, %B %d, %Y %I:%M%p")


def events_output(rows: list[dict[str, Any]], a: dict[str, Any], calendar_id: str, tz: dt.tzinfo) -> Any:
    if (a.get("output") or "markdown") == "json":
        return {"result": rows}
    if not rows:
        return {"result": f"No events foundCalendar ID: {calendar_id}\nTimezone: {tz_name(tz)}\n"}
    zone = tz_name(tz)
    columns = [("id", "id"), ("calendar_id", "calendar_id"), ("summary", "summary"), ("description", "description"),
               ("status", "status"), ("html_link", "html_link"), ("creator_email", "creator_email"),
               ("organizer_email", "organizer_email"), (f"start_time ({zone})", "start_time_pretty"),
               (f"end_time ({zone})", "end_time_pretty"), ("start_date", "start_date"), ("end_date", "end_date"),
               ("hangout_link", "hangout_link"), ("attachments", "attachments"), ("meeting_state", "meeting_state")]
    if a.get("include_attendees_info"):
        columns.append(("attendees", "attendees"))
    shaped = [{**row, "start_time_pretty": pretty_time(row.get("start_time")),
               "end_time_pretty": pretty_time(row.get("end_time"))} for row in rows]
    return {"result": md_table(columns, shaped)}


CAL_OUTPUT = Flag("output", default="markdown", choices=("markdown", "json"),
                  help="markdown for a readable table, json for scripts.")
CAL_TZ = Flag("timezone", help="IANA zone for times in the output. Blank means the machine's zone.")

CALENDAR_LIST_FLAGS = [CAL_OUTPUT]


def cmd_calendar_list(client: GoogleClient, a: dict[str, Any]) -> Any:
    """calendar list: --output markdown|json."""
    rows = []
    token = None
    while True:
        page = client.get(f"{CALENDAR_API}/users/me/calendarList", pageToken=token, maxResults=250)
        for c in page.get("items") or []:
            rows.append({"id": c.get("id"), "summary": c.get("summary"), "description": c.get("description") or "",
                         "time_zone": c.get("timeZone"), "access_role": c.get("accessRole"),
                         "primary": bool(c.get("primary")), "selected": bool(c.get("selected")),
                         "color_id": c.get("colorId"), "background_color": c.get("backgroundColor"),
                         "foreground_color": c.get("foregroundColor")})
        token = page.get("nextPageToken")
        if not token:
            break
    if (a.get("output") or "markdown") == "json":
        return {"result": rows}
    cols = [(k, k) for k in ("id", "summary", "description", "time_zone", "access_role", "primary", "selected",
                             "color_id", "background_color", "foreground_color")]
    return {"result": md_table(cols, rows)}


CALENDAR_EVENTS_FLAGS = [
    Flag("time-period", "json", required=True, help='A preset ("yesterday", "today", "tomorrow", "this_week", "next_week") or a {"time_min","time_max"} object.'),
    Flag("calendar-id", help="Calendar to read. Blank means the primary calendar."),
    Flag("event-ids", "list", help="Only these event IDs."),
    Flag("event-status", choices=("confirmed", "tentative", "cancelled", ""), help="Only events with this status."),
    Flag("include-attendees-info", "bool", help="Add the attendee list to each event."),
    Flag("meeting-state", default="all", choices=("completed", "upcoming", "in progress", "all"), help="Only past, upcoming, or running events."),
    Flag("rsvp-status", choices=("needsAction", "tentative", "declined", "accepted", ""), help="Only events where the owner's response matches."),
    CAL_OUTPUT, CAL_TZ,
]


def cal_fetch_events(client: GoogleClient, calendar_id: str, lo: dt.datetime, hi: dt.datetime,
                     q: str | None = None, max_results: int | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    token = None
    while True:
        page = client.get(f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id, safe='@')}/events",
                          timeMin=lo.isoformat(), timeMax=hi.isoformat(), singleEvents="true", orderBy="startTime",
                          maxResults=min(250, max_results) if max_results else 250, pageToken=token, q=q)
        events.extend(page.get("items") or [])
        token = page.get("nextPageToken")
        if not token or (max_results and len(events) >= max_results):
            break
    return events[:max_results] if max_results else events


def cal_filter(events: list[dict[str, Any]], a: dict[str, Any], now: dt.datetime) -> list[dict[str, Any]]:
    wanted_ids = set(a.get("event_ids") or [])
    kept = []
    for ev in events:
        if wanted_ids and ev.get("id") not in wanted_ids:
            continue
        if a.get("event_status") and ev.get("status") != a["event_status"]:
            continue
        state = a.get("meeting_state") or "all"
        if state != "all" and meeting_state(ev.get("start"), ev.get("end"), now) != state:
            continue
        if a.get("rsvp_status"):
            mine = next((x for x in ev.get("attendees") or [] if x.get("self")), None)
            if not mine or mine.get("responseStatus") != a["rsvp_status"]:
                continue
        kept.append(ev)
    return kept


def cmd_calendar_events(client: GoogleClient, a: dict[str, Any]) -> Any:
    """calendar events: --time-period --calendar-id
    --event-ids --event-status --include-attendees-info --meeting-state
    --rsvp-status --output --timezone."""
    tz = tzinfo_for(a.get("timezone"))
    now = dt.datetime.now(tz)
    calendar_id = a.get("calendar_id") or "primary"
    lo, hi = calendar_range(a.get("time_period"), tz)
    events = cal_filter(cal_fetch_events(client, calendar_id, lo, hi), a, now)
    label = calendar_id if calendar_id != "primary" else cal_primary_id(client)
    rows = [event_row(ev, label, tz, now, bool(a.get("include_attendees_info"))) for ev in events]
    return events_output(rows, a, label, tz)


def cal_primary_id(client: GoogleClient) -> str:
    try:
        return str(client.get(f"{CALENDAR_API}/calendars/primary").get("id") or "primary")
    except HttpError:
        return "primary"


CALENDAR_FIND_FLAGS = [
    Flag("query", required=True, help="Text to look for in event fields."),
    Flag("time-period", "json", required=True, help='A preset name or a {"time_min","time_max"} object.'),
    Flag("calendar-id", help="Calendar to search. Blank means the primary calendar."),
    Flag("max-results", "int", default=10, help="Most matches to return."),
    CAL_OUTPUT, CAL_TZ,
]


def cmd_calendar_find(client: GoogleClient, a: dict[str, Any]) -> Any:
    """calendar find: --query --time-period
    --calendar-id --max-results --output --timezone."""
    tz = tzinfo_for(a.get("timezone"))
    now = dt.datetime.now(tz)
    calendar_id = a.get("calendar_id") or "primary"
    lo, hi = calendar_range(a.get("time_period"), tz)
    events = cal_fetch_events(client, calendar_id, lo, hi, q=a["query"], max_results=int(a.get("max_results") or 10))
    label = calendar_id if calendar_id != "primary" else cal_primary_id(client)
    return events_output([event_row(ev, label, tz, now) for ev in events], a, label, tz)


CALENDAR_EVENT_FLAGS = [
    Flag("event-id", required=True, help="Event ID, or the event's Google Calendar link."),
    Flag("calendar-id", help="Calendar that holds the event. Blank means the primary calendar."),
    CAL_OUTPUT, CAL_TZ,
]


def cal_event_ref(value: str, calendar_id: str | None) -> tuple[str, str]:
    """Accept an event id or a calendar URL (`?eid=<base64 'eventId calendarId'>`)."""
    if "eid=" in value or value.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
        eid = (query.get("eid") or [""])[0] or value
        try:
            decoded = b64url_decode(eid).decode("utf-8", errors="replace")
        except Exception:
            raise ShimError("error: calendar: could not decode the event URL", 2)
        parts = decoded.split(" ")
        return parts[0], calendar_id or (parts[1] if len(parts) > 1 else "primary")
    return value, calendar_id or "primary"


def cmd_calendar_event(client: GoogleClient, a: dict[str, Any]) -> Any:
    """calendar event: --event-id --calendar-id --output --timezone."""
    tz = tzinfo_for(a.get("timezone"))
    event_id, calendar_id = cal_event_ref(str(a["event_id"]), a.get("calendar_id") or None)
    ev = client.get(f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id, safe='@')}/events/{event_id}")
    row = event_row(ev, calendar_id, tz, dt.datetime.now(tz), attendees=True)
    if (a.get("output") or "markdown") == "json":
        return {"result": row}
    lines = [f"{k}: {md_cell(v)}" for k, v in row.items() if v not in (None, "", [])]
    return {"result": "\n".join(lines) + "\n"}


def cal_mutation_flags(update: bool) -> list[Flag]:
    flags = [Flag("event-id", required=True, help="")] if update else []
    flags += [
        Flag("start-time", required=not update, help="Start as RFC 3339, for example 2026-01-15T09:00:00-08:00."),
        Flag("end-time", required=not update, help="End as RFC 3339."),
        Flag("title", required=not update, help=""),
        Flag("calendar-id" if update else "user-calendarID", default="primary" if update else "", help=""),
        Flag("description", help=""), Flag("location", help=""),
        Flag("color-id", help="Google Calendar color number: 1 Lavender, 2 Sage, 3 Grape, 4 Flamingo, 5 Banana, 6 Tangerine, 7 Peacock, 8 Graphite, 9 Blueberry, 10 Basil, 11 Tomato."),
        Flag("required-attendees", "list", help="Addresses of required guests."),
        Flag("optional-attendees", "list", help="Addresses of optional guests."),
        Flag("recurrence", "list", help="RRULE strings for a repeating event."),
        Flag("reminders", "json", help="Reminder list, for example [{\"method\": \"popup\", \"minutes\": 30}]."),
        Flag("send-updates", default="all", choices=("all", "externalOnly", "none"), help="Which guests get an email about the change."),
        Flag("transparency", help=""), Flag("visibility", help=""),
        Flag("guests-can-invite-others", "bool", help=""), Flag("guests-can-modify", "bool", help=""),
        Flag("guests-can-see-others", "bool", help=""),
        Flag("with-meet", "bool", default=None if update else True, help="Add a Google Meet link."),
        Flag("dry-run", "bool", help="Print the request that would be sent and stop."),
        Flag("timezone", help="IANA zone for the start and end, for example 'America/New_York'."),
    ]
    if update:
        flags += [Flag("merge-attendees", "bool", help="Add the given guests to the current list instead of replacing it."),
                  Flag("scope", default="single", choices=("single", "future", "all"), help="Which instances of a repeating event to change."),
                  Flag("original-start-time", help="Ignored here.")]
    return flags


def cal_event_body(a: dict[str, Any], tz: dt.tzinfo) -> dict[str, Any]:
    body: dict[str, Any] = {}
    zone = a.get("timezone") or tz_name(tz)
    if a.get("title") is not None:
        body["summary"] = a["title"]
    for key, field in (("description", "description"), ("location", "location"), ("color_id", "colorId"),
                       ("transparency", "transparency"), ("visibility", "visibility"),
                       ("guests_can_invite_others", "guestsCanInviteOthers"), ("guests_can_modify", "guestsCanModify"),
                       ("guests_can_see_others", "guestsCanSeeOtherGuests")):
        if a.get(key) is not None:
            body[field] = a[key]
    for key, field in (("start_time", "start"), ("end_time", "end")):
        if a.get(key):
            value = str(a[key])
            body[field] = {"date": value} if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else {"dateTime": value, "timeZone": zone}
    attendees = [{"email": e} for e in a.get("required_attendees") or []]
    attendees += [{"email": e, "optional": True} for e in a.get("optional_attendees") or []]
    if attendees:
        body["attendees"] = attendees
    if a.get("recurrence"):
        body["recurrence"] = a["recurrence"]
    if a.get("reminders") is not None:
        body["reminders"] = {"useDefault": False, "overrides": a["reminders"]}
    if a.get("with_meet"):
        body["conferenceData"] = {"createRequest": {"requestId": secrets.token_hex(8),
                                                    "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    return body


def cal_mutation_result(action: str, ev: dict[str, Any], tz: dt.tzinfo, calendar_id: str) -> dict[str, Any]:
    row = event_row(ev, calendar_id, tz, dt.datetime.now(tz), attendees=True)
    text = (f"Event {action}.\nID: {row.get('id')}\nSummary: {row.get('summary') or ''}\n"
            f"Start: {row.get('start_time') or row.get('start_date') or ''}\n"
            f"End: {row.get('end_time') or row.get('end_date') or ''}\nLink: {row.get('html_link') or ''}\n"
            f"Meet: {row.get('hangout_link') or ''}")
    return {"result": text, "event": row}


def cmd_calendar_create(client: GoogleClient, a: dict[str, Any]) -> Any:
    """calendar create: --title --start-time --end-time
    --user-calendarID --description --location --color-id --required-attendees
    --optional-attendees --recurrence --reminders --send-updates --transparency
    --visibility --guests-can-* --with-meet --dry-run --timezone."""
    tz = tzinfo_for(a.get("timezone"))
    calendar_id = a.get("user_calendarID") or a.get("user_calendarid") or "primary"
    body = cal_event_body(a, tz)
    params = {"sendUpdates": a.get("send_updates") or "all", "conferenceDataVersion": 1}
    url = f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id, safe='@')}/events"
    if a.get("dry_run"):
        return {"result": "Dry run: no event was created.", "request": {"method": "POST", "url": url, "params": params, "body": body}}
    ev = client.send("POST", url, body, params=params)
    return cal_mutation_result("created", ev, tz, calendar_id)


def cmd_calendar_update(client: GoogleClient, a: dict[str, Any]) -> Any:
    """calendar update: --event-id --calendar-id plus the
    create flags, --merge-attendees, --scope single|all (future is not supported)."""
    tz = tzinfo_for(a.get("timezone"))
    calendar_id = a.get("calendar_id") or "primary"
    event_id, calendar_id = cal_event_ref(str(a["event_id"]), calendar_id)
    scope = a.get("scope") or "single"
    if scope == "future":
        raise ShimError("error: calendar update: --scope future is not supported by the shim", 2)
    base = f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id, safe='@')}/events"
    current = client.get(f"{base}/{event_id}")
    if scope == "all" and current.get("recurringEventId"):
        event_id = current["recurringEventId"]
        current = client.get(f"{base}/{event_id}")
    body = cal_event_body(a, tz)
    if body.get("attendees") and a.get("merge_attendees"):
        known = {x.get("email") for x in current.get("attendees") or []}
        body["attendees"] = (current.get("attendees") or []) + [x for x in body["attendees"] if x["email"] not in known]
    if scope == "all" and ("start" in body or "end" in body):
        # Keep the series anchored: apply the new wall-clock time to the first instance's date.
        for field in ("start", "end"):
            if field in body and "dateTime" in body[field] and current.get(field, {}).get("dateTime"):
                first = parse_rfc3339(current[field]["dateTime"])
                new = parse_rfc3339(body[field]["dateTime"])
                body[field]["dateTime"] = dt.datetime.combine(first.date(), new.timetz()).isoformat()
    params = {"sendUpdates": a.get("send_updates") or "all", "conferenceDataVersion": 1}
    if a.get("dry_run"):
        return {"result": "Dry run: no event was updated.", "request": {"method": "PATCH", "url": f"{base}/{event_id}", "params": params, "body": body}}
    ev = client.send("PATCH", f"{base}/{event_id}", body, params=params)
    return cal_mutation_result("updated", ev, tz, calendar_id)


CALENDAR_DELETE_FLAGS = [
    Flag("event-id", required=True, help=""),
    Flag("calendar-id", default="primary", help=""),
    Flag("dry-run", "bool", help="Print the delete request that would be sent and stop."),
    Flag("scope", default="single", choices=("single", "future", "all"), help="Which instances of a repeating event to delete."),
    Flag("send-updates", choices=("all", "externalOnly", "none"), help="Which guests get an email about the deletion."),
]


def cmd_calendar_delete(client: GoogleClient, a: dict[str, Any]) -> Any:
    """calendar delete: --event-id --calendar-id --scope
    single|all --send-updates --dry-run."""
    event_id, calendar_id = cal_event_ref(str(a["event_id"]), a.get("calendar_id") or "primary")
    if (a.get("scope") or "single") == "future":
        raise ShimError("error: calendar delete: --scope future is not supported by the shim", 2)
    base = f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id, safe='@')}/events"
    if a.get("scope") == "all":
        current = client.get(f"{base}/{event_id}")
        event_id = current.get("recurringEventId") or event_id
    params = {"sendUpdates": a.get("send_updates")}
    if a.get("dry_run"):
        return {"result": "Dry run: no event was deleted.", "request": {"method": "DELETE", "url": f"{base}/{event_id}", "params": params}}
    client.send("DELETE", f"{base}/{event_id}", params=params)
    return {"result": f"Event deleted.\nID: {event_id}\nCalendar: {calendar_id}"}


# ----------------------------------------------------------------------- drive

def drive_file_id(value: str) -> str:
    text = str(value).strip()
    if not text.startswith("http"):
        return text
    m = re.search(r"/(?:d|folders|file/d)/([A-Za-z0-9_-]{10,})", text) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", text)
    if not m:
        raise ShimError(f"error: drive: could not find a file id in {text}", 2)
    return m.group(1)


def drive_urls(file_id: str, mime: str) -> dict[str, str]:
    if mime == MIME_DOC:
        base = f"https://docs.google.com/document/d/{file_id}"
    elif mime == MIME_SHEET:
        base = f"https://docs.google.com/spreadsheets/d/{file_id}"
    elif mime == MIME_SLIDES:
        base = f"https://docs.google.com/presentation/d/{file_id}"
    elif mime == MIME_FOLDER:
        url = f"https://drive.google.com/drive/folders/{file_id}"
        return {"edit": url, "preview": url, "web": url}
    else:
        return {"edit": f"https://drive.google.com/file/d/{file_id}/view", "preview": f"https://drive.google.com/file/d/{file_id}/preview",
                "web": f"https://drive.google.com/file/d/{file_id}/view"}
    return {"edit": base + "/edit", "preview": base + "/preview", "web": base + "/edit"}


def drive_file_row(f: dict[str, Any], urls: bool = False) -> dict[str, Any]:
    row = {"created_time": f.get("createdTime"), "export_links": f.get("exportLinks") or {}, "id": f.get("id"),
           "mime_type": f.get("mimeType"), "modified_time": f.get("modifiedTime"), "name": f.get("name"),
           "owners": f.get("owners") or [], "parents": f.get("parents") or [], "size": f.get("size"),
           "starred": bool(f.get("starred")), "thumbnail_link": f.get("thumbnailLink"), "trashed": bool(f.get("trashed")),
           "viewed_by_me_time": f.get("viewedByMeTime"), "web_view_link": f.get("webViewLink")}
    if f.get("shortcutDetails"):
        row["shortcut_details"] = f["shortcutDetails"]
    if urls:
        row["urls"] = drive_urls(str(f.get("id")), str(f.get("mimeType")))
    return row


DRIVE_QUERY_FLAGS = [
    Flag("raw-query", required=True, help="A Drive files.list query, passed through as the q parameter."),
    Flag("corpora", default="all_drives", choices=("user", "drive", "all_drives"), help="Where to search: your files, one shared drive, or every drive."),
    Flag("drive-id", help="Shared drive to search when --corpora is drive."),
    Flag("page-size", "int", default=10, help="Most files to return."),
    Flag("page-token", help="Token from an earlier raw search, to continue from that page."),
]


def drive_search_call(client: GoogleClient, q: str, corpora: str, drive_id: str, page_size: int,
                      page_token: str, order_by: str | None = None) -> dict[str, Any]:
    page = client.get(f"{DRIVE_API}/files", q=q, corpora={"all_drives": "allDrives", "user": "user", "drive": "drive"}[corpora],
                      driveId=drive_id or None, pageSize=page_size, pageToken=page_token or None, orderBy=order_by,
                      includeItemsFromAllDrives="true", supportsAllDrives="true",
                      fields=f"nextPageToken,files({DRIVE_FILE_FIELDS})")
    files = [drive_file_row(f) for f in page.get("files") or []]
    notes = ["Note:"]
    notes.append({"all_drives": "Results are shown for all your accessible drives.", "user": "Results are shown for your personal drive.",
                  "drive": f"Results are shown for shared drive {drive_id}."}[corpora])
    if len(files) >= page_size:
        notes.append(f"Only up to {page_size} results are shown. Try increasing the page_size parameter for more results.")
    return {"corpora": corpora, "drive_id": drive_id or "", "files": files, "next_page_token": page.get("nextPageToken") or "",
            "notes": notes, "page_size": page_size, "page_token": page_token or "", "query": q}


def cmd_drive_query(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive query: --raw-query --corpora --drive-id --page-size --page-token."""
    return drive_search_call(client, str(a["raw_query"]), a.get("corpora") or "all_drives", a.get("drive_id") or "",
                             int(a.get("page_size") or 10), a.get("page_token") or "")


DRIVE_SEARCH_FLAGS = [
    Flag("drive-type", required=True, choices=("file", "label", "drive"), help=""),
    Flag("query", help=""), Flag("field", help="Match on name or full_text."),
    Flag("mime-type", help="Only files of this MIME type."), Flag("parent", help="Only files in this folder ID."),
    Flag("owner", "json", help="'me', an address, or a list of them."),
    Flag("corpora", default="all_drives", choices=("user", "drive", "all_drives"), help="Where to search: your files, one shared drive, or every drive."),
    Flag("drive-id", help=""), Flag("page-size", "int", default=5, help="Most files to return."),
    Flag("modified-time-min", help=""), Flag("modified-time-max", help=""),
    Flag("created-time-min", help=""), Flag("created-time-max", help=""),
    Flag("viewed-by-me-time-min", help=""), Flag("viewed-by-me-time-max", help=""),
    Flag("order-by-field", default="viewedByMeTime", choices=("modifiedTime", "createdTime", "viewedByMeTime", "name_natural"), help=""),
    Flag("order-by-direction", default="desc", choices=("asc", "desc"), help=""),
    Flag("include-labels", "bool", help="Ignored here."), Flag("label-field", "json", help="Ignored here."),
    Flag("label-presence", "json", help="Ignored here."), Flag("visibility", help="Ignored here."),
]


def cmd_drive_search(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive search (deprecated): --drive-type file --query --field name|full_text
    --mime-type --parent --owner --corpora --drive-id --page-size --*-time-min/max
    --order-by-field --order-by-direction. Builds a files.list q expression."""
    if a["drive_type"] != "file":
        raise ShimError("error: drive search: only --drive-type file is supported by the shim", 2)
    terms = ["trashed = false"]
    query = str(a.get("query") or "").replace("'", "\\'")
    if query:
        terms.append(f"fullText contains '{query}'" if a.get("field") == "full_text" else f"name contains '{query}'")
    if a.get("mime_type"):
        terms.append(f"mimeType = '{a['mime_type']}'")
    if a.get("parent"):
        terms.append(f"'{a['parent']}' in parents")
    owners = a.get("owner")
    for owner in ([owners] if isinstance(owners, str) else owners or []):
        terms.append(f"'{owner}' in owners")
    for key, field in (("modified_time_min", "modifiedTime >"), ("modified_time_max", "modifiedTime <"),
                       ("created_time_min", "createdTime >"), ("created_time_max", "createdTime <"),
                       ("viewed_by_me_time_min", "viewedByMeTime >"), ("viewed_by_me_time_max", "viewedByMeTime <")):
        if a.get(key):
            terms.append(f"{field} '{a[key]}'")
    order = f"{a.get('order_by_field') or 'viewedByMeTime'} {a.get('order_by_direction') or 'desc'}"
    result = drive_search_call(client, " and ".join(terms), a.get("corpora") or "all_drives", a.get("drive_id") or "",
                               int(a.get("page_size") or 5), "", order_by=order)
    result["notes"].append("Deprecated: use raw_search for Drive file searches.")
    return result


def drive_metadata(client: GoogleClient, file_id: str) -> dict[str, Any]:
    return client.get(f"{DRIVE_API}/files/{file_id}", fields=DRIVE_FILE_FIELDS, supportsAllDrives="true")


def drive_export_text(client: GoogleClient, file_id: str, mime: str) -> str:
    return client.get_raw(f"{DRIVE_API}/files/{file_id}/export", mimeType=mime).text


def sheets_to_csv(client: GoogleClient, file_id: str, sheet_name: str | None) -> str:
    meta = client.get(f"{SHEETS_API}/{file_id}", fields="sheets.properties.title")
    titles = [s["properties"]["title"] for s in meta.get("sheets") or []]
    if sheet_name:
        if sheet_name not in titles:
            raise ShimError(f"error: drive read: sheet '{sheet_name}' not found (sheets: {', '.join(titles)})", 1)
        titles = [sheet_name]
    chunks = []
    for title in titles:
        values = client.get(f"{SHEETS_API}/{file_id}/values/{urllib.parse.quote(title, safe='')}").get("values") or []
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(values)
        chunks.append((f"## Sheet: {title}\n" if len(titles) > 1 or sheet_name is None else "") + buf.getvalue())
    return "\n".join(chunks)


DRIVE_READ_FLAGS = [
    Flag("id-or-url", required=True, help=""),
    Flag("output-format", default="json", choices=("json", "markdown"), help="For Google Docs: json gives plain text here, markdown gives markdown."),
    Flag("include-metadata", "bool", help="Print the file's Drive metadata next to its content."),
    Flag("mime-type", help="MIME type to read the file as. Taken from the file when left out."),
    Flag("sheet-name", help="For Google Sheets: the one tab to read."),
    Flag("range", "json", help="A {\"start\",\"end\"} slice of the text output."),
    Flag("include-images", "bool", help="Ignored here."),
    Flag("read-all-tabs", "bool", help="Ignored here: a Docs export already includes every tab."),
    Flag("tab-id", help="Ignored here."), Flag("tab-title", help="Ignored here."),
]


def cmd_drive_read(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive read: --id-or-url --output-format json|markdown --include-metadata
    --mime-type --sheet-name --range. Docs export as text or markdown, Sheets as
    CSV, Slides as text, folders as a listing, other files as text when decodable."""
    file_id = drive_file_id(str(a["id_or_url"]))
    meta = drive_metadata(client, file_id)
    mime = a.get("mime_type") or meta.get("mimeType") or ""
    if mime == MIME_DOC:
        content = drive_export_text(client, file_id, "text/markdown" if a.get("output_format") == "markdown" else "text/plain")
    elif mime == MIME_SHEET:
        content = sheets_to_csv(client, file_id, a.get("sheet_name") or None)
    elif mime == MIME_SLIDES:
        content = drive_export_text(client, file_id, "text/plain")
    elif mime == MIME_FOLDER:
        listing = drive_search_call(client, f"'{file_id}' in parents and trashed = false", "all_drives", "", 100, "")
        content = "\n".join(f"{f['name']}\t{f['mime_type']}\t{f['id']}" for f in listing["files"])
    else:
        raw = client.get_raw(f"{DRIVE_API}/files/{file_id}", alt="media", supportsAllDrives="true").body
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = "(binary content; use export for base64)\n" + base64.b64encode(raw[:4096]).decode()
    rng = a.get("range")
    if isinstance(rng, dict):
        content = content[int(rng.get("start") or 0):int(rng.get("end") or len(content))]
    if a.get("include_metadata"):
        return {"content": content, "file": drive_file_row(meta, urls=True), "mime_type": mime}
    return {"result": content}


DRIVE_INFO_FLAGS = [Flag("file-id-or-url", required=True, help="Drive file ID or link.")]


def cmd_drive_info(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive info: --file-id-or-url. Prints {"file": {..., "urls": {edit, preview, web}}}."""
    file_id = drive_file_id(str(a["file_id_or_url"]))
    return {"file": drive_file_row(drive_metadata(client, file_id), urls=True)}


def strip_quoted(comment: dict[str, Any]) -> dict[str, Any]:
    comment.pop("quotedFileContent", None)
    return comment


DRIVE_COMMENTS_FLAGS = [
    Flag("file-id-or-url", required=True, help="Drive file ID or link to list comments for."),
    Flag("all-pages", "bool", help="Keep paging until --max-comments is reached."),
    Flag("fail-empty", "bool", help="Exit with an error when the file has no comments."),
    Flag("include-quoted-content", "bool", default=True, help="Add the file text each comment is anchored to."),
    Flag("max-comments", "int", default=100, help="Most comments to return."),
    Flag("page-size", "int", default=100, help="Comments to request per API page."),
    Flag("page-token", help="Token from an earlier call, to continue from that page."),
]


def cmd_drive_comments(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive comments: --file-id-or-url --all-pages --fail-empty
    --include-quoted-content --max-comments --page-size --page-token. Comments
    keep the Drive API camelCase fields (author.me, createdTime, resolved, replies)."""
    file_id = drive_file_id(str(a["file_id_or_url"]))
    page_size = max(1, min(100, int(a.get("page_size") or 100)))
    max_comments = int(a.get("max_comments") or 100)
    comments: list[dict[str, Any]] = []
    token = a.get("page_token") or ""
    while True:
        page = client.get(f"{DRIVE_API}/files/{file_id}/comments", fields="*", pageSize=page_size, pageToken=token or None)
        comments.extend(page.get("comments") or [])
        token = page.get("nextPageToken") or ""
        if not token or not a.get("all_pages") or len(comments) >= max_comments:
            break
    comments = comments[:max_comments]
    if not a.get("include_quoted_content", True):
        comments = [strip_quoted(c) for c in comments]
    if a.get("fail_empty") and not comments:
        raise ShimError("error: drive comments: no comments found", 1)
    return {"comments": comments, "next_page_token": token, "page_size": page_size, "page_token": a.get("page_token") or ""}


DRIVE_COMMENT_FLAGS = [
    Flag("comment-id", required=True, help="ID of the comment."),
    Flag("file-id-or-url", required=True, help="Drive file ID or link that holds the comment."),
    Flag("include-quoted-content", "bool", default=True, help="Add the file text the comment is anchored to."),
]


def cmd_drive_comment(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive comment: --file-id-or-url --comment-id --include-quoted-content.
    Prints {"comment": {...}} (reply_check.py reads comment.author.me, replies, resolved)."""
    file_id = drive_file_id(str(a["file_id_or_url"]))
    comment = client.get(f"{DRIVE_API}/files/{file_id}/comments/{a['comment_id']}", fields="*")
    if not a.get("include_quoted_content", True):
        comment = strip_quoted(comment)
    return {"comment": comment}


DRIVE_COMMENT_ADD_FLAGS = [
    Flag("content", required=True, help="Text of the new comment."),
    Flag("file-id-or-url", required=True, help="Drive file ID or link to comment on."),
    Flag("include-quoted-content", "bool", default=True, help="Add the anchored file text to the printed result."),
]


def cmd_drive_comment_add(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive comment-add: --file-id-or-url --content --include-quoted-content."""
    file_id = drive_file_id(str(a["file_id_or_url"]))
    comment = client.send("POST", f"{DRIVE_API}/files/{file_id}/comments", {"content": a["content"]}, params={"fields": "*"})
    if not a.get("include_quoted_content", True):
        comment = strip_quoted(comment)
    return {"comment": comment}


DRIVE_COMMENT_REPLY_FLAGS = [
    Flag("comment-id", required=True, help="ID of the comment being answered."),
    Flag("content", required=True, help="Text of the reply."),
    Flag("file-id-or-url", required=True, help="Drive file ID or link that holds the thread."),
    Flag("resolve-comment", "bool", help="Mark the thread resolved with this reply."),
]


def cmd_drive_comment_reply(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive comment-reply: --file-id-or-url --comment-id --content --resolve-comment."""
    file_id = drive_file_id(str(a["file_id_or_url"]))
    body: dict[str, Any] = {"content": a["content"]}
    if a.get("resolve_comment"):
        body["action"] = "resolve"
    reply = client.send("POST", f"{DRIVE_API}/files/{file_id}/comments/{a['comment_id']}/replies", body, params={"fields": "*"})
    return {"comment_id": a["comment_id"], "reply": reply}


EXPORT_MIMES = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "csv": "text/csv", "text": "text/plain", "markdown": "text/markdown"}
DRIVE_EXPORT_FLAGS = [
    Flag("file-id-or-url", required=True, help="Drive file ID or link to download or convert."),
    Flag("export-format", default="original", choices=("original", "text", "markdown", "pdf", "docx", "xlsx", "pptx", "csv"), help="Format to return."),
    Flag("max-bytes", "int", default=250000, help="Size cap. A larger file fails rather than being cut."),
]


def cmd_drive_export(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive export: --file-id-or-url --export-format --max-bytes.
    Text formats return utf-8 content; binary formats return base64."""
    file_id = drive_file_id(str(a["file_id_or_url"]))
    meta = drive_metadata(client, file_id)
    fmt = a.get("export_format") or "original"
    mime = str(meta.get("mimeType") or "")
    if mime.startswith("application/vnd.google-apps."):
        target = EXPORT_MIMES.get(fmt) or {MIME_DOC: "text/plain", MIME_SHEET: "text/csv", MIME_SLIDES: "text/plain"}.get(mime, "application/pdf")
        body = client.get_raw(f"{DRIVE_API}/files/{file_id}/export", mimeType=target).body
    else:
        target = mime
        body = client.get_raw(f"{DRIVE_API}/files/{file_id}", alt="media", supportsAllDrives="true").body
    limit = int(a.get("max_bytes") or 250000)
    if len(body) > limit:
        raise ShimError(f"error: drive export: artifact is {len(body)} bytes, above max_bytes {limit}", 1)
    textual = target.startswith("text/") or target in ("application/json",)
    content = body.decode("utf-8", errors="replace") if textual else base64.b64encode(body).decode()
    return {"content": content, "encoding": "utf-8" if textual else "base64", "export_format": fmt, "file_id": file_id,
            "mime_type": target, "name": meta.get("name"), "size_bytes": len(body)}


DRIVE_PUT_FLAGS = [
    Flag("file-id", help="File to update. Leave out to create a new one."), Flag("name", help=""), Flag("mime-type", help="MIME type the stored file should have."),
    Flag("parent-id", help="Folder for a new file. My Drive's root when left out."),
    Flag("body", help="Text to upload. Use this or --binary-body-base64, not both."),
    Flag("binary-body-base64", help="Bytes to upload, base64-encoded."),
    Flag("body-file", help="Shim only: upload the contents of this local file."),
    Flag("source-mime-type", help="MIME type of the uploaded bytes. Markdown for Docs, CSV for Sheets, otherwise the target type."),
    Flag("replace-existing", "bool", help="When creating by name, overwrite a live file of that name in the same folder."),
    Flag("strip-markdown-frontmatter", "bool", help="Drop a leading YAML frontmatter block before uploading."),
    Flag("include-metadata", "bool", help="Print the file's Drive metadata after the write."),
    Flag("keep-revision-forever", "bool", help="Keep this revision from being pruned (keepRevisionForever)."),
    Flag("target-id", help="Target file ID when making a shortcut."), Flag("update-labels", "json", help="Ignored here."),
]


def cmd_drive_put(client: GoogleClient, a: dict[str, Any]) -> Any:
    """drive put: --file-id --name --mime-type --parent-id --body
    --binary-body-base64 --body-file (shim-only) --source-mime-type
    --replace-existing --strip-markdown-frontmatter --include-metadata
    --keep-revision-forever --target-id."""
    file_id = a.get("file_id") or ""
    name = a.get("name") or ""
    mime = a.get("mime_type") or ""
    content: bytes | None = None
    source_mime = a.get("source_mime_type") or ""
    if a.get("body_file"):
        path = Path(str(a["body_file"]))
        if not path.is_file():
            raise ShimError(f"error: drive put: --body-file not found: {path}", 2)
        content = path.read_bytes()
        if not name:
            name = path.name
        if not source_mime:
            source_mime = mimetypes.guess_type(path.name)[0] or ""
    elif a.get("binary_body_base64"):
        content = base64.b64decode(str(a["binary_body_base64"]))
    elif a.get("body") is not None and a.get("body") != "":
        content = str(a["body"]).encode("utf-8")
    if content is not None and a.get("strip_markdown_frontmatter"):
        text = content.decode("utf-8", errors="replace")
        content = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S).encode("utf-8")
    if not file_id and not name:
        raise ShimError("error: drive put: --name is required when creating", 2)
    if not source_mime and content is not None:
        source_mime = {MIME_DOC: "text/markdown", MIME_SHEET: "text/csv", MIME_SLIDES: "text/plain"}.get(mime) or mime or "text/plain"
    if not file_id and a.get("replace_existing") and name:
        safe = name.replace("'", "\\'")
        q = f"name = '{safe}' and trashed = false" + (f" and '{a['parent_id']}' in parents" if a.get("parent_id") else "")
        found = drive_search_call(client, q, "all_drives", "", 1, "")["files"]
        if found:
            file_id = str(found[0]["id"])
    metadata: dict[str, Any] = {}
    if name:
        metadata["name"] = name
    if mime:
        metadata["mimeType"] = mime
    if a.get("target_id"):
        metadata["mimeType"] = MIME_SHORTCUT
        metadata["shortcutDetails"] = {"targetId": a["target_id"]}
    params: dict[str, Any] = {"supportsAllDrives": "true", "fields": DRIVE_FILE_FIELDS}
    if file_id:
        if content is None:
            result = client.send("PATCH", f"{DRIVE_API}/files/{file_id}", metadata, params=params)
        else:
            params.update({"uploadType": "multipart", "keepRevisionForever": "true" if a.get("keep_revision_forever") else None})
            result = client.upload("PATCH", f"{DRIVE_UPLOAD_API}/files/{file_id}", metadata, content, source_mime, params=params)
        action = "updated"
    else:
        if a.get("parent_id"):
            metadata["parents"] = [a["parent_id"]]
        if content is None:
            result = client.send("POST", f"{DRIVE_API}/files", metadata, params=params)
        else:
            params["uploadType"] = "multipart"
            result = client.upload("POST", f"{DRIVE_UPLOAD_API}/files", metadata, content, source_mime, params=params)
        action = "created"
    row = drive_file_row(result, urls=True)
    out_payload: dict[str, Any] = {"action": action, "file": row} if a.get("include_metadata") else {
        "action": action, "id": row["id"], "mime_type": row["mime_type"], "name": row["name"], "web_view_link": row["web_view_link"]}
    return out_payload


# ----------------------------------------------------------------------- slack

class SlackClient:
    """Slack Web API with the stored token plus name/id resolvers and a small
    per-process cache for user and channel lookups."""

    def __init__(self, http: Http, auth: SlackAuth):
        self.http, self.auth = http, auth
        self._users: dict[str, dict[str, Any]] = {}
        self._channels: list[dict[str, Any]] | None = None
        self._channel_names: dict[str, str] = {}
        self._me: dict[str, Any] | None = None

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        # Form encoding works for every Web API method (search.messages and
        # users.list reject JSON bodies); lists and dicts travel as JSON strings.
        clean = {k: (json.dumps(v) if isinstance(v, (list, dict)) else str(v).lower() if isinstance(v, bool) else v)
                 for k, v in params.items() if v is not None and v != ""}
        body = urllib.parse.urlencode(clean).encode("utf-8")
        resp = self.http.call("POST", f"{SLACK_API}/{method}", headers=self.auth.headers(), data=body,
                              content_type="application/x-www-form-urlencoded; charset=utf-8")
        payload = resp.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            err = (payload or {}).get("error") if isinstance(payload, dict) else "unexpected response"
            raise SlackError(str(err), payload if isinstance(payload, dict) else {})
        return payload

    def me(self) -> dict[str, Any]:
        if self._me is None:
            self._me = self.call("auth.test")
        return self._me

    def user(self, user_id: str) -> dict[str, Any]:
        if user_id not in self._users:
            try:
                self._users[user_id] = self.call("users.info", user=user_id).get("user") or {"id": user_id}
            except SlackError:
                self._users[user_id] = {"id": user_id}
        return self._users[user_id]

    def user_row(self, u: dict[str, Any], deactivated: bool = False) -> dict[str, Any]:
        profile = u.get("profile") or {}
        row = {"display_name": profile.get("display_name") or u.get("name") or "", "email": profile.get("email"),
               "id": u.get("id"), "is_bot": bool(u.get("is_bot")), "real_name": u.get("real_name") or profile.get("real_name") or "",
               "username": u.get("name") or ""}
        if deactivated:
            row["is_deactivated"] = bool(u.get("deleted"))
        return row

    def all_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = self.call("users.list", limit=1000, cursor=cursor)
            users.extend(page.get("members") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        for u in users:
            self._users[str(u.get("id"))] = u
        return users

    def find_users(self, query: str, include_deactivated: bool = False, limit: int = 10) -> list[dict[str, Any]]:
        q = query.strip()
        if q.lower() == "me":
            return [self.user(str(self.me().get("user_id")))]
        if re.fullmatch(r"[UW][A-Z0-9]{6,}", q):
            return [self.user(q)]
        if "@" in q and not q.startswith("@"):
            try:
                return [self.call("users.lookupByEmail", email=q).get("user") or {}]
            except SlackError as exc:
                if exc.error == "users_not_found":
                    return []
                raise
        needle = q.lstrip("@").lower()
        hits = []
        for u in self.all_users():
            if u.get("deleted") and not include_deactivated:
                continue
            profile = u.get("profile") or {}
            names = [u.get("name"), u.get("real_name"), profile.get("display_name"), profile.get("real_name"), profile.get("email")]
            if any(needle in str(n or "").lower() for n in names):
                hits.append(u)
        exact = [u for u in hits if needle in {str(u.get("name") or "").lower(), str((u.get("profile") or {}).get("display_name") or "").lower()}]
        return (exact + [u for u in hits if u not in exact])[:limit]

    def resolve_user(self, selector: str) -> str:
        users = self.find_users(selector, limit=1)
        if not users or not users[0].get("id"):
            raise ShimError(f"error: User '{selector}' was not found.", 1)
        return str(users[0]["id"])

    def channels(self, types: str = "public_channel,private_channel,im,mpim", exclude_archived: bool = True) -> list[dict[str, Any]]:
        if self._channels is None:
            rows: list[dict[str, Any]] = []
            cursor = None
            while True:
                page = self.call("conversations.list", types=types, exclude_archived=exclude_archived, limit=1000, cursor=cursor)
                rows.extend(page.get("channels") or [])
                cursor = (page.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            self._channels = rows
            for c in rows:
                if c.get("name"):
                    self._channel_names[str(c["id"])] = str(c["name"])
        return self._channels

    def resolve_channel(self, selector: str) -> str:
        sel = selector.strip()
        if re.fullmatch(r"[CGD][A-Z0-9]{6,}", sel):
            return sel
        if sel.startswith("@"):
            return self.call("conversations.open", users=self.resolve_user(sel)).get("channel", {}).get("id", "")
        name = sel.lstrip("#").lower()
        for c in self.channels():
            if str(c.get("name") or "").lower() == name:
                return str(c["id"])
        raise ShimError(f"error: Channel '{selector}' was not found.", 1)

    def channel_name(self, channel_id: str) -> str:
        if channel_id in self._channel_names:
            return self._channel_names[channel_id]
        try:
            info = self.call("conversations.info", channel=channel_id).get("channel") or {}
        except SlackError:
            info = {}
        name = str(info.get("name") or "")
        if not name and info.get("is_im") and info.get("user"):
            name = self.user(str(info["user"])).get("name") or str(info["user"])
        self._channel_names[channel_id] = name or channel_id
        return self._channel_names[channel_id]


class SlackError(Exception):
    def __init__(self, error: str, payload: dict[str, Any]):
        super().__init__(error)
        self.error, self.payload = error, payload


def slack_time(ts: str, tz: dt.tzinfo) -> str:
    try:
        moment = dt.datetime.fromtimestamp(float(ts), tz)
    except (TypeError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M ") + (moment.tzname() or "")


def slack_message_row(client: SlackClient, m: dict[str, Any], tz: dt.tzinfo, include_blocks: bool = False,
                      thread_root: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"blocks_available": bool(m.get("blocks")), "text": m.get("text") or "",
                           "time": slack_time(str(m.get("ts") or ""), tz), "ts": m.get("ts")}
    if m.get("user"):
        u = client.user(str(m["user"]))
        row["user"] = {"id": u.get("id"), "is_bot": bool(u.get("is_bot")), "real_name": u.get("real_name") or "",
                       "username": u.get("name") or str(m["user"])}
    if m.get("bot_id"):
        row["bot_id"] = m["bot_id"]
    if m.get("subtype"):
        row["subtype"] = m["subtype"]
    if m.get("thread_ts"):
        row["thread_ts"] = m["thread_ts"]
        row["is_reply"] = m["thread_ts"] != m.get("ts")
    elif thread_root:
        row["thread_ts"] = thread_root
        row["is_reply"] = thread_root != m.get("ts")
    if m.get("reply_count"):
        row["reply_count"] = m["reply_count"]
    if m.get("reactions"):
        row["reactions"] = [{"name": r.get("name"), "count": r.get("count")} for r in m["reactions"]]
    if m.get("files"):
        row["files"] = [{"id": f.get("id"), "name": f.get("name"), "mimetype": f.get("mimetype")} for f in m["files"]]
    if include_blocks and m.get("blocks"):
        row["blocks"] = m["blocks"]
    return row


SLACK_OUTPUT = Flag("output-format", default="json", choices=("auto", "json"), help="Output is always JSON here.")
SLACK_TZ = Flag("timezone", help="IANA zone for printed times. The machine's zone by default.")

SLACK_POST_FLAGS = [
    Flag("text", required=True, help="Message text."),
    Flag("channel", help="Where to post: a channel name with or without #, or a Slack ID."),
    Flag("thread-ts", help="Timestamp of the thread's first message, to reply in that thread."),
    Flag("blocks", "json", help="Block Kit blocks. With blocks set, --text is the notification text."),
    Flag("attachments", "json", help="Legacy Slack attachments."),
    Flag("user", help="Person to DM: `@handle`, an email address, or a `U…` ID."),
    Flag("users", "list", help="Up to 8 people for a group DM."),
    Flag("dm-self", "bool", help="Post to your own DM."),
    Flag("reply-broadcast", "bool", help="Also show a thread reply in the channel."),
    Flag("unfurl-links", "bool", help="Turn link previews on or off for ordinary URLs."),
    Flag("unfurl-media", "bool", help="Turn previews on or off for media URLs."),
    Flag("post-at", "json", help="Send later, at unix seconds or an RFC 3339 time (chat.scheduleMessage)."),
]


def slack_target(client: SlackClient, a: dict[str, Any]) -> str:
    if a.get("dm_self"):
        return client.call("conversations.open", users=str(client.me().get("user_id"))).get("channel", {}).get("id", "")
    if a.get("users"):
        ids = ",".join(client.resolve_user(u) for u in a["users"][:8])
        return client.call("conversations.open", users=ids).get("channel", {}).get("id", "")
    if a.get("user"):
        return client.call("conversations.open", users=client.resolve_user(str(a["user"]))).get("channel", {}).get("id", "")
    if a.get("channel"):
        return client.resolve_channel(str(a["channel"]))
    raise ShimError("error: slack post: provide --channel, --user, --users or --dm-self", 2)


def cmd_slack_post(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack post: --text --channel --thread-ts --blocks --attachments
    --user --users --dm-self --reply-broadcast --unfurl-links --unfurl-media
    --post-at. Prints {"channel", "ts", "message": {"ts", "text"}}; slack-send.sh
    reads `.ts // .message.ts`. Plain `@user` / `#channel` text mentions are not
    rewritten by the shim; use `<@U...>` / `<#C...>` syntax."""
    if not str(a["text"]).strip() and not a.get("blocks") and not a.get("attachments"):
        raise ShimError("error: slack post: --text is empty", 2)
    channel = slack_target(client, a)
    params: dict[str, Any] = {"channel": channel, "text": a["text"], "thread_ts": a.get("thread_ts") or None,
                              "blocks": a.get("blocks"), "attachments": a.get("attachments"),
                              "reply_broadcast": a.get("reply_broadcast"), "unfurl_links": a.get("unfurl_links"),
                              "unfurl_media": a.get("unfurl_media")}
    if a.get("post_at") is not None:
        post_at = a["post_at"]
        if isinstance(post_at, str) and not post_at.isdigit():
            post_at = int(parse_rfc3339(post_at).timestamp())
        result = client.call("chat.scheduleMessage", post_at=int(post_at), **params)
        return {"channel": result.get("channel"), "post_at": result.get("post_at"),
                "scheduled_message_id": result.get("scheduled_message_id"), "text": a["text"]}
    result = client.call("chat.postMessage", **params)
    message = result.get("message") or {}
    row: dict[str, Any] = {"channel": result.get("channel"), "ts": result.get("ts"),
                           "message": {"text": message.get("text", a["text"]), "ts": message.get("ts") or result.get("ts")}}
    if message.get("thread_ts"):
        row["thread_ts"] = message["thread_ts"]
    try:
        row["permalink"] = client.call("chat.getPermalink", channel=result.get("channel"), message_ts=result.get("ts")).get("permalink")
    except (SlackError, HttpError):
        pass
    return row


SLACK_EDIT_FLAGS = [
    Flag("channel", required=True, help="Channel or DM that holds the message."),
    Flag("message-ts", required=True, help="The message's `ts` value."),
    Flag("text", help="New message text."),
    Flag("blocks", "json", help="New Block Kit blocks; `[]` removes them."),
    Flag("attachments", "json", help="New legacy attachments; `[]` removes them."),
]


def cmd_slack_edit(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack edit: --channel --message-ts --text --blocks --attachments."""
    if a.get("text") in (None, "") and a.get("blocks") is None and a.get("attachments") is None:
        raise ShimError("error: slack edit: provide at least one of --text, --blocks, --attachments", 2)
    channel = client.resolve_channel(str(a["channel"]))
    result = client.call("chat.update", channel=channel, ts=a["message_ts"], text=a.get("text") or None,
                         blocks=a.get("blocks"), attachments=a.get("attachments"))
    return {"channel": result.get("channel"), "text": result.get("text"), "ts": result.get("ts")}


SLACK_SEARCH_FLAGS = [
    Flag("query", required=True, help="Words to search for."),
    Flag("from-user", help="Who sent it: `me`, an email address, a Slack ID, or `@handle`."),
    Flag("with-user", help="Person whose DMs to search."),
    Flag("channels", "list", help="Only these channels or DMs."),
    Flag("channel-types", "list", help="Ignored here: search.messages has no type filter."),
    Flag("content-types", "list", help="Ignored here: only messages are searched."),
    Flag("after-date", help="Only results after this date."), Flag("before-date", help="Only results before this date."),
    Flag("after-ts", "int", help="Lower bound as unix seconds."), Flag("before-ts", "int", help="Upper bound as unix seconds."),
    Flag("on", help="Only results from one date, or `today` / `yesterday`."),
    Flag("newer-than", help="Only results within an ISO 8601 duration, for example P7D."),
    Flag("limit", "int", default=20, help="Most results to return."),
    Flag("cursor", help="Page number from an earlier response."),
    Flag("sort", default="timestamp", choices=("timestamp", "score"), help="Order by time or by relevance."),
    Flag("sort-dir", default="desc", choices=("asc", "desc"), help="Ascending or descending."),
    Flag("search-api", default="legacy", choices=("rts", "legacy"), help="Ignored here: search.messages is always used."),
    Flag("include-context", "bool", help="Ignored here."),
    SLACK_OUTPUT, SLACK_TZ,
]


def slack_search_query(client: SlackClient, a: dict[str, Any], tz: dt.tzinfo) -> str:
    parts = [str(a["query"]).strip()]
    if a.get("from_user"):
        parts.append(f"from:<@{client.resolve_user(str(a['from_user']))}>")
    if a.get("with_user"):
        parts.append(f"with:<@{client.resolve_user(str(a['with_user']))}>")
    for ch in a.get("channels") or []:
        parts.append(f"in:<#{client.resolve_channel(str(ch))}>")
    today = dt.datetime.now(tz).date()
    if a.get("newer_than"):
        since = dt.datetime.now(tz) - parse_iso_duration(str(a["newer_than"]))
        parts.append("after:" + (since.date() - dt.timedelta(days=1)).isoformat())
    if a.get("after_date"):
        parts.append("after:" + parse_day(str(a["after_date"]), tz).date().isoformat())
    if a.get("before_date"):
        parts.append("before:" + parse_day(str(a["before_date"]), tz).date().isoformat())
    if a.get("after_ts") is not None:
        parts.append("after:" + (dt.datetime.fromtimestamp(int(a["after_ts"]), tz).date() - dt.timedelta(days=1)).isoformat())
    if a.get("before_ts") is not None:
        parts.append("before:" + (dt.datetime.fromtimestamp(int(a["before_ts"]), tz).date() + dt.timedelta(days=1)).isoformat())
    if a.get("on"):
        day = parse_day(str(a["on"]), tz).date() if str(a["on"]).lower() in ("today", "yesterday", "tomorrow") else dt.date.fromisoformat(str(a["on"])[:10])
        parts.append("on:" + day.isoformat())
    _ = today
    return " ".join(p for p in parts if p)


def cmd_slack_search(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack search: --query --from-user --with-user --channels
    --after-date --before-date --after-ts --before-ts --on --newer-than --limit
    --cursor --sort --sort-dir --timezone. Uses search.messages (user token with
    search:read). Prints {"has_more", "messages": [{channel: {id, name}, permalink,
    text, thread_ts, ts, time, user: {username, is_bot}}], "next_cursor", "query", "summary"}."""
    tz = tzinfo_for(a.get("timezone"))
    query = slack_search_query(client, a, tz)
    page = int(a["cursor"]) if str(a.get("cursor") or "").isdigit() else 1
    limit = max(1, min(100, int(a.get("limit") or 20)))
    result = client.call("search.messages", query=query, count=limit, page=page, sort=a.get("sort") or "timestamp",
                         sort_dir=a.get("sort_dir") or "desc", highlight=False)
    matches = (result.get("messages") or {}).get("matches") or []
    paging = (result.get("messages") or {}).get("paging") or {}
    rows = []
    lower = a.get("after_ts")
    upper = a.get("before_ts")
    for m in matches:
        ts = str(m.get("ts") or "")
        if lower is not None and ts and float(ts) < float(lower):
            continue
        if upper is not None and ts and float(ts) > float(upper):
            continue
        permalink = str(m.get("permalink") or "")
        thread_ts = (urllib.parse.parse_qs(urllib.parse.urlparse(permalink).query).get("thread_ts") or [None])[0]
        channel = m.get("channel") or {}
        row: dict[str, Any] = {"blocks_available": bool(m.get("blocks")),
                               "channel": {"id": channel.get("id"), "name": channel.get("name") or client.channel_name(str(channel.get("id") or ""))},
                               "is_reply": bool(thread_ts and thread_ts != ts), "permalink": permalink, "text": m.get("text") or "",
                               "time": slack_time(ts, tz), "ts": ts,
                               "user": {"id": m.get("user"), "is_bot": m.get("type") == "bot_message" or bool(m.get("bot_id")),
                                        "username": m.get("username") or ""}}
        if thread_ts:
            row["thread_ts"] = thread_ts
        rows.append(row)
    total_pages = int(paging.get("pages") or 1)
    has_more = page < total_pages
    return {"has_more": has_more, "messages": rows, "next_cursor": str(page + 1) if has_more else "", "query": query,
            "summary": f"Found {len(rows)} message{'s' if len(rows) != 1 else ''}."}


SLACK_HISTORY_FLAGS = [
    Flag("channels", "list", required=True, help="Channels or DMs to read."),
    Flag("limit", "int", default=50, help="Messages per channel."),
    Flag("cursor", help="The `next_cursor` value from an earlier call."),
    Flag("after-date", help="Only messages after this date."), Flag("before-date", help="Only messages before this date."),
    Flag("on", help="Only messages from one date, or `today` / `yesterday`."),
    Flag("newer-than", help="Only messages within an ISO 8601 duration."),
    Flag("include-blocks", "bool", help="Add each message's raw Block Kit `blocks`."),
    SLACK_OUTPUT, SLACK_TZ,
]


def slack_window(a: dict[str, Any], tz: dt.tzinfo) -> tuple[str | None, str | None]:
    oldest: dt.datetime | None = None
    latest: dt.datetime | None = None
    if a.get("newer_than"):
        oldest = dt.datetime.now(tz) - parse_iso_duration(str(a["newer_than"]))
    if a.get("after_date"):
        oldest = parse_day(str(a["after_date"]), tz)
    if a.get("before_date"):
        latest = parse_day(str(a["before_date"]), tz)
    if a.get("on"):
        oldest, latest = parse_day(str(a["on"]), tz), parse_day(str(a["on"]), tz, end=True)
    return (f"{oldest.timestamp():.6f}" if oldest else None, f"{latest.timestamp():.6f}" if latest else None)


def cmd_slack_history(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack history: --channels --limit --cursor --after-date --before-date
    --on --newer-than --include-blocks --timezone. Top-level messages only."""
    tz = tzinfo_for(a.get("timezone"))
    oldest, latest = slack_window(a, tz)
    limit = max(1, min(1000, int(a.get("limit") or 50)))
    results = []
    for selector in a["channels"][:20]:
        channel = client.resolve_channel(str(selector))
        page = client.call("conversations.history", channel=channel, limit=limit, cursor=a.get("cursor") or None,
                           oldest=oldest, latest=latest, inclusive=True)
        msgs = [m for m in page.get("messages") or [] if not m.get("thread_ts") or m.get("thread_ts") == m.get("ts")]
        results.append({"channel": {"id": channel, "name": client.channel_name(channel)}, "has_more": bool(page.get("has_more")),
                        "messages": [slack_message_row(client, m, tz, bool(a.get("include_blocks"))) for m in msgs],
                        "messages_requested": limit, "messages_returned": len(msgs),
                        "next_cursor": (page.get("response_metadata") or {}).get("next_cursor") or "", "timezone": tz_name(tz)})
    return {"result_count": len(results), "results": results}


SLACK_THREAD_FLAGS = [
    Flag("channel", required=True, help="Channel or DM that holds the thread."),
    Flag("thread-ts", required=True, help="Timestamp of the thread's first message."),
    Flag("limit", "int", default=200, help="Most thread messages to return."),
    Flag("cursor", help="The `next_cursor` value from an earlier call."),
    Flag("include-blocks", "bool", help="Add each message's raw Block Kit `blocks`."),
    SLACK_OUTPUT, SLACK_TZ,
]


def cmd_slack_thread(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack thread: --channel --thread-ts --limit --cursor --include-blocks
    --timezone. First message is the starter; replies carry `is_reply: true`."""
    tz = tzinfo_for(a.get("timezone"))
    channel = client.resolve_channel(str(a["channel"]))
    page = client.call("conversations.replies", channel=channel, ts=a["thread_ts"], limit=max(1, min(1000, int(a.get("limit") or 200))),
                       cursor=a.get("cursor") or None)
    msgs = page.get("messages") or []
    return {"channel": {"id": channel, "name": client.channel_name(channel)}, "has_more": bool(page.get("has_more")),
            "messages": [slack_message_row(client, m, tz, bool(a.get("include_blocks")), thread_root=str(a["thread_ts"])) for m in msgs],
            "next_cursor": (page.get("response_metadata") or {}).get("next_cursor") or "", "thread_ts": a["thread_ts"],
            "timezone": tz_name(tz)}


SLACK_PEOPLE_FLAGS = [
    Flag("query", required=True, help="A Slack ID, email address, handle, `me`, or part of a name."),
    Flag("include-deactivated", "bool", help="Also return deactivated accounts."),
    Flag("limit", "int", default=10, help="Most people to return."),
    SLACK_OUTPUT,
]


def cmd_slack_people(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack people: --query --include-deactivated --limit. Prints
    {"query", "results": [{display_name, email, id, is_bot, real_name, username}]}."""
    users = client.find_users(str(a["query"]), bool(a.get("include_deactivated")), int(a.get("limit") or 10))
    return {"query": a["query"], "results": [client.user_row(u, bool(a.get("include_deactivated"))) for u in users if u.get("id")]}


SLACK_RESOLVE_FLAGS = [
    Flag("name", required=True, help="User or channel to look up, with or without `@` or `#`."),
    Flag("entity-type", choices=("user", "channel"), help="Force user or channel. A leading `@` means user; anything else means channel."),
    SLACK_OUTPUT,
]


def cmd_slack_resolve(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack resolve: --name --entity-type user|channel."""
    name = str(a["name"]).strip()
    kind = a.get("entity_type") or ("user" if name.startswith("@") or ("@" in name and "." in name) else "channel")
    if kind == "user":
        users = client.find_users(name, limit=1)
        if not users or not users[0].get("id"):
            raise ShimError(f"error: User '{name}' was not found.", 1)
        u = users[0]
        return {"id": u.get("id"), "is_bot": bool(u.get("is_bot")), "name": u.get("name"), "real_name": u.get("real_name") or "", "type": "user"}
    channel_id = client.resolve_channel(name)
    info = next((c for c in client.channels() if c.get("id") == channel_id), {})
    return {"id": channel_id, "is_private": bool(info.get("is_private")), "name": info.get("name") or client.channel_name(channel_id), "type": "channel"}


SLACK_WHOAMI_FLAGS = [Flag("user", default="me", help="Whose profile to read. `me` by default."), SLACK_OUTPUT]


def cmd_slack_whoami(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack whoami: --user. Prints {"profile": {...}, "user": {...}} with
    best-effort DND fields."""
    user_id = client.resolve_user(str(a.get("user") or "me"))
    u = client.call("users.info", user=user_id).get("user") or {}
    row = client.user_row(u)
    row.update({"tz": u.get("tz"), "tz_label": u.get("tz_label"), "tz_offset": u.get("tz_offset"),
                "title": (u.get("profile") or {}).get("title"), "is_admin": bool(u.get("is_admin"))})
    try:
        dnd = client.call("dnd.info", user=user_id)
        row.update({"dnd_enabled": dnd.get("dnd_enabled"), "next_dnd_start_ts": dnd.get("next_dnd_start_ts"),
                    "next_dnd_end_ts": dnd.get("next_dnd_end_ts"), "snooze_enabled": dnd.get("snooze_enabled"),
                    "snooze_endtime": dnd.get("snooze_endtime")})
    except (SlackError, HttpError) as exc:
        row["dnd_status_error"] = str(exc)
    return {"profile": u.get("profile") or {}, "user": row}


SLACK_CHANNELS_FLAGS = [
    Flag("query", help="Text to match in channel names. Empty lists everything."),
    Flag("conversation-types", "list", default=["public_channel", "private_channel", "im", "mpim"], help="Kinds of conversation to include."),
    Flag("exclude-archived", "bool", default=True, help="Skip archived channels."),
    Flag("limit", "int", default=50, help="Most channels to return."),
    Flag("cursor", help="Offset from an earlier response."),
    SLACK_OUTPUT,
]


def cmd_slack_channels(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack channels: --query --conversation-types --exclude-archived --limit --cursor."""
    types = ",".join(a.get("conversation_types") or ["public_channel", "private_channel", "im", "mpim"])
    rows = client.channels(types=types, exclude_archived=bool(a.get("exclude_archived", True)))
    needle = str(a.get("query") or "").lstrip("#").lower()
    if needle:
        rows = [c for c in rows if needle in str(c.get("name") or "").lower()]
        rows.sort(key=lambda c: (str(c.get("name") or "").lower() != needle, str(c.get("name") or "")))
    offset = int(a["cursor"]) if str(a.get("cursor") or "").isdigit() else 0
    limit = max(1, int(a.get("limit") or 50))
    page = rows[offset:offset + limit]
    has_more = offset + limit < len(rows)
    results = []
    for c in page:
        kind = "im" if c.get("is_im") else "mpim" if c.get("is_mpim") else "private_channel" if c.get("is_private") else "public_channel"
        results.append({"id": c.get("id"), "is_private": bool(c.get("is_private")), "name": c.get("name") or c.get("user") or "",
                        "num_members": c.get("num_members"), "purpose": (c.get("purpose") or {}).get("value", ""),
                        "topic": (c.get("topic") or {}).get("value", ""), "type": kind})
    return {"has_more": has_more, "next_cursor": str(offset + limit) if has_more else "", "query": a.get("query") or "", "results": results}


SLACK_REACT_FLAGS = [
    Flag("channel", required=True, help="Channel or DM that holds the message."),
    Flag("emoji", required=True, help="Emoji name with no colons, for example `white_check_mark`."),
    Flag("message-ts", required=True, help="The message's `ts` value."),
]


def cmd_slack_react(client: SlackClient, a: dict[str, Any]) -> Any:
    """slack react: --channel --emoji --message-ts. Idempotent: an existing
    reaction returns was_already_present: true."""
    channel = client.resolve_channel(str(a["channel"]))
    emoji = str(a["emoji"]).strip(":")
    present = False
    try:
        client.call("reactions.add", channel=channel, name=emoji, timestamp=a["message_ts"])
    except SlackError as exc:
        if exc.error != "already_reacted":
            raise
        present = True
    return {"channel": channel, "emoji": emoji, "message_ts": a["message_ts"], "was_already_present": present}


# ------------------------------------------------------------------- registry

Handler = Callable[[Any, dict[str, Any]], Any]

# -------------------------------------------------------------------- dropbox

class DropboxError(Exception):
    def __init__(self, summary: str):
        super().__init__(summary)
        self.summary = summary


class DropboxClient:
    def __init__(self, http: Http, auth: DropboxAuth):
        self.http, self.auth = http, auth

    def rpc(self, endpoint: str, body: Any = None) -> Any:
        """One RPC call. Dropbox reports a route error as HTTP 409 with `error_summary`."""
        kwargs: dict[str, Any] = {"json_body": body} if body is not None else {"data": b"null", "content_type": "application/json"}
        resp = self.http.call("POST", f"{DROPBOX_API}/{endpoint}", headers=self.auth.headers(),
                              ok_statuses=(200, 409), **kwargs)
        payload = resp.json()
        if resp.status == 409:
            raise DropboxError(str(payload.get("error_summary") or "conflict").rstrip("/."))
        return payload

    def list_links(self, path: str) -> list[dict[str, Any]]:
        return self.rpc("sharing/list_shared_links", {"path": path, "direct_only": True}).get("links") or []


def dropbox_roots() -> list[str]:
    """Local Dropbox folders, from PM_OS_DROPBOX_ROOT or the desktop app's info.json."""
    if os.environ.get("PM_OS_DROPBOX_ROOT"):
        return [os.environ["PM_OS_DROPBOX_ROOT"]]
    places = [Path(os.environ[v]) / "Dropbox" / "info.json" for v in ("LOCALAPPDATA", "APPDATA") if os.environ.get(v)]
    places.append(Path.home() / ".dropbox" / "info.json")
    roots: list[str] = []
    for place in places:
        for account in (read_json_file(place) or {}).values():
            if isinstance(account, dict) and account.get("path") and account["path"] not in roots:
                roots.append(account["path"])
    return roots


def dropbox_api_path(value: str, roots: list[str] | None = None) -> str:
    """Turn a local path under the Dropbox folder into the API's `/a/b.pdf` form.
    A leading-slash path outside every root is taken as already being a Dropbox path."""
    original = value.strip()
    if not original:
        raise ShimError("error: dropbox: --path is empty", 2)
    roots = dropbox_roots() if roots is None else roots
    if len(roots) > 1:  # the token belongs to one account, and /a.pdf can exist in both
        raise ShimError("error: dropbox: several Dropbox accounts on this machine. Set PM_OS_DROPBOX_ROOT to the one "
                        "the token belongs to.", 2)
    git_bash = os.name == "nt" and re.match(r"^/[A-Za-z]/", original)
    raw = f"{original[1]}:{original[2:]}" if git_bash else original
    # On Windows a leading slash with no drive is a Dropbox path, even when C:\Dropbox would match it.
    if not original.startswith("/") or git_bash or os.name != "nt":
        full = os.path.abspath(raw)
        for root in roots:
            base = os.path.abspath(root)
            try:
                inside = os.path.normcase(os.path.commonpath([full, base])) == os.path.normcase(base)
            except ValueError:  # another drive
                continue
            if inside:
                if not os.path.isfile(full):  # a link to a folder would publish everything in it
                    raise ShimError(f"error: dropbox: {value} is not a file", 2)
                return "/" + os.path.relpath(full, base).replace(os.sep, "/")
    if not original.startswith("/"):
        raise ShimError(f"error: dropbox: {value} is not inside the Dropbox folder", 2)
    for root in roots:
        if os.path.isdir(os.path.join(root, original.lstrip("/"))) or not original.strip("/"):
            raise ShimError(f"error: dropbox: {value} is not a file", 2)
    return original


WHO_CAN_OPEN = {
    "public": "anyone with the link",
    "team": "members of your Dropbox team with the link",
    "team_only": "members of your Dropbox team with the link",
    "password": "anyone with the link and its password",
    "team_and_password": "team members with the link and its password",
    "members": "only people the file or folder is shared with",
    "shared_folder_only": "only people the file or folder is shared with",
    "only_you": "only you",
    "no_one": "only you",
}


def dropbox_link_row(link: dict[str, Any], existed: bool) -> dict[str, Any]:
    perms = link.get("link_permissions") or {}

    def tag(key: str) -> str | None:
        return (perms.get(key) or {}).get(".tag")

    audience = tag("effective_audience") or tag("resolved_visibility")
    who = WHO_CAN_OPEN.get(audience or "", f"unknown ({audience})")
    if perms.get("require_password") and audience == "public":
        who = WHO_CAN_OPEN["password"]
    if tag("link_access_level") not in (None, "viewer"):
        who += ", and they can edit it"
    return {"url": link.get("url"), "name": link.get("name"), "path": link.get("path_lower"), "existed": existed,
            "audience": audience, "access_level": tag("link_access_level"), "who_can_open": who,
            "require_password": bool(perms.get("require_password")), "expires": link.get("expires")}


def dropbox_link_usable(link: dict[str, Any]) -> bool:
    if link.get(".tag") != "file":
        return False
    perms = link.get("link_permissions") or {}
    if (perms.get("effective_audience") or perms.get("resolved_visibility") or {}).get(".tag") in ("no_one", "only_you"):
        return False
    if link.get("expires"):
        try:
            return parse_rfc3339(link["expires"]) > dt.datetime.now(dt.timezone.utc)
        except ValueError:
            return False
    return True


def dropbox_pick_link(links: list[dict[str, Any]], path: str) -> dict[str, Any]:
    """A file can hold a view link and an edit link. Never hand out the edit one when a view one exists."""
    usable = [link for link in links if dropbox_link_usable(link)]
    if not usable:
        raise ShimError(f"error: dropbox link: {path}: its existing links are expired, private or not "
                        f"a file link, so none was used", 1)
    for link in usable:
        if ((link.get("link_permissions") or {}).get("link_access_level") or {}).get(".tag") == "viewer":
            return link
    return usable[0]


def dropbox_create_link(client: DropboxClient, path: str) -> dict[str, Any]:
    """These scopes cannot stat a Dropbox path, so a folder is only caught here. Take its link straight back."""
    link = client.rpc("sharing/create_shared_link_with_settings", {"path": path})
    if link.get(".tag") == "file":
        return link
    try:
        client.rpc("sharing/revoke_shared_link", {"url": link.get("url")})
        state = "the link was revoked"
    except (DropboxError, HttpError) as exc:
        state = f"revoking it FAILED, remove it by hand: {link.get('url')} ({exc})"
    raise ShimError(f"error: dropbox link: {path} is not a file; {state}", 1)


def cmd_dropbox_link(client: DropboxClient, a: dict[str, Any]) -> Any:
    """Return a file's shared link, creating one with the account's default settings when none exists."""
    path = dropbox_api_path(a["path"])
    deadline = time.time() + max(0, int(a.get("wait_seconds") or 0))
    while True:
        try:
            links = client.list_links(path)
            if links:
                return dropbox_link_row(dropbox_pick_link(links, path), True)
            if a.get("existing_only"):
                return {"url": None, "path": path, "existed": False, "who_can_open": None}
            try:
                return dropbox_link_row(dropbox_create_link(client, path), False)
            except DropboxError as exc:
                if not exc.summary.startswith("shared_link_already_exists"):
                    raise
                links = client.list_links(path)
                if links:
                    return dropbox_link_row(dropbox_pick_link(links, path), True)
                raise
        except DropboxError as exc:
            if "not_found" not in exc.summary or time.time() >= deadline:
                raise ShimError(f"error: dropbox link: {path}: {exc.summary}", 1)
            time.sleep(5)  # the desktop app has not synced the file yet


DROPBOX_LINK_FLAGS = [
    Flag("path", required=True, help="Local file path under the Dropbox folder, or a /Dropbox/api/path"),
    Flag("wait-seconds", kind="int", default=0, help="Keep retrying this long while the file is still syncing"),
    Flag("existing-only", kind="bool", default=False, help="Read an existing link and never create one"),
]


COMMANDS: dict[tuple[str, str], tuple[Handler, list[Flag]]] = {
    ("mail", "search"): (cmd_mail_search, MAIL_SEARCH_FLAGS),
    ("mail", "read"): (cmd_mail_read, MAIL_READ_FLAGS),
    ("mail", "draft"): (cmd_mail_draft, MAIL_DRAFT_FLAGS),
    ("mail", "show"): (cmd_mail_show, MAIL_SHOW_FLAGS),
    ("mail", "send"): (cmd_mail_send, MAIL_SEND_FLAGS),
    ("mail", "modify"): (cmd_mail_modify, MAIL_MODIFY_FLAGS),
    ("mail", "labels"): (cmd_mail_labels, MAIL_LABELS_FLAGS),
    ("calendar", "list"): (cmd_calendar_list, CALENDAR_LIST_FLAGS),
    ("calendar", "events"): (cmd_calendar_events, CALENDAR_EVENTS_FLAGS),
    ("calendar", "event"): (cmd_calendar_event, CALENDAR_EVENT_FLAGS),
    ("calendar", "find"): (cmd_calendar_find, CALENDAR_FIND_FLAGS),
    ("calendar", "create"): (cmd_calendar_create, cal_mutation_flags(update=False)),
    ("calendar", "update"): (cmd_calendar_update, cal_mutation_flags(update=True)),
    ("calendar", "delete"): (cmd_calendar_delete, CALENDAR_DELETE_FLAGS),
    ("drive", "query"): (cmd_drive_query, DRIVE_QUERY_FLAGS),
    ("drive", "search"): (cmd_drive_search, DRIVE_SEARCH_FLAGS),
    ("drive", "read"): (cmd_drive_read, DRIVE_READ_FLAGS),
    ("drive", "info"): (cmd_drive_info, DRIVE_INFO_FLAGS),
    ("drive", "comments"): (cmd_drive_comments, DRIVE_COMMENTS_FLAGS),
    ("drive", "comment"): (cmd_drive_comment, DRIVE_COMMENT_FLAGS),
    ("drive", "comment-add"): (cmd_drive_comment_add, DRIVE_COMMENT_ADD_FLAGS),
    ("drive", "comment-reply"): (cmd_drive_comment_reply, DRIVE_COMMENT_REPLY_FLAGS),
    ("drive", "export"): (cmd_drive_export, DRIVE_EXPORT_FLAGS),
    ("drive", "put"): (cmd_drive_put, DRIVE_PUT_FLAGS),
    ("slack", "post"): (cmd_slack_post, SLACK_POST_FLAGS),
    ("slack", "edit"): (cmd_slack_edit, SLACK_EDIT_FLAGS),
    ("slack", "search"): (cmd_slack_search, SLACK_SEARCH_FLAGS),
    ("slack", "history"): (cmd_slack_history, SLACK_HISTORY_FLAGS),
    ("slack", "thread"): (cmd_slack_thread, SLACK_THREAD_FLAGS),
    ("slack", "people"): (cmd_slack_people, SLACK_PEOPLE_FLAGS),
    ("slack", "resolve"): (cmd_slack_resolve, SLACK_RESOLVE_FLAGS),
    ("slack", "whoami"): (cmd_slack_whoami, SLACK_WHOAMI_FLAGS),
    ("slack", "channels"): (cmd_slack_channels, SLACK_CHANNELS_FLAGS),
    ("slack", "react"): (cmd_slack_react, SLACK_REACT_FLAGS),
    ("dropbox", "link"): (cmd_dropbox_link, DROPBOX_LINK_FLAGS),
}

TOOL_DESCRIPTIONS = {
    "dropbox": "Optional, no skill needs it: look up or make a Dropbox shared link anyone can open.",
    "mail": "Search and read mail, write and send drafts, and manage labels.",
    "calendar": "List calendars and read, find, create, change and delete events.",
    "drive": "Find, read, export and write Drive files, and read or add comments.",
    "slack": "Search and read Slack, post and edit messages, look up people and channels.",
}


def tool_help(tool: str) -> str:
    subs = sorted(sub for (t, sub) in COMMANDS if t == tool)
    lines = [TOOL_DESCRIPTIONS[tool], "", "Run `describe` for every subcommand with its notes.", "",
             f"Usage: pmos {tool} [OPTIONS] [COMMAND]", "", "Commands:", "  describe".ljust(28) + "List every subcommand with its notes."]
    for sub in subs:
        doc = (COMMANDS[(tool, sub)][0].__doc__ or "").strip().split("\n")[0]
        lines.append(f"  {sub}".ljust(28) + doc)
    lines += ["", "Options:", "      --timeout <SECONDS>  Seconds to wait for each HTTP call. [env: PM_OS_CONNECTOR_TIMEOUT=]",
              "  -h, --help               Show this help"]
    return "\n".join(lines)


def tool_describe(tool: str) -> str:
    lines = [TOOL_DESCRIPTIONS[tool], "",
             "pmos-shim, the PM OS connector CLI (standard-library Python), serves the subcommands below from",
             "the public REST APIs with the owner's own OAuth client and tokens. Output keeps the shapes PM OS",
             "scripts parse; system/connectors/README.md lists each one.", ""]
    for (t, sub), (handler, _) in sorted(COMMANDS.items()):
        if t == tool:
            lines.append(f"- {sub}: " + " ".join((handler.__doc__ or "").split()))
    return "\n".join(lines)


# ----------------------------------------------------------------------- shim

class Shim:
    def __init__(self, http: Http | None = None):
        self.http = http or Http(resolve_timeout(sys.argv[1:]))
        self.google = GoogleAuth(self.http)
        self.slack = SlackAuth(self.http)
        self.dropbox = DropboxAuth(self.http)

    # -- connectors
    def connectors(self, argv: list[str]) -> int:
        if not argv or argv[0] in ("--help", "-h"):
            out("pmos: auth google [--client-file PATH] [--scopes readonly|full] | auth slack | auth dropbox --app-key KEY [--no-browser] | status | doctor [dropbox] | logout <google|slack|dropbox>")
            return 0
        verb = argv[0]
        if verb == "auth":
            if len(argv) < 2 or argv[1] not in ("google", "slack", "dropbox"):
                raise ShimError("error: pmos login <google|slack|dropbox>", 2)
            if argv[1] == "dropbox":
                a = parse_flags(argv[2:], [Flag("app-key", help=""), Flag("no-browser", kind="bool", default=False, help="")])
                self.dropbox.login(a.get("app_key"), None if a.get("no_browser") else webbrowser.open)
                out(f"dropbox: ready (token stored at {self.dropbox.token_path})")
                return 0
            if argv[1] == "google":
                a = parse_flags(argv[2:], [Flag("client-file", help=""), Flag("scopes", default="full", choices=("readonly", "full"), help="")])
                tok = self.google.login(a.get("client_file"), a.get("scopes") or "full")
                out(f"google: ready (token stored at {self.google.token_path}, expires {iso_from_epoch(tok['expires_at'])})")
                return 0
            if argv[2:]:
                raise ShimError("error: pmos login slack takes no arguments; set SLACK_USER_TOKEN or paste at the prompt", 2)
            tok = self.slack.login()
            out(f"slack: ready ({tok['type']} token for team {tok.get('team') or '?'} as {tok.get('user') or '?'}, stored at {self.slack.token_path})")
            return 0
        if verb == "status":
            return self.status()
        if verb == "doctor":
            if argv[1:] == ["dropbox"]:
                return self.doctor_dropbox()
            if argv[1:]:
                raise ShimError("error: pmos doctor [dropbox]", 2)
            return self.doctor()
        if verb == "logout":
            if len(argv) < 2 or argv[1] not in ("google", "slack", "dropbox"):
                raise ShimError("error: pmos logout <google|slack|dropbox>", 2)
            removed = {"google": self.google, "slack": self.slack, "dropbox": self.dropbox}[argv[1]].logout()
            out(f"{argv[1]}: {'token removed' if removed else 'no token stored'}")
            return 0
        raise ShimError(f"error: unknown connectors verb '{verb}'", 2)

    def status(self) -> int:
        google_state = ("ready (token expires " + iso_from_epoch(self.google.expires_at()) + ", refreshes automatically)"
                        if self.google.configured() else "not configured (run: pmos login google)")
        for tool in ("mail", "drive", "calendar"):
            out(f"{tool}: {google_state}")
        if self.slack.configured():
            tok = self.slack.stored() or {}
            out(f"slack: ready ({tok.get('type', 'user')} token, team {tok.get('team') or '?'}, saved {tok.get('saved_at', '?')})")
        else:
            out("slack: not configured (run: pmos login slack)")
        if self.dropbox.configured():
            out(f"dropbox: ready (refreshes automatically, saved {(self.dropbox.stored() or {}).get('saved_at', '?')})")
        else:
            out("dropbox: not configured (run: pmos login dropbox --app-key <KEY>)")
        out(f"auth directory: {connectors_dir()}")
        return 0

    # Live probes: one cheap read per tool. `status` only reads the token files.
    DOCTOR_PROBES = (
        ("mail", f"{GMAIL_API}/profile", {}, lambda j: j.get("emailAddress", "ok")),
        ("drive", f"{DRIVE_API}/about", {"fields": "user(emailAddress)"},
         lambda j: (j.get("user") or {}).get("emailAddress", "ok")),
        ("calendar", f"{CALENDAR_API}/users/me/calendarList", {"maxResults": 1, "fields": "items(id)"},
         lambda j: f"{len(j.get('items') or [])} calendar(s) visible"),
    )

    def doctor(self) -> int:
        """Probe each configured connector with one read call and print ok/fail per tool.
        Exit 0 when every configured tool answers, 1 when any probe fails, 3 when nothing is configured."""
        configured = 0
        failures = 0
        for tool, url, params, summarise in self.DOCTOR_PROBES:
            if not self.google.configured():
                out(f"{tool}: not configured (run: pmos login google)")
                continue
            configured += 1
            try:
                payload = GoogleClient(self.http, self.google, tool).get(url, **params)
                out(f"{tool}: ok ({summarise(payload)})")
            except (HttpError, ShimError, OSError) as exc:
                failures += 1
                out(f"{tool}: FAIL ({str(exc).splitlines()[0]})")
        send_line, send_ok = self.gmail_send_state()
        out(f"mail send: {send_line}")
        failures += 0 if send_ok else 1
        if self.slack.configured():
            configured += 1
            try:
                me = SlackClient(self.http, self.slack).me()
                out(f"slack: ok (team {me.get('team') or '?'} as {me.get('user') or '?'})")
            except (HttpError, SlackError, ShimError, OSError) as exc:
                failures += 1
                out(f"slack: FAIL ({str(exc).splitlines()[0]})")
        else:
            out("slack: not configured (run: pmos login slack)")
        if self.dropbox.configured():
            # Optional: no skill needs it, because tracker links are signed-in links built from the path.
            configured += 1
            self.doctor_dropbox()
        else:
            out("dropbox: not configured (optional, no skill needs it)")
        out(f"auth directory: {connectors_dir()}")
        if configured == 0:
            return 3
        return 1 if failures else 0

    def gmail_send_state(self) -> tuple[str, bool]:
        """The machine flag, and when it is on, whether the stored token can send. Reads files only."""
        try:
            on = gmail_guard().enabled()
        except ShimError:
            return "off (the send guard could not load, so drafts only)", True
        if not on:
            return "off (drafts only; PM_OS_GMAIL_SEND is not on for this machine)", True
        scopes = (self.google.stored() or {}).get("scopes") or []
        if any(s.endswith(("/auth/gmail.modify", "/auth/gmail.compose")) or s == "https://mail.google.com/" for s in scopes):
            return "on (CoS and Launchpad only, after the owner says send; the token can send)", True
        return "FAIL (PM_OS_GMAIL_SEND is on, but the token cannot send; run: pmos login google)", False

    def doctor_dropbox(self) -> int:
        """One live read with the stored token. 0 ok, 1 failed, 3 not configured."""
        if not self.dropbox.configured():
            out("dropbox: not configured (run: pmos login dropbox --app-key <KEY>)")
            return 3
        try:
            me = DropboxClient(self.http, self.dropbox).rpc("users/get_current_account")
            out(f"dropbox: ok ({me.get('email') or 'account read'})")
            return 0
        except (HttpError, DropboxError, ShimError, OSError) as exc:
            out(f"dropbox: FAIL ({str(exc).splitlines()[0]})")
            return 1

    # -- tools
    def tools(self, argv: list[str]) -> int:
        argv = [x for x in strip_timeout(argv)]
        if not argv or argv[0] in ("--help", "-h"):
            lines = ["USAGE", "   pmos <tool> <command> [<args>]", "", "TOOLS"]
            for tool in SUPPORTED_TOOLS:
                lines.append(f"   {tool}".ljust(24) + TOOL_DESCRIPTIONS[tool])
            lines += ["", "Other tools are not available in pmos-shim; see system/connectors/README.md."]
            out("\n".join(lines))
            return 0
        tool = argv[0]
        if tool not in SUPPORTED_TOOLS:
            raise not_available(tool)
        rest = argv[1:]
        if not rest or rest[0] in ("--help", "-h"):
            out(tool_help(tool))
            return 0
        sub = rest[0]
        if sub == "describe":
            out(tool_describe(tool))
            return 0
        entry = COMMANDS.get((tool, sub))
        if entry is None:
            known = sorted(s for (t, s) in COMMANDS if t == tool)
            raise ShimError(f"error: unrecognized subcommand '{sub}' for {tool} (pmos-shim covers: {', '.join(known)})", 2)
        handler, spec = entry
        if any(x in ("--help", "-h") for x in rest[1:]):
            out(render_help(tool, sub, handler.__doc__ or "", spec))
            return 0
        args = parse_flags(rest[1:], spec)
        client: Any
        if tool == "slack":
            client = SlackClient(self.http, self.slack)
        elif tool == "dropbox":
            client = DropboxClient(self.http, self.dropbox)
        else:
            client = GoogleClient(self.http, self.google, tool)
        try:
            result = handler(client, args)
        except HttpError as exc:
            raise ShimError(f"error: {tool} {sub}: HTTP {exc.status} {exc}", 1)
        except SlackError as exc:
            raise ShimError(f"error: {tool} {sub}: Slack API error: {exc.error}", 1)
        except DropboxError as exc:
            raise ShimError(f"error: {tool} {sub}: {exc.summary}", 1)
        out_json(result)
        return 0

    def dispatch(self, argv: list[str]) -> int:
        if not argv or argv[0] in ("--help", "-h", "help"):
            out(f"pmos-shim {SHIM_VERSION} (connector CLI)\n"
                "Commands: <mail|calendar|drive|slack|dropbox> <verb> ..., login, status, doctor, logout, --version")
            return 0
        if argv[0] == "--version":
            out(f"pmos-shim {SHIM_VERSION} (connector CLI)")
            return 0
        if argv[0] in ("--timeout",) or argv[0].startswith("--timeout="):
            return self.tools(argv)
        if argv[0] in SUPPORTED_TOOLS or argv[0] in OTHER_TOOLS:
            return self.tools(argv)
        if argv[0] in ("login", "status", "doctor", "logout"):
            return self.connectors(["auth" if argv[0] == "login" else argv[0], *argv[1:]])
        raise not_available(argv[0])


def iso_from_epoch(epoch: float) -> str:
    try:
        return dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).astimezone().isoformat(timespec="minutes")
    except (TypeError, ValueError, OSError):
        return "unknown"


def resolve_timeout(argv: list[str]) -> float:
    """`--timeout N` anywhere on the command line, else PM_OS_CONNECTOR_TIMEOUT, else 60."""
    for i, arg in enumerate(argv):
        if arg == "--timeout" and i + 1 < len(argv):
            try:
                return float(argv[i + 1])
            except ValueError:
                raise ShimError(f"error: invalid value '{argv[i + 1]}' for '--timeout <SECONDS>'", 2)
        if arg.startswith("--timeout="):
            try:
                return float(arg.split("=", 1)[1])
            except ValueError:
                raise ShimError(f"error: invalid value for '--timeout <SECONDS>'", 2)
    env = os.environ.get("PM_OS_CONNECTOR_TIMEOUT", "").strip()
    try:
        return float(env) if env else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def strip_timeout(argv: list[str]) -> list[str]:
    """Remove the global `--timeout N` wherever it sits before the subcommand's own flags."""
    kept: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--timeout" and i + 1 < len(argv):
            i += 2
            continue
        if argv[i].startswith("--timeout="):
            i += 1
            continue
        kept.append(argv[i])
        i += 1
    return kept


def main(argv: list[str]) -> int:
    try:
        return Shim().dispatch(argv)
    except ShimError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    # Windows text mode ends lines with CR LF; bash callers compare whole lines,
    # so print plain LF.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(newline=chr(10))
    sys.exit(main(sys.argv[1:]))
