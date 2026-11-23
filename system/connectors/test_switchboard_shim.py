#!/usr/bin/env python3
"""Unit tests for switchboard-shim.py. No network: every test routes HTTP through a
fake transport installed on `Http.send`, the shim's single network seam.

Run from system/connectors:  python3 -m unittest test_switchboard_shim
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("switchboard_shim", HERE / "switchboard-shim.py")
switchboard_shim = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(switchboard_shim)

FIELD_RE = re.compile(r"^(Subject|From|To|Date|ID|Thread ID|Labels|Snippet): ?(.*)$")
TOKEN_RE = re.compile(r"^Next page token: ?(\S+)")


def parse_search_text(text: str):
    """Copy of system/scripts/gmail_search.parse_search_text."""
    rows, current, token = [], None, None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = TOKEN_RE.match(line)
        if m:
            token = m.group(1)
            continue
        m = FIELD_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if key == "Subject":
            current = {"subject": value}
            rows.append(current)
            continue
        if current is None:
            continue
        current[{"Thread ID": "threadId", "ID": "id"}.get(key, key.lower())] = value
    return [r for r in rows if r.get("id")], token


class FakeHttp(switchboard_shim.Http):
    """Records every request and answers from a list of (method, path-regex, responder)."""

    def __init__(self, routes=None):
        super().__init__(timeout=5)
        self.routes = routes or []
        self.calls = []

    def send(self, method, url, headers, data):
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        request = {"method": method, "url": url, "path": parsed.path, "params": params, "headers": headers, "data": data}
        self.calls.append(request)
        for route_method, pattern, responder in self.routes:
            if route_method == method and re.search(pattern, parsed.path):
                payload = responder(request) if callable(responder) else responder
                if isinstance(payload, switchboard_shim.Response):
                    return payload
                status = 200
                if isinstance(payload, tuple):
                    status, payload = payload
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return switchboard_shim.Response(status, {"Content-Type": "application/json"}, body)
        raise AssertionError(f"unexpected request {method} {parsed.path}")

    def form(self, index):
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.calls[index]["data"].decode()).items()}


class ShimCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = dict(os.environ)
        os.environ["SWITCHBOARD_CONNECTORS_DIR"] = str(self.dir)
        os.environ.pop("SWITCHBOARD_SHIM_TRACE", None)
        os.environ.pop("SWITCHBOARD_CONNECTOR_TIMEOUT", None)
        os.environ.pop("SLACK_USER_TOKEN", None)
        os.environ["SWITCHBOARD_GMAIL_SEND"] = "off"  # the real machine flag must not reach these tests

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def google_token(self, expires_in=3600):
        switchboard_shim.write_private(self.dir / "google-token.json", {
            "access_token": "ya29.test", "refresh_token": "1//refresh", "client_id": "cid",
            "client_secret": "csec", "token_uri": "https://oauth2.googleapis.com/token",
            "expires_at": time.time() + expires_in})

    def slack_token(self):
        switchboard_shim.write_private(self.dir / "slack-token.json",
                              {"token": "xoxp-test", "type": "user", "team": "T1", "saved_at": "2026-01-01T00:00:00+00:00"})

    def run_shim(self, argv, http=None):
        http = http or FakeHttp()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = switchboard_shim.Shim(http).dispatch(argv)
            except switchboard_shim.ShimError as exc:
                print(str(exc), file=sys.stderr)
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def run_json(self, argv, http):
        code, out, err = self.run_shim(argv, http)
        self.assertEqual(code, 0, err)
        return json.loads(out)


# ------------------------------------------------------------ top level


class TopLevelTests(ShimCase):
    def test_version(self):
        code, out, _ = self.run_shim(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"switchboard-shim {switchboard_shim.SHIM_VERSION} (connector CLI)")

    def test_unsupported_verbs_exit_2(self):
        for argv, verb in ([["curl", "https://x"], "curl"], [["packs"], "packs"],
                           [["linear", "list"], "linear"],
                           [["jira"], "jira"]):
            code, out, err = self.run_shim(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, "")
            self.assertEqual(err.strip(), f"switchboard-shim: '{verb}' is not available in the connector shim (it "
                                          f"covers mail, calendar, drive, slack, dropbox). See system/connectors/README.md.")

    def test_help_lists_the_tools(self):
        code, out, _ = self.run_shim(["--help"])
        self.assertEqual(code, 0)
        for tool in ("mail", "calendar", "drive", "slack"):
            self.assertIn(tool, out)

    def test_tool_help_describe_and_subcommand_help(self):
        for argv in (["slack", "--help"], ["slack", "describe"],
                     ["slack", "post", "--help"]):
            code, out, _ = self.run_shim(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("post", out)
        code, out, _ = self.run_shim(["mail", "search", "--help"])
        self.assertIn("--max-results", out)
        self.assertIn("--timeout <SECONDS>", out)

    def test_unknown_subcommand_exits_2(self):
        code, _, err = self.run_shim(["mail", "send-now", "--to", "x"])
        self.assertEqual(code, 2)
        self.assertIn("unrecognized subcommand 'send-now'", err)

    def test_missing_google_auth_exits_3(self):
        code, _, err = self.run_shim(["mail", "search", "--max-results", "1"])
        self.assertEqual(code, 3)
        self.assertEqual(err.strip(), "error: mail: not configured. Run: switchboard login google")
        code, _, err = self.run_shim(["drive", "query", "--raw-query", "name='x'"])
        self.assertEqual(code, 3)
        self.assertEqual(err.strip(), "error: drive: not configured. Run: switchboard login google")

    def test_missing_slack_auth_exits_3(self):
        code, _, err = self.run_shim(["slack", "people", "--query", "owner"])
        self.assertEqual(code, 3)
        self.assertEqual(err.strip(), "error: slack: not configured. Run: switchboard login slack")

    def test_http_error_shape(self):
        self.google_token()
        http = FakeHttp([("GET", r"/gmail/v1/users/me/labels", {"labels": [{"id": "INBOX", "name": "INBOX"}]}),
                         ("GET", r"/gmail/v1/users/me/messages$",
                          (403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes."}}))])
        code, _, err = self.run_shim(["mail", "search", "--query", "x"], http)
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "error: mail search: HTTP 403 Request had insufficient authentication scopes.")

    def test_timeout_flag_and_env(self):
        self.assertEqual(switchboard_shim.resolve_timeout(["--timeout", "30", "mail"]), 30.0)
        self.assertEqual(switchboard_shim.resolve_timeout(["mail", "search", "--timeout=12"]), 12.0)
        os.environ["SWITCHBOARD_CONNECTOR_TIMEOUT"] = "7"
        self.assertEqual(switchboard_shim.resolve_timeout(["mail"]), 7.0)
        self.assertEqual(switchboard_shim.strip_timeout(["--timeout", "30", "mail", "x", "--timeout=5"]), ["mail", "x"])

    def test_trace_prints_method_and_url_without_query(self):
        os.environ["SWITCHBOARD_SHIM_TRACE"] = "1"
        self.slack_token()
        http = FakeHttp([("POST", r"/api/users.list", {"ok": True, "members": []})])
        code, _, err = self.run_shim(["slack", "people", "--query", "nobody"], http)
        self.assertEqual(code, 0)
        self.assertIn("[switchboard-shim] POST https://slack.com/api/users.list", err)
        self.assertNotIn("xoxp", err)
        self.assertNotIn("?", err)


# ------------------------------------------------------------ flag parsing


class FlagParsingTests(unittest.TestCase):
    SPEC = [switchboard_shim.Flag("query", required=True), switchboard_shim.Flag("limit", "int", default=20),
            switchboard_shim.Flag("ids", "list"), switchboard_shim.Flag("fetch-all", "bool", default=True),
            switchboard_shim.Flag("blocks", "json"), switchboard_shim.Flag("mode", default="a", choices=("a", "b"))]

    def test_values_lists_bools_json(self):
        a = switchboard_shim.parse_flags(["--query", "hello world", "--limit=5", "--ids", "a", "b", "c", "--no-fetch-all",
                                 "--blocks", '[{"type":"divider"}]', "--raw", "--header", "X=1", "--timeout", "9"], self.SPEC)
        self.assertEqual(a["query"], "hello world")
        self.assertEqual(a["limit"], 5)
        self.assertEqual(a["ids"], ["a", "b", "c"])
        self.assertFalse(a["fetch_all"])
        self.assertEqual(a["blocks"], [{"type": "divider"}])
        self.assertEqual(a["mode"], "a")

    def test_bool_optional_value_and_json_payload(self):
        a = switchboard_shim.parse_flags(["--query", "q", "--fetch-all", "false"], self.SPEC)
        self.assertFalse(a["fetch_all"])
        a = switchboard_shim.parse_flags(["--query", "q", "--fetch-all", "--ids", "x"], self.SPEC)
        self.assertTrue(a["fetch_all"])
        a = switchboard_shim.parse_flags(["--json", '{"query": "from json", "limit": 3}'], self.SPEC)
        self.assertEqual((a["query"], a["limit"]), ("from json", 3))

    def test_errors_exit_2(self):
        for argv in ([], ["--query", "q", "--bogus", "1"], ["--query", "q", "--limit", "x"],
                     ["--query", "q", "--mode", "zzz"], ["positional"], ["--query"]):
            with self.assertRaises(switchboard_shim.ShimError, msg=argv) as ctx:
                switchboard_shim.parse_flags(argv, self.SPEC)
            self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(switchboard_shim.ShimError) as ctx:
            switchboard_shim.parse_flags([], self.SPEC)
        self.assertIn("--query <TEXT>", str(ctx.exception))


# ------------------------------------------------------------ auth files


class AuthTests(ShimCase):
    def test_status_strings(self):
        code, out, _ = self.run_shim(["status"])
        self.assertEqual(code, 0)
        self.assertIn("mail: not configured (run: switchboard login google)", out)
        self.assertIn("slack: not configured (run: switchboard login slack)", out)
        self.google_token()
        self.slack_token()
        code, out, _ = self.run_shim(["status"])
        self.assertRegex(out, r"mail: ready \(token expires .*refreshes automatically\)")
        self.assertIn("drive: ready", out)
        self.assertIn("calendar: ready", out)
        self.assertIn("slack: ready (user token, team T1", out)
        self.assertNotIn("xoxp", out)
        self.assertNotIn("ya29", out)

    def test_doctor_probes_only_configured_tools(self):
        code, out, _ = self.run_shim(["doctor"])
        self.assertEqual(code, 3)
        self.assertIn("mail: not configured", out)
        self.assertIn("slack: not configured", out)

        self.google_token()
        self.slack_token()
        http = FakeHttp([
            ("GET", r"/gmail/v1/users/me/profile$", {"emailAddress": "owner@example.com"}),
            ("GET", r"/drive/v3/about$", {"user": {"emailAddress": "owner@example.com"}}),
            ("GET", r"/calendar/v3/users/me/calendarList$", {"items": [{"id": "primary"}]}),
            ("POST", r"/api/auth\.test$", {"ok": True, "team": "Acme", "user": "owner"}),
        ])
        code, out, _ = self.run_shim(["doctor"], http)
        self.assertEqual(code, 0, out)
        self.assertIn("mail: ok (owner@example.com)", out)
        self.assertIn("drive: ok (owner@example.com)", out)
        self.assertIn("calendar: ok (1 calendar(s) visible)", out)
        self.assertIn("slack: ok (team Acme as owner)", out)
        self.assertEqual(len(http.calls), 4)
        self.assertNotIn("xoxp", out)

        # A failing probe marks only that tool and exits 1; a Slack error payload is reported as FAIL.
        http = FakeHttp([
            ("GET", r"/gmail/v1/users/me/profile$", (403, {"error": {"message": "insufficient scopes"}})),
            ("GET", r"/drive/v3/about$", {"user": {"emailAddress": "owner@example.com"}}),
            ("GET", r"/calendar/v3/users/me/calendarList$", {"items": []}),
            ("POST", r"/api/auth\.test$", {"ok": False, "error": "invalid_auth"}),
        ])
        code, out, _ = self.run_shim(["doctor"], http)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"mail: FAIL \(.*insufficient scopes")
        self.assertIn("drive: ok", out)
        self.assertRegex(out, r"slack: FAIL \(.*invalid_auth")

    def test_refresh_when_expired_writes_private_file(self):
        self.google_token(expires_in=-10)
        http = FakeHttp([("POST", r"/token$", {"access_token": "ya29.fresh", "expires_in": 3600}),
                         ("GET", r"/calendar/v3/users/me/calendarList", {"items": []})])
        code, out, err = self.run_shim(["calendar", "list", "--output", "json"], http)
        self.assertEqual(code, 0, err)
        self.assertEqual(http.calls[0]["method"], "POST")
        form = http.form(0)
        self.assertEqual(form["grant_type"], "refresh_token")
        self.assertEqual(form["refresh_token"], "1//refresh")
        self.assertEqual(http.calls[1]["headers"]["Authorization"], "Bearer ya29.fresh")
        stored = json.loads((self.dir / "google-token.json").read_text())
        self.assertEqual(stored["access_token"], "ya29.fresh")
        self.assertGreater(stored["expires_at"], time.time() + 3000)
        if os.name != "nt":
            mode = stat.S_IMODE((self.dir / "google-token.json").stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_refresh_failure_exits_3(self):
        self.google_token(expires_in=-10)
        http = FakeHttp([("POST", r"/token$", (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}))])
        code, _, err = self.run_shim(["mail", "labels", "--operation", "list_labels"], http)
        self.assertEqual(code, 3)
        self.assertIn("token refresh failed: HTTP 400 invalid_grant", err)
        self.assertIn("Run: switchboard login google", err)

    def test_google_client_file_missing(self):
        code, _, err = self.run_shim(["login", "google"])
        self.assertEqual(code, 3)
        self.assertIn("OAuth client file not found", err)
        self.assertIn("google-oauth-client.json", err)

    def test_google_exchange_stores_token(self):
        http = FakeHttp([("POST", r"/token$", {"access_token": "ya29.new", "refresh_token": "1//new", "expires_in": 3599})])
        auth = switchboard_shim.GoogleAuth(http, self.dir)
        client = {"client_id": "cid", "client_secret": "csec", "token_uri": "https://oauth2.googleapis.com/token"}
        tok = auth.exchange(client, "code123", "verifier", "http://127.0.0.1:1234/", switchboard_shim.GOOGLE_SCOPES["full"])
        self.assertEqual(tok["refresh_token"], "1//new")
        form = http.form(0)
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code_verifier"], "verifier")
        self.assertTrue(auth.configured())
        self.assertEqual(auth.token(), "ya29.new")

    def test_slack_auth_from_env_never_from_argv(self):
        code, _, err = self.run_shim(["login", "slack", "xoxp-leak"])
        self.assertEqual(code, 2)
        self.assertNotIn("xoxp-leak", err)
        os.environ["SLACK_USER_TOKEN"] = "xoxp-env-token"
        http = FakeHttp([("POST", r"/api/auth.test", {"ok": True, "team": "Team", "user": "owner", "user_id": "UOWNERAAA"})])
        code, out, err = self.run_shim(["login", "slack"], http)
        self.assertEqual(code, 0, err)
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer xoxp-env-token")
        self.assertNotIn("xoxp-env-token", out)
        stored = json.loads((self.dir / "slack-token.json").read_text())
        self.assertEqual(stored["token"], "xoxp-env-token")
        self.assertEqual(stored["type"], "user")

    def test_slack_auth_rejects_bad_token(self):
        os.environ["SLACK_USER_TOKEN"] = "notatoken"
        code, _, err = self.run_shim(["login", "slack"])
        self.assertEqual(code, 2)
        self.assertIn("must start with xoxp- or xoxb-", err)

    def test_logout(self):
        self.slack_token()
        code, out, _ = self.run_shim(["logout", "slack"])
        self.assertEqual((code, out.strip()), (0, "slack: token removed"))
        self.assertFalse((self.dir / "slack-token.json").exists())
        code, out, _ = self.run_shim(["logout", "google"])
        self.assertEqual(out.strip(), "google: no token stored")


# ------------------------------------------------------------ gmail


def gmail_message(mid, tid, subject, snippet="hi &amp; bye", labels=("INBOX", "UNREAD"), body=None):
    payload = {"headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": "A <a@example.com>"},
                           {"name": "To", "value": "b@example.com"}, {"name": "Date", "value": "Mon, 2 Nov 2026 09:00:00 -0800"}]}
    if body is not None:
        payload["mimeType"] = "text/plain"
        payload["body"] = {"data": switchboard_shim.base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")}
    return {"id": mid, "threadId": tid, "labelIds": list(labels), "snippet": snippet, "payload": payload}


class GmailTests(ShimCase):
    def setUp(self):
        super().setUp()
        self.google_token()
        self.labels = {"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "UNREAD", "name": "UNREAD"},
                                  {"id": "Label_7", "name": "Follow Up"}]}

    def test_search_text_layout_and_query(self):
        messages = {"m1": gmail_message("m1", "t1", "Quarterly plan"), "m2": gmail_message("m2", "t2", "Re: invoice")}
        http = FakeHttp([
            ("GET", r"/messages$", {"messages": [{"id": "m1"}, {"id": "m2"}], "resultSizeEstimate": 42, "nextPageToken": "TOK9"}),
            ("GET", r"/messages/(m1|m2)$", lambda r: messages[r["path"].rsplit("/", 1)[-1]]),
            ("GET", r"/labels$", self.labels)])
        payload = self.run_json(["mail", "search", "--query", "invoice", "--label", "Follow Up",
                                 "--max-results", "2", "--newer-than-days", "7", "--include-next-page-token",
                                 "--no-fetch-all"], http)
        text = payload["result"]
        self.assertTrue(text.startswith("Search summary:\nMode: messages\nReturned count: 2\nResult size estimate: 42\nNext page token: TOK9\n\n"))
        rows, token = parse_search_text(text)
        self.assertEqual(token, "TOK9")
        self.assertEqual([r["id"] for r in rows], ["m1", "m2"])
        self.assertEqual(rows[0]["threadId"], "t1")
        self.assertEqual(rows[0]["subject"], "Quarterly plan")
        self.assertEqual(rows[0]["labels"], "INBOX, UNREAD")
        self.assertEqual(rows[0]["snippet"], "hi &amp; bye")
        self.assertIn("Note:\nOnly up to 2 results were returned", text)
        listing = next(c for c in http.calls if c["path"].endswith("/messages"))
        self.assertEqual(listing["params"]["q"], ["invoice newer_than:7d"])
        self.assertEqual(listing["params"]["labelIds"], ["Label_7"])
        self.assertEqual(listing["params"]["maxResults"], ["2"])

    def test_search_zero_results_warns_inbox_only(self):
        http = FakeHttp([("GET", r"/labels$", self.labels), ("GET", r"/messages", {"resultSizeEstimate": 0})])
        payload = self.run_json(["mail", "search", "--query", "invented-term"], http)
        self.assertIn("covered only the Inbox label", payload["result"])
        self.assertIn("--label all", payload["result"])

    def test_search_label_all_skips_label_filter(self):
        http = FakeHttp([("GET", r"/messages$", {"messages": [], "resultSizeEstimate": 0})])
        payload = self.run_json(["mail", "search", "--label", "all", "--max-results", "5"], http)
        self.assertNotIn("labelIds", http.calls[0]["params"])
        self.assertIn("Returned count: 0", payload["result"])
        self.assertEqual(parse_search_text(payload["result"]), ([], None))

    def test_read_message_body_zero_and_body(self):
        msg = gmail_message("m1", "t1", "Hello", body="line one\nline two")
        http = FakeHttp([("GET", r"/messages/m1$", msg)])
        payload = self.run_json(["mail", "read", "--message-ids", "m1", "--max-body-chars", "0"], http)
        self.assertTrue(payload["result"].endswith(
            "Body:\n(Body omitted because max_body_chars is 0; original length: 17 characters.)"))
        self.assertEqual(http.calls[0]["params"]["format"], ["full"])
        payload = self.run_json(["mail", "read", "--message-ids", "m1", "--max-body-chars", "8"], http)
        head, _, body = payload["result"].partition("\nBody:\n")
        rows, _ = parse_search_text(head)
        self.assertEqual(rows[0]["id"], "m1")
        self.assertEqual(body.rsplit("\n[Truncated to ", 1)[0], "line one")
        self.assertIn("[Truncated to 8 characters; original length: 17 characters.]", body)

    def test_read_thread_message_ids_newest_first(self):
        thread = {"id": "t1", "messages": [gmail_message("old", "t1", "Topic", labels=("SENT",), body="a"),
                                           gmail_message("new", "t1", "Re: Topic", labels=("INBOX",), body="b")]}
        http = FakeHttp([("GET", r"/threads/t1$", thread)])
        payload = self.run_json(["mail", "read", "--thread-ids", "t1", "--max-body-chars", "0"], http)
        text = payload["result"]
        ids_line = next(l for l in text.splitlines() if l.startswith("Message IDs:"))
        self.assertEqual(ids_line, "Message IDs: new, old")
        self.assertIn("Thread ID: t1\nMessage count: 2\nLatest message ID: new\n", text)
        self.assertIn("Labels: INBOX, SENT", text)
        self.assertIn("Bodies:\nMessage new body:\n(Body omitted because max_body_chars is 0; original length: 1 characters.)", text)

    def test_draft_message_reply_never_sends(self):
        original = gmail_message("m1", "t1", "Question", body="original")
        original["payload"]["headers"] += [{"name": "Message-ID", "value": "<abc@mail>"}, {"name": "Reply-To", "value": "a@example.com"}]
        http = FakeHttp([("GET", r"/messages/m1$", original),
                         ("POST", r"/drafts$", {"id": "d1", "message": {"id": "dm1", "threadId": "t1"}})])
        payload = self.run_json(["mail", "draft", "--body", "Thanks", "--draft-type", "reply",
                                 "--reference-message-id", "m1"], http)
        self.assertEqual(payload["draft_metadata"]["draft_id"], "d1")
        self.assertIn("Draft created. It is saved in Gmail and was not sent.\nDraft ID: d1", payload["result"])
        post = next(c for c in http.calls if c["method"] == "POST")
        self.assertTrue(post["path"].endswith("/drafts"))
        self.assertFalse(any(c["path"].endswith("/send") for c in http.calls))
        body = json.loads(post["data"].decode())
        self.assertEqual(body["message"]["threadId"], "t1")
        raw = switchboard_shim.b64url_decode(body["message"]["raw"]).decode()
        self.assertIn("Subject: Re: Question", raw)
        self.assertIn("In-Reply-To: <abc@mail>", raw)
        self.assertIn("To: a@example.com", raw)

    def test_edit_message_labels(self):
        http = FakeHttp([("GET", r"/labels$", self.labels),
                         ("POST", r"/messages/batchModify$", b"")])
        payload = self.run_json(["mail", "modify", "--message-ids", "m1", "m2",
                                 "--labels-to-add", "Follow Up", "--labels-to-remove", "UNREAD"], http)
        post = next(c for c in http.calls if c["method"] == "POST")
        body = json.loads(post["data"].decode())
        self.assertEqual(body, {"ids": ["m1", "m2"], "addLabelIds": ["Label_7"], "removeLabelIds": ["UNREAD"]})
        self.assertIn("Modified 2 message(s): m1, m2", payload["result"])

    def test_labels_tool_list(self):
        http = FakeHttp([("GET", r"/labels$", self.labels)])
        payload = self.run_json(["mail", "labels", "--operation", "list_labels"], http)
        self.assertEqual([l["name"] for l in payload["labels"]], ["INBOX", "UNREAD", "Follow Up"])


# ------------------------------------------------------------ calendar


class CalendarTests(ShimCase):
    def setUp(self):
        super().setUp()
        self.google_token()
        self.calendars = {"items": [{"id": "owner@example.com", "summary": "owner@example.com", "timeZone": "America/Los_Angeles",
                                     "accessRole": "owner", "primary": True, "selected": True, "colorId": "14",
                                     "backgroundColor": "#9fe1e7", "foregroundColor": "#000000"}]}

    def test_list_calendars_markdown_and_json(self):
        http = FakeHttp([("GET", r"/calendarList$", self.calendars)])
        payload = self.run_json(["calendar", "list"], http)
        lines = payload["result"].splitlines()
        self.assertEqual(lines[0], "| id | summary | description | time_zone | access_role | primary | selected | color_id | background_color | foreground_color |")
        self.assertTrue(lines[1].startswith("|---"))
        self.assertIn("| owner@example.com | owner@example.com |  | America/Los_Angeles | owner | True | True | 14 |", lines[2])
        payload = self.run_json(["calendar", "list", "--output", "json"], http)
        row = payload["result"][0]
        self.assertEqual(sorted(row), ["access_role", "background_color", "color_id", "description", "foreground_color",
                                       "id", "primary", "selected", "summary", "time_zone"])

    def test_calendar_range_accepts_wrapped_time_range_and_preset(self):
        # The chief-of-staff skill passes the real tool's wrapped shape; the shim
        # used to refuse it, so start of day read "calendar unavailable" (2026-11-12).
        tz = switchboard_shim.dt.timezone(switchboard_shim.dt.timedelta(hours=-8))
        flat = switchboard_shim.calendar_range({"time_min": "2026-11-12T00:00:00-08:00",
                                       "time_max": "2026-11-13T00:00:00-08:00"}, tz)
        wrapped = switchboard_shim.calendar_range({"time_range": {"time_min": "2026-11-12T00:00:00-08:00",
                                                         "time_max": "2026-11-13T00:00:00-08:00"}}, tz)
        self.assertEqual(flat, wrapped)
        self.assertEqual(switchboard_shim.calendar_range({"preset": "today"}, tz), switchboard_shim.calendar_range("today", tz))
        with self.assertRaises(switchboard_shim.ShimError):
            switchboard_shim.calendar_range({"time_range": {"time_min": "2026-11-12T00:00:00-08:00"}}, tz)

    def test_skill_json_shape_reads_events(self):
        http = FakeHttp([("GET", r"/calendarList$", self.calendars), ("GET", r"/calendars/primary$", {"id": "owner@example.com"}),
                         ("GET", r"/calendars/[^/]+/events$", {"items": []})])
        payload = self.run_json(["calendar", "events", "--json",
                                 '{"time_period":{"time_range":{"time_min":"2026-11-12T00:00:00-08:00",'
                                 '"time_max":"2026-11-13T00:00:00-08:00"}},"include_attendees_info":true,"output":"json"}'], http)
        self.assertEqual(payload["result"], [])

    def test_get_events_today_json_and_empty_text(self):
        event = {"id": "ev1", "summary": "Standup", "status": "confirmed", "htmlLink": "https://cal/ev1",
                 "creator": {"email": "owner@example.com"}, "organizer": {"email": "owner@example.com"},
                 "start": {"dateTime": "2026-11-02T09:35:00-08:00"}, "end": {"dateTime": "2026-11-02T10:00:00-08:00"},
                 "hangoutLink": "https://meet/x"}
        http = FakeHttp([("GET", r"/calendarList$", self.calendars), ("GET", r"/calendars/primary$", {"id": "owner@example.com"}),
                         ("GET", r"/calendars/[^/]+/events$", {"items": [event]})])
        payload = self.run_json(["calendar", "events", "--time-period", '"today"',
                                 "--output", "json", "--timezone", "America/Los_Angeles"], http)
        row = payload["result"][0]
        self.assertEqual(row["id"], "ev1")
        self.assertEqual(row["start_time"], "2026-11-02T09:35:00")
        self.assertEqual(row["end_time"], "2026-11-02T10:00:00")
        self.assertEqual(row["hangout_link"], "https://meet/x")
        for key in ("attachments", "calendar_id", "creator_email", "description", "end_date", "html_link",
                    "meeting_state", "organizer_email", "start_date", "status", "summary"):
            self.assertIn(key, row)
        listing = next(c for c in http.calls if c["path"].endswith("/events"))
        self.assertEqual(listing["params"]["singleEvents"], ["true"])
        self.assertIn("timeMin", listing["params"])
        self.assertIn("timeMax", listing["params"])
        http = FakeHttp([("GET", r"/calendarList$", self.calendars), ("GET", r"/calendars/primary$", {"id": "owner@example.com"}),
                         ("GET", r"/events$", {"items": []})])
        payload = self.run_json(["calendar", "events", "--time-period",
                                 '{"time_min": "2026-11-02T00:00:00-08:00", "time_max": "2026-11-03T00:00:00-08:00"}',
                                 "--timezone", "America/Los_Angeles"], http)
        self.assertTrue(payload["result"].startswith("No events foundCalendar ID: "))
        self.assertIn("Timezone: America/Los_Angeles", payload["result"])

    def test_create_event_posts_body_and_dry_run(self):
        created = {"id": "new1", "summary": "Sync", "htmlLink": "https://cal/new1",
                   "start": {"dateTime": "2026-11-03T10:00:00-08:00"}, "end": {"dateTime": "2026-11-03T10:30:00-08:00"}}
        http = FakeHttp([("POST", r"/calendars/primary/events$", created)])
        payload = self.run_json(["calendar", "create", "--title", "Sync",
                                 "--start-time", "2026-11-03T10:00:00-08:00", "--end-time", "2026-11-03T10:30:00-08:00",
                                 "--required-attendees", "a@example.com", "b@example.com", "--no-with-meet",
                                 "--timezone", "America/Los_Angeles"], http)
        body = json.loads(http.calls[0]["data"].decode())
        self.assertEqual(body["summary"], "Sync")
        self.assertEqual([x["email"] for x in body["attendees"]], ["a@example.com", "b@example.com"])
        self.assertNotIn("conferenceData", body)
        self.assertEqual(payload["event"]["id"], "new1")
        http = FakeHttp()
        code, out, err = self.run_shim(["calendar", "create", "--title", "Sync",
                                       "--start-time", "2026-11-03T10:00:00-08:00", "--end-time", "2026-11-03T10:30:00-08:00",
                                       "--dry-run"], http)
        self.assertEqual(code, 0, err)
        self.assertEqual(http.calls, [])
        self.assertEqual(json.loads(out)["request"]["method"], "POST")
        self.assertIn("Dry run", json.loads(out)["result"])

    def test_delete_event(self):
        http = FakeHttp([("DELETE", r"/calendars/primary/events/ev1$", b"")])
        payload = self.run_json(["calendar", "delete", "--event-id", "ev1"], http)
        self.assertEqual(http.calls[0]["method"], "DELETE")
        self.assertEqual(payload["result"], "Event deleted.\nID: ev1\nCalendar: primary")


# ------------------------------------------------------------ drive


class DriveTests(ShimCase):
    def setUp(self):
        super().setUp()
        self.google_token()
        self.file = {"id": "FAKEDOCIDA", "name": "Plan", "mimeType": switchboard_shim.MIME_DOC, "createdTime": "2026-01-01T00:00:00Z",
                     "modifiedTime": "2026-02-01T00:00:00Z", "owners": [{"displayName": "Owner", "emailAddress": "o@example.com",
                                                                        "permissionId": "p1"}], "parents": ["P1"], "webViewLink": "https://docs/FAKEDOCIDA"}

    def test_raw_search_shape_and_params(self):
        http = FakeHttp([("GET", r"/drive/v3/files$", {"files": [self.file], "nextPageToken": "NPT"})])
        payload = self.run_json(["drive", "query", "--raw-query", "name contains 'Plan'",
                                 "--page-size", "1"], http)
        params = http.calls[0]["params"]
        self.assertEqual(params["q"], ["name contains 'Plan'"])
        self.assertEqual(params["corpora"], ["allDrives"])
        self.assertEqual(params["pageSize"], ["1"])
        self.assertEqual(params["includeItemsFromAllDrives"], ["true"])
        self.assertEqual(sorted(payload), ["corpora", "drive_id", "files", "next_page_token", "notes", "page_size", "page_token", "query"])
        self.assertEqual(payload["next_page_token"], "NPT")
        f = payload["files"][0]
        self.assertEqual((f["id"], f["name"], f["mime_type"]), ("FAKEDOCIDA", "Plan", switchboard_shim.MIME_DOC))
        self.assertEqual(f["owners"][0]["emailAddress"], "o@example.com")
        self.assertEqual(payload["notes"][0], "Note:")
        self.assertIn("Only up to 1 results are shown.", payload["notes"][-1])

    def test_deprecated_search_builds_query(self):
        http = FakeHttp([("GET", r"/drive/v3/files$", {"files": []})])
        self.run_json(["drive", "search", "--drive-type", "file", "--query", "roadmap",
                       "--field", "name", "--mime-type", switchboard_shim.MIME_DOC], http)
        q = http.calls[0]["params"]["q"][0]
        self.assertIn("name contains 'roadmap'", q)
        self.assertIn(f"mimeType = '{switchboard_shim.MIME_DOC}'", q)
        self.assertIn("trashed = false", q)

    def test_get_file_metadata_accepts_url(self):
        http = FakeHttp([("GET", r"/drive/v3/files/FAKEDOCIDA$", dict(self.file, trashed=False))])
        payload = self.run_json(["drive", "info", "--file-id-or-url",
                                 "https://docs.google.com/document/d/FAKEDOCIDA/edit?tab=t.0"], http)
        self.assertEqual(payload["file"]["id"], "FAKEDOCIDA")
        self.assertEqual(payload["file"]["urls"]["edit"], "https://docs.google.com/document/d/FAKEDOCIDA/edit")
        self.assertIn("web", payload["file"]["urls"])

    def test_get_comment_shape(self):
        comment = {"id": "c1", "author": {"displayName": "Reviewer", "me": False}, "content": "Please fix",
                   "createdTime": "2026-03-01T00:00:00Z", "resolved": False, "deleted": False,
                   "quotedFileContent": {"value": "text"}, "replies": [{"id": "r1", "author": {"me": True}, "content": "Done"}]}
        http = FakeHttp([("GET", r"/files/FAKEDOCIDA/comments/c1$", comment)])
        payload = self.run_json(["drive", "comment", "--file-id-or-url", "FAKEDOCIDA", "--comment-id", "c1",
                                 "--no-include-quoted-content"], http)
        self.assertEqual(list(payload), ["comment"])
        self.assertEqual(payload["comment"]["author"]["me"], False)
        self.assertEqual(payload["comment"]["replies"][0]["author"]["me"], True)
        self.assertNotIn("quotedFileContent", payload["comment"])
        self.assertEqual(http.calls[0]["params"]["fields"], ["*"])

    def test_list_comments_shape(self):
        http = FakeHttp([("GET", r"/files/FAKEDOCIDA/comments$", {"comments": [{"id": "c1", "resolved": True}, {"id": "c2", "resolved": False}]})])
        payload = self.run_json(["drive", "comments", "--file-id-or-url", "FAKEDOCIDA"], http)
        self.assertEqual(sorted(payload), ["comments", "next_page_token", "page_size", "page_token"])
        self.assertEqual(payload["next_page_token"], "")

    def test_read_doc_exports_text(self):
        http = FakeHttp([("GET", r"/drive/v3/files/FAKEDOCIDA$", self.file),
                         ("GET", r"/files/FAKEDOCIDA/export$", switchboard_shim.Response(200, {"Content-Type": "text/plain"}, b"Doc body\nline 2"))])
        payload = self.run_json(["drive", "read", "--id-or-url", "FAKEDOCIDA"], http)
        self.assertEqual(http.calls[1]["params"]["mimeType"], ["text/plain"])
        self.assertEqual(payload["result"], "Doc body\nline 2")
        payload = self.run_json(["drive", "read", "--id-or-url", "FAKEDOCIDA", "--include-metadata"], http)
        self.assertEqual(payload["file"]["id"], "FAKEDOCIDA")
        self.assertEqual(payload["mime_type"], switchboard_shim.MIME_DOC)

    def test_read_sheet_as_csv(self):
        sheet = dict(self.file, mimeType=switchboard_shim.MIME_SHEET)
        http = FakeHttp([("GET", r"/drive/v3/files/FAKEDOCIDA$", sheet),
                         ("GET", r"/spreadsheets/FAKEDOCIDA$", {"sheets": [{"properties": {"title": "Tab A"}}]}),
                         ("GET", r"/spreadsheets/FAKEDOCIDA/values/", {"values": [["name", "qty"], ["apple", "3"]]})])
        payload = self.run_json(["drive", "read", "--id-or-url", "FAKEDOCIDA"], http)
        self.assertIn("name,qty", payload["result"])
        self.assertIn("apple,3", payload["result"])

    def test_upsert_create_and_update(self):
        http = FakeHttp([("POST", r"/upload/drive/v3/files$", {"id": "N1abcdefghijklmnop", "name": "notes.md", "mimeType": "text/markdown"})])
        payload = self.run_json(["drive", "put", "--name", "notes.md", "--parent-id", "P1",
                                 "--body", "# hello", "--source-mime-type", "text/markdown"], http)
        self.assertEqual((payload["action"], payload["id"]), ("created", "N1abcdefghijklmnop"))
        self.assertEqual(http.calls[0]["params"]["uploadType"], ["multipart"])
        self.assertIn(b'"parents": ["P1"]', http.calls[0]["data"])
        self.assertIn(b"# hello", http.calls[0]["data"])
        http = FakeHttp([("PATCH", r"/upload/drive/v3/files/N1abcdefghijklmnop$", {"id": "N1abcdefghijklmnop", "name": "notes.md"})])
        payload = self.run_json(["drive", "put", "--file-id", "N1abcdefghijklmnop", "--body", "updated"], http)
        self.assertEqual(http.calls[0]["method"], "PATCH")
        self.assertEqual(payload["action"], "updated")

    def test_create_and_reply_comment(self):
        http = FakeHttp([("POST", r"/files/FAKEDOCIDA/comments$", {"id": "c9", "content": "New note"}),
                         ("POST", r"/files/FAKEDOCIDA/comments/c9/replies$", {"id": "r1", "content": "Reply"})])
        payload = self.run_json(["drive", "comment-add", "--file-id-or-url", "FAKEDOCIDA", "--content", "New note"], http)
        self.assertEqual(payload["comment"]["id"], "c9")
        payload = self.run_json(["drive", "comment-reply", "--file-id-or-url", "FAKEDOCIDA", "--comment-id", "c9",
                                 "--content", "Reply"], http)
        self.assertEqual(payload["reply"]["id"], "r1")


# ------------------------------------------------------------ slack


class SlackTests(ShimCase):
    def setUp(self):
        super().setUp()
        self.slack_token()
        self.users = {"ok": True, "members": [
            {"id": "UOWNERAAA", "name": "owner", "real_name": "The Owner", "is_bot": False,
             "profile": {"display_name": "owner", "email": "owner@example.com", "real_name": "The Owner"}},
            {"id": "UROBOTAAA", "name": "robot", "is_bot": True, "profile": {"display_name": "Robot"}}]}
        self.channels = {"ok": True, "channels": [{"id": "CGENERALA", "name": "general", "is_private": False, "num_members": 3,
                                                   "purpose": {"value": "p"}, "topic": {"value": "t"}}]}

    def test_post_message_matches_slack_send(self):
        http = FakeHttp([("POST", r"/api/conversations.list", self.channels),
                         ("POST", r"/api/chat.postMessage", {"ok": True, "channel": "CGENERALA", "ts": "1700000000.000100",
                                                               "message": {"text": "hello", "ts": "1700000000.000100"}}),
                         ("POST", r"/api/chat.getPermalink", {"ok": True, "permalink": "https://slack/p1"})])
        blocks = '[{"type":"section","text":{"type":"mrkdwn","text":"hello"}}]'
        payload = self.run_json(["slack", "post", "--channel", "#general", "--text", "hello",
                                 "--blocks", blocks], http)
        self.assertEqual(payload["ts"], "1700000000.000100")
        self.assertEqual(payload["message"]["ts"], "1700000000.000100")
        self.assertEqual(payload["channel"], "CGENERALA")
        self.assertEqual(payload["permalink"], "https://slack/p1")
        post = next(i for i, c in enumerate(http.calls) if c["path"].endswith("chat.postMessage"))
        form = http.form(post)
        self.assertEqual(form["channel"], "CGENERALA")
        self.assertEqual(form["text"], "hello")
        self.assertEqual(json.loads(form["blocks"]), json.loads(blocks))
        self.assertEqual(http.calls[post]["headers"]["Authorization"], "Bearer xoxp-test")
        self.assertIn("application/x-www-form-urlencoded", http.calls[post]["headers"]["Content-Type"])

    def test_post_message_thread_reply_by_channel_id(self):
        http = FakeHttp([("POST", r"/api/chat.postMessage", {"ok": True, "channel": "CGENERALA", "ts": "2.0", "message": {"ts": "2.0", "thread_ts": "1.0"}}),
                         ("POST", r"/api/chat.getPermalink", {"ok": False, "error": "x"})])
        payload = self.run_json(["slack", "post", "--channel", "CGENERALA", "--text", "reply", "--thread-ts", "1.0"], http)
        self.assertEqual(http.form(0)["thread_ts"], "1.0")
        self.assertEqual(payload["thread_ts"], "1.0")
        self.assertNotIn("permalink", payload)

    def test_post_message_requires_target(self):
        code, _, err = self.run_shim(["slack", "post", "--text", "x"])
        self.assertEqual(code, 2)
        self.assertIn("--channel", err)

    def test_slack_api_error(self):
        http = FakeHttp([("POST", r"/api/chat.postMessage", {"ok": False, "error": "channel_not_found"})])
        code, _, err = self.run_shim(["slack", "post", "--channel", "CMISSINGA", "--text", "x"], http)
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "error: slack post: Slack API error: channel_not_found")

    def test_search_messages_query_and_shape(self):
        match = {"ts": "1700000000.000200", "text": "status update", "type": "message", "user": "UOWNERAAA", "username": "owner",
                 "channel": {"id": "CGENERALA", "name": "general"},
                 "permalink": "https://x.slack.com/archives/CGENERALA/p1700000000000200?thread_ts=1699990000.000100&cid=CGENERALA"}
        http = FakeHttp([("POST", r"/api/auth.test", {"ok": True, "user_id": "UOWNERAAA"}),
                         ("POST", r"/api/users.info", {"ok": True, "user": self.users["members"][0]}),
                         ("POST", r"/api/search.messages", {"ok": True, "messages": {"matches": [match], "paging": {"page": 1, "pages": 3}}})])
        payload = self.run_json(["slack", "search", "--query", "status", "--from-user", "me",
                                 "--newer-than", "P3D", "--limit", "5", "--sort", "timestamp"], http)
        form = http.form(next(i for i, c in enumerate(http.calls) if c["path"].endswith("search.messages")))
        self.assertTrue(form["query"].startswith("status from:<@UOWNERAAA> after:"))
        self.assertEqual(form["count"], "5")
        self.assertEqual(form["sort"], "timestamp")
        self.assertEqual(sorted(payload), ["has_more", "messages", "next_cursor", "query", "summary"])
        self.assertEqual(payload["summary"], "Found 1 message.")
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["next_cursor"], "2")
        m = payload["messages"][0]
        self.assertEqual(m["channel"], {"id": "CGENERALA", "name": "general"})
        self.assertEqual(m["ts"], "1700000000.000200")
        self.assertEqual(m["thread_ts"], "1699990000.000100")
        self.assertTrue(m["is_reply"])
        self.assertEqual(m["user"]["username"], "owner")
        self.assertRegex(m["time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} ")

    def test_search_users_and_resolve_name(self):
        http = FakeHttp([("POST", r"/api/users.list", self.users), ("POST", r"/api/conversations.list", self.channels)])
        payload = self.run_json(["slack", "people", "--query", "owner"], http)
        self.assertEqual(payload["query"], "owner")
        self.assertEqual(payload["results"][0], {"display_name": "owner", "email": "owner@example.com", "id": "UOWNERAAA",
                                                 "is_bot": False, "real_name": "The Owner", "username": "owner"})
        payload = self.run_json(["slack", "resolve", "--name", "@owner"], http)
        self.assertEqual(payload, {"id": "UOWNERAAA", "is_bot": False, "name": "owner", "real_name": "The Owner", "type": "user"})
        payload = self.run_json(["slack", "resolve", "--name", "general"], http)
        self.assertEqual((payload["id"], payload["type"]), ("CGENERALA", "channel"))
        code, _, err = self.run_shim(["slack", "resolve", "--name", "missing"], http)
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "error: Channel 'missing' was not found.")

    def test_search_channels(self):
        http = FakeHttp([("POST", r"/api/conversations.list", self.channels)])
        payload = self.run_json(["slack", "channels", "--query", "gen"], http)
        self.assertEqual(sorted(payload), ["has_more", "next_cursor", "query", "results"])
        self.assertEqual(payload["results"][0]["name"], "general")
        self.assertEqual(sorted(payload["results"][0]), ["id", "is_private", "name", "num_members", "purpose", "topic", "type"])

    def test_read_channels_and_thread(self):
        history = {"ok": True, "messages": [{"ts": "1700000000.000100", "text": "root", "user": "UOWNERAAA"},
                                            {"ts": "1700000100.000100", "text": "bot says", "bot_id": "B1", "subtype": "bot_message"}],
                   "has_more": False, "response_metadata": {"next_cursor": ""}}
        http = FakeHttp([("POST", r"/api/conversations.list", self.channels),
                         ("POST", r"/api/conversations.history", history),
                         ("POST", r"/api/conversations.replies", history),
                         ("POST", r"/api/conversations.info", {"ok": True, "channel": {"id": "CGENERALA", "name": "general"}}),
                         ("POST", r"/api/users.info", {"ok": True, "user": self.users["members"][0]})])
        payload = self.run_json(["slack", "history", "--channels", "general", "--limit", "2"], http)
        self.assertEqual(payload["result_count"], 1)
        r = payload["results"][0]
        self.assertEqual(r["channel"], {"id": "CGENERALA", "name": "general"})
        self.assertEqual(r["messages_returned"], 2)
        self.assertEqual(r["messages"][0]["text"], "root")
        self.assertRegex(r["messages"][0]["time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} ")
        self.assertEqual(r["messages"][1]["bot_id"], "B1")
        payload = self.run_json(["slack", "thread", "--channel", "CGENERALA", "--thread-ts", "1700000000.000100"], http)
        self.assertEqual(payload["thread_ts"], "1700000000.000100")
        self.assertEqual(payload["channel"]["id"], "CGENERALA")
        self.assertEqual(payload["messages"][0]["user"]["id"], "UOWNERAAA")

    def test_read_user_profile(self):
        http = FakeHttp([("POST", r"/api/auth.test", {"ok": True, "user_id": "UOWNERAAA"}),
                         ("POST", r"/api/users.info", {"ok": True, "user": dict(self.users["members"][0], tz="America/Los_Angeles",
                                                                               tz_label="Pacific Standard Time", tz_offset=-28800)}),
                         ("POST", r"/api/dnd.info", {"ok": True, "dnd_enabled": False})])
        payload = self.run_json(["slack", "whoami"], http)
        self.assertEqual(payload["user"]["email"], "owner@example.com")
        self.assertEqual(payload["user"]["tz"], "America/Los_Angeles")
        self.assertEqual(payload["profile"]["email"], "owner@example.com")
        self.assertIs(payload["user"]["dnd_enabled"], False)

    def test_update_message_and_add_reaction(self):
        http = FakeHttp([("POST", r"/api/chat.update", {"ok": True, "channel": "CGENERALA", "ts": "1.0", "text": "new"}),
                         ("POST", r"/api/reactions.add", {"ok": False, "error": "already_reacted"})])
        payload = self.run_json(["slack", "edit", "--channel", "CGENERALA", "--message-ts", "1.0", "--text", "new"], http)
        self.assertEqual(payload, {"channel": "CGENERALA", "text": "new", "ts": "1.0"})
        payload = self.run_json(["slack", "react", "--channel", "CGENERALA", "--message-ts", "1.0", "--emoji", ":eyes:"], http)
        self.assertTrue(payload["was_already_present"])
        self.assertEqual(http.form(1)["name"], "eyes")


class DropboxTests(ShimCase):
    PDF = "/documents/shared folder/reports/2026/2026-09-02.pdf"

    def dropbox_token(self, expires_in=3600):
        switchboard_shim.write_private(self.dir / "dropbox-token.json", {
            "access_token": "sl.test", "refresh_token": "rt.test", "app_key": "appkey",
            "expires_at": time.time() + expires_in, "saved_at": "2026-11-08T00:00:00+00:00"})

    def link(self, level="viewer", audience="public"):
        return {".tag": "file", "url": "https://www.dropbox.com/scl/fi/x/2026-09-02.pdf?rlkey=k&dl=0", "name": "2026-09-02.pdf",
                "path_lower": self.PDF, "link_permissions": {"effective_audience": {".tag": audience},
                                                             "link_access_level": {".tag": level}}}

    def test_status_and_doctor(self):
        _, out, _ = self.run_shim(["status"])
        self.assertIn("dropbox: not configured (run: switchboard login dropbox --app-key <KEY>)", out)
        code, out, _ = self.run_shim(["doctor", "dropbox"])
        self.assertEqual(code, 3)
        self.dropbox_token()
        _, out, _ = self.run_shim(["status"])
        self.assertIn("dropbox: ready", out)
        self.assertNotIn("sl.test", out)
        self.assertNotIn("rt.test", out)
        http = FakeHttp([("POST", r"/2/users/get_current_account$", {"email": "owner@example.com"})])
        code, out, _ = self.run_shim(["doctor", "dropbox"], http)
        self.assertEqual(code, 0, out)
        self.assertIn("dropbox: ok (owner@example.com)", out)
        self.assertEqual(http.calls[0]["data"], b"null")
        http = FakeHttp([("POST", r"/2/users/get_current_account$", (401, {"error_summary": "missing_scope/.."}))])
        code, out, _ = self.run_shim(["doctor", "dropbox"], http)
        self.assertEqual(code, 1)
        self.assertIn("dropbox: FAIL (missing_scope)", out)

    def test_full_doctor_treats_dropbox_as_optional(self):
        self.google_token()
        probes = [
            ("GET", r"/gmail/v1/users/me/profile$", {"emailAddress": "owner@example.com"}),
            ("GET", r"/drive/v3/about$", {"user": {"emailAddress": "owner@example.com"}}),
            ("GET", r"/calendar/v3/users/me/calendarList$", {"items": []}),
        ]
        code, out, _ = self.run_shim(["doctor"], FakeHttp(list(probes)))
        self.assertEqual(code, 0, out)
        self.assertIn("dropbox: not configured (optional, no skill needs it)", out)
        self.dropbox_token()
        http = FakeHttp(probes + [("POST", r"/2/users/get_current_account$", (401, {"error_summary": "missing_scope/.."}))])
        code, out, _ = self.run_shim(["doctor"], http)
        self.assertEqual(code, 0, out)
        self.assertIn("dropbox: FAIL (missing_scope)", out)

    def test_refresh_uses_no_secret_and_keeps_refresh_token(self):
        self.dropbox_token(expires_in=-10)
        http = FakeHttp([
            ("POST", r"/oauth2/token$", {"access_token": "sl.new", "expires_in": 14400}),
            ("POST", r"/2/users/get_current_account$", {"email": "owner@example.com"}),
        ])
        code, _, _ = self.run_shim(["doctor", "dropbox"], http)
        self.assertEqual(code, 0)
        self.assertEqual(http.form(0), {"client_id": "appkey", "refresh_token": "rt.test", "grant_type": "refresh_token"})
        stored = json.loads((self.dir / "dropbox-token.json").read_text())
        self.assertEqual(stored["access_token"], "sl.new")
        self.assertEqual(stored["refresh_token"], "rt.test")

    def test_exchange_refuses_missing_scope_and_stores_nothing(self):
        http = FakeHttp([("POST", r"/oauth2/token$", {"access_token": "a", "refresh_token": "r", "scope": "account_info.read"})])
        with self.assertRaises(switchboard_shim.ShimError) as caught:
            switchboard_shim.DropboxAuth(http, self.dir).exchange("appkey", "code", "verifier", "http://localhost:7473/")
        self.assertIn("sharing.write", str(caught.exception))
        self.assertFalse((self.dir / "dropbox-token.json").exists())

    def test_existing_link_is_reused_and_never_created(self):
        self.dropbox_token()
        http = FakeHttp([("POST", r"/2/sharing/list_shared_links$", {"links": [self.link("editor"), self.link()]})])
        code, out, _ = self.run_shim(["dropbox", "link", "--path", self.PDF], http)
        self.assertEqual(code, 0, out)
        row = json.loads(out)
        self.assertTrue(row["existed"])
        self.assertEqual(row["access_level"], "viewer")
        self.assertEqual(row["who_can_open"], "anyone with the link")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(json.loads(http.calls[0]["data"]), {"path": self.PDF, "direct_only": True})

    def test_new_link_uses_account_default_settings(self):
        self.dropbox_token()
        http = FakeHttp([
            ("POST", r"/2/sharing/list_shared_links$", {"links": []}),
            ("POST", r"/2/sharing/create_shared_link_with_settings$", self.link()),
        ])
        code, out, _ = self.run_shim(["dropbox", "link", "--path", self.PDF], http)
        self.assertEqual(code, 0, out)
        self.assertFalse(json.loads(out)["existed"])
        self.assertEqual(json.loads(http.calls[1]["data"]), {"path": self.PDF})

    def test_existing_only_never_creates(self):
        self.dropbox_token()
        http = FakeHttp([("POST", r"/2/sharing/list_shared_links$", {"links": []})])
        code, out, _ = self.run_shim(["dropbox", "link", "--path", self.PDF, "--existing-only"], http)
        self.assertEqual(code, 0, out)
        self.assertIsNone(json.loads(out)["url"])
        self.assertEqual(len(http.calls), 1)

    def test_create_race_falls_back_to_the_existing_link(self):
        self.dropbox_token()
        answers = iter([{"links": []}, {"links": [self.link()]}])
        http = FakeHttp([
            ("POST", r"/2/sharing/list_shared_links$", lambda _: next(answers)),
            ("POST", r"/2/sharing/create_shared_link_with_settings$", (409, {"error_summary": "shared_link_already_exists/.."})),
        ])
        code, out, _ = self.run_shim(["dropbox", "link", "--path", self.PDF], http)
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["existed"])

    def test_not_found_names_the_path_and_exits_1(self):
        self.dropbox_token()
        http = FakeHttp([("POST", r"/2/sharing/list_shared_links$", (409, {"error_summary": "path/not_found/."}))])
        code, _, err = self.run_shim(["dropbox", "link", "--path", self.PDF], http)
        self.assertEqual(code, 1)
        self.assertIn(self.PDF, err)
        self.assertIn("path/not_found", err)

    def test_not_configured_exits_3(self):
        code, _, err = self.run_shim(["dropbox", "link", "--path", self.PDF], FakeHttp())
        self.assertEqual(code, 3)
        self.assertIn("switchboard login dropbox", err)

    def test_who_can_open_with_a_password_and_unknown_audience(self):
        row = switchboard_shim.dropbox_link_row({"link_permissions": {"effective_audience": {".tag": "public"}, "require_password": True}}, True)
        self.assertEqual(row["who_can_open"], "anyone with the link and its password")
        row = switchboard_shim.dropbox_link_row({"link_permissions": {"effective_audience": {".tag": "martian"}}}, True)
        self.assertEqual(row["who_can_open"], "unknown (martian)")

    def test_local_path_maps_to_api_path(self):
        root = str(self.dir / "Dropbox")
        local = os.path.join(root, "Documents", "Shared Folder", "a.pdf")
        os.makedirs(os.path.dirname(local))
        Path(local).write_bytes(b"%PDF")
        self.assertEqual(switchboard_shim.dropbox_api_path(local, [root]), "/Documents/Shared Folder/a.pdf")
        self.assertEqual(switchboard_shim.dropbox_api_path(local.upper() if os.name == "nt" else local, [root]).lower(),
                         "/documents/shared folder/a.pdf")
        self.assertEqual(switchboard_shim.dropbox_api_path("/Documents/a.pdf", [root]), "/Documents/a.pdf")
        if os.name == "nt":  # a POSIX absolute path outside the roots reads as an API path
            with self.assertRaises(switchboard_shim.ShimError):
                switchboard_shim.dropbox_api_path(str(self.dir / "elsewhere" / "a.pdf"), [root])
        if os.name == "nt":  # sibling folder sharing the name prefix (POSIX paths read as API paths)
            with self.assertRaises(switchboard_shim.ShimError):
                switchboard_shim.dropbox_api_path(root + " (Old)" + os.sep + "a.pdf", [root])
        with self.assertRaises(switchboard_shim.ShimError):  # the root itself is not a file
            switchboard_shim.dropbox_api_path(root, [root])

    def test_folders_and_empty_paths_are_refused_before_any_call(self):
        root = str(self.dir / "Dropbox")
        os.makedirs(os.path.join(root, "Documents"))
        here = os.getcwd()
        os.chdir(root)
        try:
            for bad in (" ", ".", os.path.join(root, "Documents"), os.path.join(root, "Documents") + os.sep,
                        "/Documents", "/", os.path.join(root, "missing.pdf")):
                with self.assertRaises(switchboard_shim.ShimError, msg=bad):
                    switchboard_shim.dropbox_api_path(bad, [root])
        finally:
            os.chdir(here)

    def test_several_accounts_need_an_explicit_root(self):
        with self.assertRaises(switchboard_shim.ShimError) as caught:
            switchboard_shim.dropbox_api_path("/a.pdf", ["C:/one", "C:/two"])
        self.assertIn("SWITCHBOARD_DROPBOX_ROOT", str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "Windows path forms")
    def test_windows_leading_slash_is_a_dropbox_path(self):
        root = str(self.dir / "Dropbox")
        self.assertEqual(switchboard_shim.dropbox_api_path("/Dropbox/a.pdf", ["C:\\Dropbox"]), "/Dropbox/a.pdf")
        self.assertEqual(switchboard_shim.dropbox_api_path("/a/b.pdf", [root]), "/a/b.pdf")

    def test_folder_link_is_revoked_and_refused(self):
        self.dropbox_token()
        folder = dict(self.link(), **{".tag": "folder"})
        http = FakeHttp([
            ("POST", r"/2/sharing/list_shared_links$", {"links": []}),
            ("POST", r"/2/sharing/create_shared_link_with_settings$", folder),
            ("POST", r"/2/sharing/revoke_shared_link$", {}),
        ])
        code, out, err = self.run_shim(["dropbox", "link", "--path", "/Documents/thing"], http)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("the link was revoked", err)
        self.assertEqual(json.loads(http.calls[2]["data"]), {"url": folder["url"]})

    def test_unusable_links_are_skipped(self):
        self.dropbox_token()
        expired = dict(self.link(), expires="2020-01-01T00:00:00Z")
        private = self.link(audience="no_one")
        live = dict(self.link(), url="https://www.dropbox.com/live")
        http = FakeHttp([("POST", r"/2/sharing/list_shared_links$", {"links": [expired, private, live]})])
        code, out, _ = self.run_shim(["dropbox", "link", "--path", self.PDF], http)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["url"], "https://www.dropbox.com/live")
        http = FakeHttp([("POST", r"/2/sharing/list_shared_links$", {"links": [expired, private]})])
        code, out, err = self.run_shim(["dropbox", "link", "--path", self.PDF], http)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("none was used", err)

    def test_edit_only_link_says_it_can_edit(self):
        self.dropbox_token()
        http = FakeHttp([("POST", r"/2/sharing/list_shared_links$", {"links": [self.link("editor")]})])
        _, out, _ = self.run_shim(["dropbox", "link", "--path", self.PDF], http)
        self.assertEqual(json.loads(out)["who_can_open"], "anyone with the link, and they can edit it")

    def test_login_survives_probes_and_stale_tabs(self):
        """A port probe, a request with no state and an old tab must not end the login."""
        import socket
        import threading
        import urllib.request
        with socket.socket() as probe:  # a free port, so this never meets a real sign-in on the fixed one
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        original_port = switchboard_shim.DROPBOX_REDIRECT_PORT
        switchboard_shim.DROPBOX_REDIRECT_PORT = port
        self.addCleanup(setattr, switchboard_shim, "DROPBOX_REDIRECT_PORT", original_port)
        http = FakeHttp([("POST", r"/oauth2/token$", {"access_token": "a", "refresh_token": "r",
                                                      "scope": " ".join(switchboard_shim.DROPBOX_SCOPES), "expires_in": 14400})])
        seen = {}

        def browser(url):
            state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

            def visit():
                base = f"http://127.0.0.1:{switchboard_shim.DROPBOX_REDIRECT_PORT}/"
                for _ in range(3):  # connect then close, the way a port check does
                    socket.create_connection(("127.0.0.1", switchboard_shim.DROPBOX_REDIRECT_PORT), timeout=5).close()
                urllib.request.urlopen(base + "favicon.ico", timeout=15).read()
                seen["stale"] = urllib.request.urlopen(base + "?code=old&state=old", timeout=15).read().decode()
                seen["good"] = urllib.request.urlopen(base + "?" + urllib.parse.urlencode({"code": "good", "state": state}), timeout=15).read().decode()
            threading.Thread(target=visit, daemon=True).start()
            return True

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            switchboard_shim.DropboxAuth(http, self.dir).login("appkey", browser)
        self.assertIn("out of date", seen["stale"])
        self.assertIn("authorization received", seen["good"])
        self.assertEqual(http.form(0)["code"], "good")
        self.assertEqual(json.loads((self.dir / "dropbox-token.json").read_text())["refresh_token"], "r")

    def test_a_temporary_refresh_failure_is_not_signed_out(self):
        self.dropbox_token(expires_in=-10)
        http = FakeHttp([("POST", r"/oauth2/token$", (503, {"error": "unavailable"}))])
        code, _, err = self.run_shim(["dropbox", "link", "--path", self.PDF], http)
        self.assertEqual(code, 1, err)
        http = FakeHttp([("POST", r"/oauth2/token$", (400, {"error": "invalid_grant"}))])
        code, _, err = self.run_shim(["dropbox", "link", "--path", self.PDF], http)
        self.assertEqual(code, 3, err)


if __name__ == "__main__":
    unittest.main()
