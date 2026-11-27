#!/usr/bin/env python3
"""The owner-browser proxy against a fake MCP server, with invented tabs and throwaway folders.

No case reaches a real browser or the real local/. The live check is a separate script,
owner_browser_live_check.py, because it needs the owner's Allow click.

  python3 system/scripts/test_owner_browser.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import owner_browser as ob  # noqa: E402

OWNER_TABS = "1: Secret Invented Tab (https://private.example.invalid/inbox)\n2: Another Invented Tab (https://bank.example.invalid/)"


def pages(*own: str, selected: int | None = None) -> dict:
    lines = ["## Pages", *[f"{n}: {t}{' [selected]' if selected == n else ''}"
                           for n, t in [(1, "Secret Invented Tab (https://private.example.invalid/inbox)"),
                                        (2, "Another Invented Tab (https://bank.example.invalid/)")]]]
    lines += [f"{7 + i}: {t}{' [selected]' if selected == 7 + i else ''}" for i, t in enumerate(own)]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


class LeaseCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lease.json"
        self.now = 1000.0
        self.dead: set[int] = set()

    def lease(self, holder: str, pid: int) -> ob.Lease:
        return ob.Lease(self.path, holder, f"the {holder} worker", pid, idle=180,
                        alive=lambda p: p not in self.dead, clock=lambda: self.now)

    def test_second_agent_waits_with_a_clear_message(self):
        a, b = self.lease("alpha", 11), self.lease("beta", 22)
        self.assertIsNone(a.acquire())
        held = b.acquire()
        self.assertEqual(held["holder"], "alpha")
        message = b.wait_message(held)
        self.assertIn("the alpha worker", message)
        self.assertIn("One agent drives it at a time", message)
        self.assertIn("retry", message)

    def test_holder_renews_itself(self):
        a = self.lease("alpha", 11)
        a.acquire()
        self.now += 170
        self.assertIsNone(a.acquire())
        self.now += 170  # still inside the window, because the last call renewed it
        self.assertIsNotNone(self.lease("beta", 22).acquire())

    def test_idle_lease_frees(self):
        self.lease("alpha", 11).acquire()
        self.now += 181
        self.assertIsNone(self.lease("beta", 22).acquire())

    def test_dead_holder_frees(self):
        self.lease("alpha", 11).acquire()
        self.dead.add(11)
        self.assertIsNone(self.lease("beta", 22).acquire())

    def test_a_live_holder_is_found_with_the_real_process_check(self):
        real = ob.Lease(self.path, "alpha", "the alpha worker", os.getpid())
        self.assertIsNone(real.acquire())
        other = ob.Lease(self.path, "beta", "the beta worker", os.getpid() + 1)
        self.assertEqual(other.acquire()["holder"], "alpha")
        self.assertIn("alpha", json.dumps(real.read()))

    def test_a_dead_holder_is_found_with_the_real_process_check(self):
        ob.Lease(self.path, "alpha", "the alpha worker", 4000000).acquire()
        self.assertIsNone(ob.Lease(self.path, "beta", "the beta worker", os.getpid()).acquire())

    def test_touch_never_takes_a_lease_back(self):
        a = self.lease("alpha", 11)
        a.acquire()
        a.release()
        a.touch()
        self.assertIsNone(a.read())

    def test_release_frees_only_own_lease(self):
        a, b = self.lease("alpha", 11), self.lease("beta", 22)
        a.acquire()
        b.release()
        self.assertEqual(a.read()["holder"], "alpha")
        a.release()
        self.assertIsNone(a.read())


class PolicyCases(unittest.TestCase):
    def opened(self) -> ob.Policy:
        policy = ob.Policy()
        allowed, args, _, words = policy.before("new_page", {"url": "https://example.com"})
        self.assertTrue(allowed)
        policy.after("new_page", args, pages("Mine (https://example.com)", selected=7), words)
        return policy

    def test_new_page_is_forced_to_background(self):
        allowed, args, _, _ = ob.Policy().before("new_page", {"url": "https://example.com", "background": False})
        self.assertTrue(allowed)
        self.assertIs(args["background"], True)

    def test_owner_words_allow_foreground_and_are_stripped(self):
        allowed, args, _, words = ob.Policy().before(
            "new_page", {"url": "https://example.com", "background": False, ob.APPROVAL_ARG: "bring it up please"})
        self.assertTrue(allowed)
        self.assertIs(args["background"], False)
        self.assertNotIn(ob.APPROVAL_ARG, args)
        self.assertEqual(words, "bring it up please")

    def test_tab_listings_hide_the_owners_tabs(self):
        policy = self.opened()
        result = policy.after("list_pages", {}, pages("Mine (https://example.com)", selected=7), None)
        text = result["content"][0]["text"]
        self.assertNotIn("private.example.invalid", text)
        self.assertNotIn("Secret Invented Tab", text)
        self.assertNotIn("bank.example.invalid", text)
        self.assertIn("Mine", text)
        self.assertIn("2 other tabs of the owner's hidden", text)

    def test_new_page_response_is_redacted_too(self):
        policy = ob.Policy()
        result = policy.after("new_page", {}, pages("Mine (https://example.com)", selected=7), None)
        self.assertNotIn("private.example.invalid", result["content"][0]["text"])

    def test_structured_pages_are_filtered(self):
        policy = self.opened()
        raw = {**pages("Mine", selected=7), "structuredContent": {
            "pages": [{"id": 1, "url": "https://private.example.invalid/"}, {"id": 7, "url": "https://example.com"}],
            "extensionPages": [{"id": 3, "url": "chrome-extension://invented"}]}}
        result = policy.after("list_pages", {}, raw, None)
        self.assertEqual([p["id"] for p in result["structuredContent"]["pages"]], [7])
        self.assertNotIn("extensionPages", result["structuredContent"])

    def test_owner_words_show_the_listing(self):
        policy = self.opened()
        allowed, args, _, words = policy.before("list_pages", {ob.APPROVAL_ARG: "show my tabs"})
        self.assertTrue(allowed)
        result = policy.after("list_pages", args, pages("Mine", selected=7), words)
        self.assertIn("private.example.invalid", result["content"][0]["text"])

    def test_a_section_after_pages_is_kept(self):
        policy = self.opened()
        raw = {"content": [{"type": "text", "text": OWNER_TABS.replace("1:", "## Pages\n1:", 1) + "\n## Network\n1: GET ok"}]}
        text = policy.after("list_pages", {}, raw, None)["content"][0]["text"]
        self.assertIn("## Network\n1: GET ok", text)

    def test_page_tools_refused_before_any_own_tab(self):
        allowed, _, refusal, _ = ob.Policy().before("take_snapshot", {"pageId": 1})
        self.assertFalse(allowed)
        self.assertIn("not a tab this session opened", refusal)

    def test_page_tools_work_on_own_tab_and_need_a_page_id(self):
        policy = self.opened()
        self.assertTrue(policy.before("take_snapshot", {"pageId": 7})[0])
        self.assertFalse(policy.before("take_snapshot", {})[0])
        self.assertFalse(policy.before("take_snapshot", {"pageId": [7]})[0])
        self.assertFalse(policy.before("take_snapshot", {"pageId": True})[0])

    def test_whole_browser_tools_have_no_override(self):
        policy = self.opened()
        for tool in ("resize_page", "lighthouse_audit", "take_heapsnapshot"):
            self.assertFalse(policy.before(tool, {"pageId": 7, ob.APPROVAL_ARG: "go ahead"})[0], tool)

    def test_approval_for_something_else_stays_in_the_background(self):
        _, args, _, _ = ob.Policy().before("new_page", {"url": "https://example.com", ob.APPROVAL_ARG: "read that tab"})
        self.assertIs(args["background"], True)

    def test_isolated_context_needs_owner_words(self):
        _, args, _, _ = ob.Policy().before("new_page", {"url": "https://example.com", "isolatedContext": "x"})
        self.assertNotIn("isolatedContext", args)

    def test_owner_tab_titled_selected_is_not_adopted(self):
        policy = ob.Policy()
        raw = {"content": [{"type": "text", "text": "## Pages\n3: Items [selected] - Shop (https://shop.example.invalid/)\n"
                                                    "9: Mine (https://example.com/) [selected]"}]}
        policy.after("new_page", {}, raw, None)
        self.assertEqual(policy.opened, {9})

    def test_unicode_line_breaks_in_titles_do_not_defeat_redaction(self):
        policy = self.opened()
        for title in ("Bank\u2028Statement", "x\u2028## y", "a\x85b", "a\x0bb", "a\x1cb"):
            raw = {"content": [{"type": "text", "text": f"## Pages\n1: {title} (https://bank.example.invalid/)\n"
                                                        "2: Later (https://later.example.invalid/)\n7: Mine (https://example.com/) [selected]"}]}
            text = policy.after("list_pages", {}, raw, None)["content"][0]["text"]
            self.assertNotIn("example.invalid", text, repr(title))
            self.assertNotIn("Statement", text, repr(title))
            self.assertIn("Mine", text)

    def test_extension_sections_are_hidden(self):
        policy = self.opened()
        raw = {"content": [{"type": "text", "text": "## Extension Pages\n4: chrome-extension://invented/x"}]}
        self.assertNotIn("invented", policy.after("list_pages", {}, raw, None)["content"][0]["text"])

    def test_reconnect_drops_stale_ids_and_says_so(self):
        policy = self.opened()
        raw = {"content": [{"type": "text", "text": "Note: the browser was restarted or reconnected since the last call. Page ids have changed.\n## Pages\n20: Secret (https://private.example.invalid/)"}]}
        out = policy.after("list_pages", {}, raw, None)
        self.assertEqual(policy.opened, set())
        self.assertFalse(policy.before("close_page", {"pageId": 7})[0])
        self.assertIn("no longer tracked", json.dumps(out))
        self.assertNotIn("private.example.invalid", json.dumps(out))

    def test_failed_new_page_is_adopted_only_when_it_made_a_new_tab(self):
        policy = ob.Policy()
        policy.note_baseline({"content": [{"type": "text", "text": "## Pages\n1: Secret (https://p.example.invalid/) [selected]"}]})
        policy.reconcile({"content": [{"type": "text", "text": "## Pages\n1: Secret (https://p.example.invalid/) [selected]"}]})
        self.assertEqual(policy.opened, set())
        policy.reconcile({"content": [{"type": "text", "text": "## Pages\n1: Secret (https://p.example.invalid/)\n7: about:blank [selected]"}]})
        self.assertEqual(policy.opened, {7})

    def test_page_tools_refused_on_owner_tab(self):
        policy = self.opened()
        self.assertFalse(policy.before("take_snapshot", {"pageId": 1})[0])
        self.assertFalse(policy.before("evaluate_script", {"pageId": 2, "function": "() => 1"})[0])
        self.assertFalse(policy.before("select_page", {"pageId": 1})[0])

    def test_owner_words_reach_another_tab_for_that_call_only(self):
        policy = self.opened()
        self.assertTrue(policy.before("take_snapshot", {"pageId": 1, ob.APPROVAL_ARG: "read that tab"})[0])
        self.assertFalse(policy.before("take_snapshot", {"pageId": 1})[0])

    def test_bring_to_front_refused_without_owner_words(self):
        policy = self.opened()
        allowed, _, refusal, _ = policy.before("select_page", {"pageId": 7, "bringToFront": True})
        self.assertFalse(allowed)
        self.assertIn("Focus stays with the owner", refusal)
        self.assertTrue(policy.before("select_page", {"pageId": 7, "bringToFront": True, ob.APPROVAL_ARG: "show me"})[0])

    def test_closing_another_tab_has_no_override(self):
        policy = self.opened()
        self.assertFalse(policy.before("close_page", {"pageId": 1})[0])
        self.assertFalse(policy.before("close_page", {"pageId": 1, ob.APPROVAL_ARG: "close it"})[0])
        self.assertTrue(policy.before("close_page", {"pageId": 7})[0])

    def test_closing_own_tab_leaves_no_way_onto_the_owners_tab(self):
        policy = self.opened()
        policy.after("close_page", {"pageId": 7}, {"content": [{"type": "text", "text": "## Pages\n1: Secret [selected]"}]}, None)
        self.assertEqual(policy.opened, set())
        self.assertFalse(policy.before("take_snapshot", {"pageId": 1})[0])
        self.assertFalse(policy.before("take_snapshot", {"pageId": 7})[0])

    def test_internal_pages_have_no_override(self):
        policy = self.opened()
        for url in ("brave://settings/passwords", "chrome://password-manager", "file:///C:/invented.txt",
                    "chrome-extension://invented/x.html", "view-source:https://example.com"):
            self.assertFalse(policy.before("navigate_page", {"url": url, "pageId": 7})[0], url)
            self.assertFalse(policy.before("new_page", {"url": url, ob.APPROVAL_ARG: "go"})[0], url)
        self.assertTrue(policy.before("navigate_page", {"url": "https://example.com/x", "pageId": 7})[0])
        self.assertTrue(policy.before("navigate_page", {"type": "reload", "pageId": 7})[0])

    def test_scripts_reading_stored_secrets_have_no_override(self):
        policy = self.opened()
        for body in ("() => document.cookie", "() => localStorage.getItem('x')", "() => window.sessionStorage",
                     "() => indexedDB.databases()", "() => navigator.credentials.get({})"):
            self.assertFalse(policy.before("evaluate_script", {"function": body, "pageId": 7, ob.APPROVAL_ARG: "ok"})[0], body)
        self.assertTrue(policy.before("evaluate_script", {"function": "() => document.title", "pageId": 7})[0])


FAKE_SERVER = textwrap.dedent('''
    import json, sys, time
    behavior = sys.argv[2] if len(sys.argv) > 2 else ""
    with open(sys.argv[1] + ".starts", "a") as fh:
        fh.write("started\\n")
    next_id = 7
    pages = {1: "Secret Invented Tab (https://private.example.invalid/inbox)"}
    if behavior == "hostile":
        pages[3] = "Items [selected] - Shop (https://shop.example.invalid/)"
        pages[4] = "Bank\\u2028Statement (https://bank.example.invalid/acct)"
    selected = 1
    served = 0
    def listing():
        return "## Pages\\n" + "\\n".join(f"{i}: {t}{' [selected]' if i == selected else ''}" for i, t in pages.items())
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        out = {}
        if m["method"] == "tools/list":
            out = {"tools": [{"name": "new_page", "inputSchema": {"properties": {}}},
                             {"name": "close_page", "inputSchema": {"properties": {}}},
                             {"name": "resize_page", "inputSchema": {"properties": {}}}]}
        elif m["method"] == "tools/call":
            name, args = m["params"]["name"], m["params"]["arguments"]
            with open(sys.argv[1], "a") as fh:
                fh.write(json.dumps([name, args]) + "\\n")
            served += 1
            if behavior == "die" and served == 2:
                sys.exit(3)
            if behavior == "hang" and served == 2:
                time.sleep(30)
            if behavior == "slow" and name == "new_page":
                time.sleep(0.6)
            if behavior == "collide":
                print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "method": "roots/list"}), flush=True)
            if behavior == "reconnect" and name == "list_pages":
                out = {"content": [{"type": "text", "text": listing() + "\\nNote: the browser was restarted or reconnected."}]}
            elif behavior == "down":
                out = {"content": [{"type": "text", "text": "Could not connect to Chrome. Check it."}], "isError": True}
            elif name == "new_page":
                pages[next_id] = "Mine (" + args["url"] + ")"; selected = next_id; next_id += 1
                if "dns-failure" in args["url"]:
                    out = {"content": [{"type": "text", "text": "net::ERR_NAME_NOT_RESOLVED"}], "isError": True}
                else:
                    out = {"content": [{"type": "text", "text": listing()}]}
            elif name == "close_page":
                pages.pop(args["pageId"], None); selected = 1
                out = {"content": [{"type": "text", "text": listing()}]}
            elif name == "list_pages":
                out = {"content": [{"type": "text", "text": listing()}]}
            elif "pageId" not in args:
                out = {"content": [{"type": "text", "text": "Required at pageId"}], "isError": True}
            else:
                out = {"content": [{"type": "text", "text": "done on " + str(args["pageId"])}]}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": out}), flush=True)
''')


class BlockingLines(io.RawIOBase):
    """Feeds client lines, then ends the stream, so shutdown runs as it does on stdin EOF."""

    def __init__(self, lines: list[dict], delay: float = 0.25):
        self.lines = [(json.dumps(m) + "\n").encode() for m in lines]
        self.delay = delay

    def __iter__(self):
        import time
        for line in self.lines:
            yield line
            time.sleep(self.delay)
        time.sleep(self.delay)


class ProxyCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "fake.py").write_text(FAKE_SERVER, encoding="utf-8")
        self.calls = self.dir / "calls.jsonl"
        self.env = {**os.environ, "SWITCHBOARD_OWNER_BROWSER_STATE": str(self.dir), "SWITCHBOARD_MACHINE": "INVENTED",
                    "SWITCHBOARD_LOCAL_DIR": str(self.dir / "local"), "CLAUDE_CODE_SESSION_ATTENDED": "1"}
        self.env.pop("SWITCHBOARD_WORKSTREAM_SLUG", None)

    def drive(self, client: list[dict], env: dict | None = None, cwd: str = "C:/invented", lease=None,
              behavior: str = "", delay: float = 0.25, call_wait: float = 120) -> list[dict]:
        env = env or self.env
        out = io.BytesIO()
        lease = lease or ob.Lease(self.dir / "lease.json", "me", "the invented worker", os.getpid())
        proxy = ob.Proxy([sys.executable, str(self.dir / "fake.py"), str(self.calls), behavior], env, cwd,
                         BlockingLines(client, delay), out, lease, call_wait=call_wait)
        proxy.run()
        self.proxy = proxy
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def forwarded(self) -> list[list]:
        return [json.loads(l) for l in self.calls.read_text().splitlines()] if self.calls.exists() else []

    @staticmethod
    def call(i: int, name: str, **args) -> dict:
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}

    def test_open_read_close_flow(self):
        answers = self.drive([self.call(1, "new_page", url="https://example.com"), self.call(2, "close_page", pageId=7)])
        self.assertNotIn("private.example.invalid", json.dumps(answers))
        self.assertIn("Mine", answers[0]["result"]["content"][0]["text"])
        self.assertEqual(self.forwarded()[0], ["list_pages", {}])  # the baseline, so the owner's tabs are never adopted
        self.assertEqual(self.forwarded()[1], ["new_page", {"url": "https://example.com", "background": True}])
        self.assertEqual(self.forwarded()[2], ["close_page", {"pageId": 7}])
        self.assertFalse(answers[1]["result"].get("isError"))

    def test_a_left_open_tab_is_closed_on_exit_and_lease_freed(self):
        self.drive([self.call(1, "new_page", url="https://example.com")])
        self.assertIn(["close_page", {"pageId": 7}], self.forwarded())
        self.assertFalse((self.dir / "lease.json").exists())

    def test_the_owners_tab_is_never_closed_on_exit(self):
        self.drive([self.call(1, "new_page", url="https://example.com")])
        self.assertNotIn(["close_page", {"pageId": 1}], self.forwarded())

    def test_refused_call_never_reaches_the_server(self):
        answers = self.drive([self.call(1, "take_snapshot", pageId=1)])
        self.assertTrue(answers[0]["result"]["isError"])
        self.assertEqual(self.forwarded(), [])

    def test_second_session_gets_wait_message_and_nothing_is_forwarded(self):
        # The real process check, so a broken import shows up here: the holder's pid is alive.
        other = ob.Lease(self.dir / "lease.json", "other", "the invented other worker", os.getpid())
        self.assertIsNone(other.acquire())
        mine = ob.Lease(self.dir / "lease.json", "me", "the invented worker", os.getpid() + 1)
        answers = self.drive([self.call(1, "list_pages")], lease=mine)
        self.assertIn("the invented other worker", answers[0]["result"]["content"][0]["text"])
        self.assertTrue(answers[0]["result"]["isError"])
        self.assertEqual(self.forwarded(), [])

    def test_scheduled_job_is_refused_and_pointed_at_headless(self):
        for env, cwd in (({**self.env, "SWITCHBOARD_SCHEDULED_JOB": "1"}, "C:/invented"),
                         ({**self.env, "SWITCHBOARD_WORKSTREAM_SLUG": "scheduled-jobs"}, "C:/invented"),
                         ({**self.env, "CLAUDE_CODE_SESSION_ATTENDED": "0"}, "C:/invented"),
                         (self.env, "C:\\invented\\routines\\scheduled-jobs")):
            answers = self.drive([self.call(1, "list_pages")], env=env, cwd=cwd)
            self.assertIn("chrome-devtools-headless", answers[0]["result"]["content"][0]["text"])
        self.assertEqual(self.forwarded(), [])

    def test_unreachable_browser_gets_the_plain_message(self):
        answers = self.drive([self.call(1, "list_pages")], behavior="down")
        text = answers[0]["result"]["content"][0]["text"]
        self.assertIn("not reachable", text)
        self.assertIn("ask-cos.sh", text)
        self.assertIn("never start, restart, or reconfigure", text)

    def test_tools_list_offers_the_approval_argument_except_on_close(self):
        answers = self.drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}])
        tools = {t["name"]: t["inputSchema"]["properties"] for t in answers[0]["result"]["tools"]}
        self.assertIn(ob.APPROVAL_ARG, tools["new_page"])
        self.assertNotIn(ob.APPROVAL_ARG, tools["close_page"])

    def test_a_call_nobody_answers_fails_with_what_to_do_even_after_attach(self):
        answers = self.drive([self.call(1, "list_pages"), self.call(2, "list_pages")], behavior="hang",
                             delay=2.5, call_wait=1)
        second = [a for a in answers if a.get("id") == 2]
        self.assertEqual(len(second), 1)
        self.assertIn("click Allow", second[0]["result"]["content"][0]["text"])

    def test_a_dead_server_answers_the_open_call_and_later_ones(self):
        answers = self.drive([self.call(1, "list_pages"), self.call(2, "list_pages"), self.call(3, "list_pages")],
                             behavior="die", delay=0.5)
        by_id = {a["id"]: a for a in answers}
        self.assertFalse(by_id[1]["result"].get("isError"))
        self.assertIn("stopped running", by_id[2]["result"]["content"][0]["text"])
        self.assertIn("stopped running", by_id[3]["result"]["content"][0]["text"])

    def test_malformed_calls_are_answered_and_the_proxy_keeps_going(self):
        answers = self.drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "take_snapshot", "arguments": [1]}},
            self.call(3, "take_snapshot", pageId=[7]),
            self.call(4, "list_pages"),
        ])
        by_id = {a["id"]: a for a in answers}
        self.assertTrue(by_id[1]["result"]["isError"])
        self.assertTrue(by_id[2]["result"]["isError"])
        self.assertTrue(by_id[3]["result"]["isError"])
        self.assertFalse(by_id[4]["result"].get("isError"))

    def test_hostile_titles_end_to_end(self):
        answers = self.drive([self.call(1, "new_page", url="https://example.com"), self.call(2, "close_page", pageId=3)],
                             behavior="hostile")
        dumped = json.dumps(answers)
        for secret in ("example.invalid", "Statement", "Shop", "Secret"):
            self.assertNotIn(secret, dumped)
        self.assertEqual(self.proxy.policy.opened, set())  # our tab 7 was adopted and closed on exit
        self.assertIn(["close_page", {"pageId": 7}], self.forwarded())
        self.assertNotIn(["close_page", {"pageId": 3}], self.forwarded())

    def test_failed_navigation_tab_is_owned_and_closed_on_exit(self):
        answers = self.drive([self.call(1, "new_page", url="https://dns-failure.example.invalid/")])
        self.assertTrue(answers[0]["result"]["isError"])
        self.assertIn(["close_page", {"pageId": 7}], self.forwarded())

    def test_cancelled_new_page_is_owned_and_closed_on_exit(self):
        self.drive([self.call(1, "new_page", url="https://example.com"),
                    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}], delay=0.02)
        self.assertIn(["close_page", {"pageId": 7}], self.forwarded())

    def test_a_slow_cancelled_new_page_is_still_closed_on_exit(self):
        # Wrong impl: the exit drain waited only for client calls. A cancelled new_page leaves
        # them at once, so a client that cancels and disconnects before the tab listing answers
        # left the tab open (the test above failed about half the time on Windows).
        self.drive([self.call(1, "new_page", url="https://example.com"),
                    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}],
                   behavior="slow", delay=0.02)
        self.assertIn(["close_page", {"pageId": 7}], self.forwarded())
        self.assertEqual(self.proxy.policy.opened, set())

    def test_a_tab_still_opening_at_exit_is_closed(self):
        self.drive([self.call(1, "new_page", url="https://example.com")], behavior="slow", delay=0)
        self.assertIn(["close_page", {"pageId": 7}], self.forwarded())

    def test_a_server_request_with_a_matching_id_cannot_unlock_a_raw_listing(self):
        answers = self.drive([self.call(1, "list_pages")], behavior="collide")
        self.assertNotIn("private.example.invalid", json.dumps(answers))
        self.assertTrue(any(a.get("method") == "roots/list" for a in answers))

    def test_whole_browser_tool_never_reaches_the_server(self):
        answers = self.drive([self.call(1, "resize_page", pageId=7, width=10, height=10)])
        self.assertIn("off limits", answers[0]["result"]["content"][0]["text"])
        self.assertEqual(self.forwarded(), [])

    def test_a_refused_call_takes_no_lease(self):
        self.drive([self.call(1, "take_snapshot", pageId=1)], delay=0.3)
        self.assertEqual(self.proxy.lease.read(), None)

    def test_log_never_holds_a_url_or_tab_title(self):
        self.drive([self.call(1, "new_page", url="https://private.example.invalid/x"), self.call(2, "list_pages")])
        self.assertNotIn("example.invalid", (self.dir / "owner-browser.jsonl").read_text() if (self.dir / "owner-browser.jsonl").exists() else "")


class ProfileCases(unittest.TestCase):
    """The agent profile is the default; the owner's Brave needs a stated signed-in need. Both log their profile."""

    setUp = ProxyCases.setUp
    forwarded = ProxyCases.forwarded
    call = staticmethod(ProxyCases.call)

    def drive(self, client: list[dict], profile: str, ensure=None, after_open=None, lease=None,
              env: dict | None = None) -> list[dict]:
        out = io.BytesIO()
        if lease is None:
            lease = (ob.NullLease("the invented worker") if profile == "agent" else
                     ob.Lease(self.dir / "lease.json", "me", "the invented worker", os.getpid()))
        proxy = ob.Proxy([sys.executable, str(self.dir / "fake.py"), str(self.calls), ""], env or self.env,
                         "C:/invented", BlockingLines(client), out, lease, profile=profile,
                         require_need=profile == "owner", ensure=ensure, after_open=after_open)
        proxy.run()
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def log(self) -> list[dict]:
        path = self.dir / "owner-browser.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_owner_profile_refuses_a_first_call_with_no_stated_need_and_forwards_nothing(self):
        answers = self.drive([self.call(1, "list_pages")], "owner")
        self.assertTrue(answers[0]["result"]["isError"])
        self.assertIn(ob.NEED_ARG, answers[0]["result"]["content"][0]["text"])
        self.assertEqual(self.forwarded(), [])
        self.assertFalse((self.dir / "lease.json").exists())

    def test_owner_profile_forwards_once_the_need_is_stated_and_strips_it(self):
        answers = self.drive([self.call(1, "list_pages", signedInNeed="brief says Browser: signed-in"),
                              self.call(2, "list_pages")], "owner")
        self.assertFalse(answers[0]["result"].get("isError"))
        self.assertFalse(answers[1]["result"].get("isError"))
        self.assertEqual(self.forwarded(), [["list_pages", {}], ["list_pages", {}]])
        logged = self.log()
        self.assertTrue(any(e.get("decision") == "signed-in-need" and e.get("profile") == "owner"
                            and e.get("words") == "brief says Browser: signed-in" for e in logged))
        self.assertTrue(any(e.get("decision") == "session-profile" and e.get("profile") == "owner" for e in logged))

    def test_owner_tools_list_asks_for_the_need(self):
        answers = self.drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}], "owner")
        tools = {t["name"]: t["inputSchema"]["properties"] for t in answers[0]["result"]["tools"]}
        self.assertIn(ob.NEED_ARG, tools["new_page"])
        self.assertNotIn(ob.NEED_ARG, tools["resize_page"])

    def test_agent_profile_needs_no_stated_need_and_takes_no_lease(self):
        other = ob.Lease(self.dir / "lease.json", "other", "the invented other worker", os.getpid())
        self.assertIsNone(other.acquire())  # the owner's browser is busy, which never blocks the agent profile
        answers = self.drive([self.call(1, "new_page", url="https://example.com")], "agent")
        self.assertFalse(answers[0]["result"].get("isError"))
        self.assertIn(["new_page", {"url": "https://example.com", "background": True}], self.forwarded())
        self.assertTrue(any(e.get("decision") == "session-profile" and e.get("profile") == "agent" for e in self.log()))
        agent_tools = self.drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}], "agent")
        self.assertNotIn(ob.NEED_ARG, agent_tools[0]["result"]["tools"][0]["inputSchema"]["properties"])

    def test_agent_profile_keeps_every_guard(self):
        answers = self.drive([self.call(1, "take_snapshot", pageId=1), self.call(2, "close_page", pageId=1),
                              self.call(3, "new_page", url="brave://settings/passwords"),
                              self.call(4, "evaluate_script", pageId=1, function="() => document.cookie")], "agent")
        self.assertTrue(all(a["result"]["isError"] for a in answers))
        self.assertEqual(self.forwarded(), [])

    def test_no_words_raise_the_agent_window(self):
        answers = self.drive([self.call(1, "new_page", url="https://example.com", background=False,
                                        ownerApproved="invented words"),
                              self.call(2, "select_page", pageId=7, bringToFront=True, ownerApproved="invented words")],
                             "agent")
        self.assertIn(["new_page", {"url": "https://example.com", "background": True}], self.forwarded())
        self.assertTrue(answers[1]["result"]["isError"])
        self.assertNotIn("select_page", [name for name, _ in self.forwarded()])
        tools = self.drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}], "agent")
        self.assertNotIn(ob.APPROVAL_ARG, tools[0]["result"]["tools"][0]["inputSchema"]["properties"])

    def test_agent_screenshot_is_refused_with_the_reason_instead_of_hanging(self):
        answers = self.drive([self.call(1, "take_screenshot", pageId=7)], "agent")
        self.assertIn("minimized", answers[0]["result"]["content"][0]["text"])
        self.assertEqual(self.forwarded(), [])

    def test_agent_browser_that_cannot_start_is_refused_before_forwarding(self):
        answers = self.drive([self.call(1, "list_pages")], "agent", ensure=lambda: ob.AGENT_DOWN)
        self.assertIn("agent status", answers[0]["result"]["content"][0]["text"])
        self.assertEqual(self.forwarded(), [])

    def test_a_new_tab_in_the_agent_profile_rechecks_the_window(self):
        opened = []
        self.drive([self.call(1, "new_page", url="https://example.com")], "agent", ensure=lambda: None,
                   after_open=lambda: opened.append(True))
        self.assertTrue(opened)

    def test_scheduled_jobs_stay_on_headless_in_the_agent_profile_too(self):
        answers = self.drive([self.call(1, "list_pages")], "agent", env={**self.env, "SWITCHBOARD_SCHEDULED_JOB": "1"})
        self.assertIn("chrome-devtools-headless", answers[0]["result"]["content"][0]["text"])
        self.assertEqual(self.forwarded(), [])


class SandboxCases(unittest.TestCase):
    """Under SWITCHBOARD_TEST_SANDBOX nothing reaches the owner's real Brave (CoS, 2026-11-09, after a stray Allow dialog)."""

    REAL = str(Path.home() / "AppData" / "Local")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {ob.KEY: "brave", "SWITCHBOARD_TEST_SANDBOX": "1", "SWITCHBOARD_OWNER_BROWSER_STATE": self.tmp.name,
                    "SWITCHBOARD_MACHINE": "INVENTED", "LOCALAPPDATA": self.REAL}

    def test_the_real_owner_profile_is_blocked_only_under_the_sandbox(self):
        self.assertTrue(ob.sandbox_blocks_owner("brave", self.env))
        self.assertTrue(ob.sandbox_blocks_owner("brave", {**self.env, "LOCALAPPDATA": self.REAL.replace("\\", "/") + "/"}))
        self.assertFalse(ob.sandbox_blocks_owner("brave", {**self.env, "LOCALAPPDATA": self.tmp.name}))
        self.assertFalse(ob.sandbox_blocks_owner("brave", {**self.env, "SWITCHBOARD_TEST_SANDBOX": ""}))
        with mock.patch.object(ob.Path, "home", return_value=Path(self.tmp.name)):  # a test with a temporary home
            self.assertTrue(ob.sandbox_blocks_owner("brave", self.env))

    def test_owner_proxy_and_broker_exit_before_starting_anything(self):
        for argv in (["proxy", "--profile", "owner", "--mcp-version", "1.2.3"], ["broker", "--mcp-version", "1.2.3"]):
            with mock.patch.dict(os.environ, self.env, clear=False), \
                    mock.patch.object(ob, "Proxy") as proxy, mock.patch.object(ob, "Broker") as broker, \
                    mock.patch.object(ob, "connect_broker") as joined, mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(ob.main(argv), 2)
            proxy.assert_not_called()
            broker.assert_not_called()
            joined.assert_not_called()

    def test_the_agent_profile_still_runs_under_the_sandbox(self):
        with mock.patch.dict(os.environ, self.env, clear=False), mock.patch.object(ob, "Proxy") as proxy:
            ob.main(["proxy", "--profile", "agent", "--mcp-version", "1.2.3"])
        self.assertEqual(proxy.call_args.kwargs["profile"], "agent")

    def test_a_broker_for_another_profile_is_never_joined_or_stopped(self):
        empty = {**self.env, "LOCALAPPDATA": self.tmp.name}
        real_profile = str(ob.profile_dir("brave", self.env))
        with self.assertRaises(RuntimeError):
            ob.check_broker_profile({"profile": real_profile}, empty)
        with self.assertRaises(RuntimeError):
            ob.check_broker_profile({"port": 1}, empty)  # names no profile, and this is a test
        ob.check_broker_profile({"port": 1}, {**empty, "SWITCHBOARD_TEST_SANDBOX": ""})
        ob.check_broker_profile({"profile": str(ob.profile_dir("brave", empty))}, empty)
        with mock.patch.object(ob, "stop_broker") as stopped, mock.patch.object(ob, "_broker_hello") as hello:
            ob.broker_path(empty).write_text(json.dumps({"port": 1, "token": "t", "profile": real_profile,
                                                         "version": "1.2.3", "code": ob.code_stamp()}))
            with self.assertRaises(RuntimeError):
                ob.connect_broker("1.2.3", empty)
        stopped.assert_not_called()
        hello.assert_not_called()


class AgentBrowserCases(unittest.TestCase):
    """The agent profile's Brave, with an invented port table: no browser starts here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"SWITCHBOARD_OWNER_BROWSER_STATE": self.tmp.name, "SWITCHBOARD_MACHINE": "INVENTED",
                    "LOCALAPPDATA": "C:/InventedAppData"}

    def browser(self, listener, uses_dir=lambda pid, folder: True, started=lambda pid: 1000) -> "ob.AgentBrowser":
        return ob.AgentBrowser(self.env, listener=listener, uses_dir=uses_dir, started=started)

    def test_profile_lives_in_appdata_not_the_dropbox_synced_local_folder(self):
        path = str(ob.agent_dir(self.env)).replace("\\", "/")
        self.assertEqual(path, "C:/InventedAppData/switchboard/agent-brave")
        self.assertNotIn("Dropbox", path)

    def test_child_attaches_to_the_local_port_only(self):
        command = ob.agent_child_command("1.2.3", self.env)
        self.assertIn("--browserUrl=http://127.0.0.1:9333", command)
        self.assertIn("--redactNetworkHeaders", command)
        self.assertNotIn("--autoConnect", command)

    def test_only_the_agent_profile_listening_on_localhost_counts(self):
        self.assertIsNone(self.browser(lambda port: []).owner_pid())
        self.assertIsNone(self.browser(lambda port: [("0.0.0.0", 41)]).owner_pid())
        self.assertIsNone(self.browser(lambda port: [("127.0.0.1", 41)], lambda pid, folder: False).owner_pid())
        checks = []
        browser = self.browser(lambda port: [("127.0.0.1", 41)], lambda pid, folder: checks.append(pid) or True)
        self.assertEqual(browser.owner_pid(), 41)
        self.assertEqual(browser.owner_pid(), 41)
        self.assertEqual(checks, [41])  # the slow command-line read runs once per process

    def test_a_second_listener_on_all_addresses_does_not_hide_brave(self):
        browser = self.browser(lambda port: [("0.0.0.0", 99), ("127.0.0.1", 41)])
        self.assertEqual(browser.owner_pid(), 41)

    def test_a_reused_process_id_is_checked_again(self):
        start = [1000]
        answers = [True, False]
        browser = self.browser(lambda port: [("127.0.0.1", 41)], lambda pid, folder: answers.pop(0),
                               started=lambda pid: start[0])
        self.assertEqual(browser.owner_pid(), 41)
        start[0] = 2000  # Windows gave the same id to a new process
        self.assertIsNone(browser.owner_pid())

    def test_a_session_waits_while_another_is_still_starting_brave(self):
        listening = []
        browser = self.browser(lambda port: listening)
        polls = [0]

        def running():
            polls[0] += 1
            if polls[0] == 3:
                listening.append(("127.0.0.1", 41))  # the other session's Brave now answers
            return True
        with mock.patch.object(browser, "running", side_effect=running), \
                mock.patch.object(browser, "_version", return_value={
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/invented"}), \
                mock.patch.object(ob, "start_minimized") as started, mock.patch.object(ob.time, "sleep"):
            self.assertIsNone(browser.ensure())
        started.assert_not_called()

    def test_another_program_on_the_port_is_never_driven(self):
        browser = self.browser(lambda port: [("127.0.0.1", 41)], lambda pid, folder: False)
        with mock.patch.object(browser, "running", return_value=False), \
                mock.patch.object(ob, "start_minimized") as started:
            self.assertEqual(browser.ensure(), ob.AGENT_PORT_TAKEN)
        started.assert_not_called()

    def test_command_line_must_name_the_exact_folder(self):
        folder = Path("C:/InventedAppData/switchboard/agent-brave")
        self.assertTrue(ob.names_dir(f'brave.exe --user-data-dir={folder} --remote-debugging-port=9333', folder))
        self.assertTrue(ob.names_dir(f'brave.exe "--user-data-dir={folder}"', folder))
        self.assertFalse(ob.names_dir(f'brave.exe --user-data-dir={folder}-old --x', folder))
        self.assertFalse(ob.names_dir('brave.exe --user-data-dir=C:/InventedAppData/BraveSoftware/User Data', folder))

    def test_prepare_marks_a_clean_exit_and_drops_the_saved_session(self):
        folder = Path(self.tmp.name) / "agent"
        (folder / "Default" / "Sessions").mkdir(parents=True)
        (folder / "Default" / "Sessions" / "Session_1").write_text("invented", encoding="utf-8")
        (folder / "Default" / "Preferences").write_text(json.dumps({"profile": {"exit_type": "Crashed"}}),
                                                        encoding="utf-8")
        ob.AgentBrowser({**self.env, "SWITCHBOARD_AGENT_BROWSER_DIR": str(folder)})._prepare()
        prefs = json.loads((folder / "Default" / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(prefs["profile"]["exit_type"], "Normal")
        self.assertEqual(prefs["session"]["restore_on_startup"], 5)
        self.assertFalse((folder / "Default" / "Sessions").exists())


class SettingCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"SWITCHBOARD_LOCAL_DIR": self.tmp.name, "SWITCHBOARD_MACHINE": "INVENTED.local", "LOCALAPPDATA": "C:/InventedAppData"}

    def test_default_is_off(self):
        self.assertEqual(ob.mode(self.env), "off")

    def test_set_keeps_other_keys_and_round_trips(self):
        path = Path(self.tmp.name) / "machines" / "INVENTED.env"
        path.parent.mkdir()
        path.write_text("SWITCHBOARD_COMMS_TRIAGE=off\n", encoding="utf-8")
        ob.set_mode("brave", self.env)
        self.assertEqual(ob.mode(self.env), "brave")
        self.assertIn("SWITCHBOARD_COMMS_TRIAGE=off", path.read_text())
        ob.set_mode("off", self.env)
        self.assertEqual(ob.mode(self.env), "off")
        self.assertEqual(path.read_text(), "SWITCHBOARD_COMMS_TRIAGE=off\n")

    def test_another_machines_file_does_not_count(self):
        other = Path(self.tmp.name) / "machines" / "OTHERBOX.env"
        other.parent.mkdir()
        other.write_text("SWITCHBOARD_OWNER_BROWSER=brave\n", encoding="utf-8")
        self.assertEqual(ob.mode(self.env), "off")

    def test_unknown_browser_is_off(self):
        self.assertEqual(ob.mode({**self.env, ob.KEY: "netscape"}), "off")

    def test_child_attaches_to_the_profile_and_redacts_headers(self):
        command = ob.child_command("brave", "1.2.3", self.env)
        self.assertIn("chrome-devtools-mcp@1.2.3", command)
        self.assertIn("--autoConnect", command)
        self.assertIn("--redactNetworkHeaders", command)
        self.assertIn("--pageIdRouting", command)
        joined = " ".join(command).replace("\\", "/")
        self.assertIn("--userDataDir=C:/InventedAppData/BraveSoftware/Brave-Browser/User Data", joined)
        self.assertNotIn("--headless", joined)
        self.assertNotIn("remote-debugging-port", joined)

    def test_release_is_a_no_op_when_off(self):
        self.assertFalse(ob.release("anything", self.env))

    def test_release_only_frees_the_named_session(self):
        env = {**self.env, ob.KEY: "brave", "SWITCHBOARD_OWNER_BROWSER_STATE": self.tmp.name, "SWITCHBOARD_WORKER_SESSION_ID": "sess-a"}
        lease = ob.make_lease(env, pid=os.getpid())
        lease.acquire()
        self.assertFalse(ob.release("sess-b", env))
        self.assertTrue(ob.release("sess-a", env))
        self.assertIsNone(lease.read())


class InstallPathCases(unittest.TestCase):
    """The install script's MCP step, as a dry run with a stub claude that reports no servers."""

    def install_output(self, switch: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "bin"
            stub.mkdir()
            (stub / "claude").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}", "SWITCHBOARD_OWNER_BROWSER": switch}
            env.pop("CLAUDECODE", None)
            done = subprocess.run(
                ["bash", str(SCRIPTS / "install-claude-code.sh"), "--dry-run", "--allow-nested", "--skip-plugins",
                 "--home", tmp], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=170)
            return done.stdout + done.stderr

    def test_opted_in_machine_gets_the_agent_profile_the_owner_entry_and_the_headless_fallback(self):
        out = self.install_output("brave")
        adds = [line for line in out.splitlines() if "mcp add -s user chrome-devtools" in line]
        agent = next(line for line in adds if " chrome-devtools -e" in line)
        owner = next(line for line in adds if " chrome-devtools-owner -e" in line)
        fallback = next(line for line in adds if " chrome-devtools-headless -e" in line)
        self.assertIn("owner_browser.py proxy --profile agent --mcp-version", agent)
        self.assertIn("owner_browser.py proxy --profile owner --mcp-version", owner)
        self.assertNotIn("--headless", agent + owner)
        self.assertIn("--headless=true", fallback)
        self.assertIn("chrome-devtools-mcp/chrome-profile", fallback)

    def test_every_other_machine_keeps_the_sanctioned_headless_profile(self):
        out = self.install_output("off")
        adds = [line for line in out.splitlines() if "mcp add -s user chrome-devtools" in line]
        self.assertEqual(len(adds), 1)
        self.assertIn("--headless=true", adds[0])
        self.assertNotIn("owner_browser.py", adds[0])


class DoctorCases(unittest.TestCase):
    """check-windows-install.py reads the entry that matches this machine's rule."""

    def findings(self, switch: str, servers: dict) -> dict:
        spec = importlib.util.spec_from_file_location("doctor_for_owner_browser", SCRIPTS / "check-windows-install.py")
        doctor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doctor)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
            with mock.patch.object(doctor.Path, "home", return_value=Path(tmp)), \
                    mock.patch.object(doctor, "mcp_handshake", return_value=(True, "stubbed")), \
                    mock.patch.object(doctor, "owner_browser_mode", return_value=switch):
                doctor.results.clear()
                doctor.check_mcp()
        return {row["check"]: row["level"] for row in doctor.results}

    PROXY = {"command": "python3", "args": ["C:/invented/owner_browser.py", "proxy", "--profile", "agent",
                                            "--mcp-version", "1.2.3"]}
    OWNER = {"command": "python3", "args": ["C:/invented/owner_browser.py", "proxy", "--profile", "owner",
                                            "--mcp-version", "1.2.3"]}
    OLD_PROXY = {"command": "python3", "args": ["C:/invented/owner_browser.py", "proxy", "--mcp-version", "1.2.3"]}
    HEADLESS = {"command": "npx", "args": ["--headless=true", "--userDataDir=C:/invented/.cache/chrome-devtools-mcp/chrome-profile"]}

    def test_opted_in_and_configured(self):
        found = self.findings("brave", {"chrome-devtools": self.PROXY, "chrome-devtools-owner": self.OWNER,
                                        "chrome-devtools-headless": self.HEADLESS})
        self.assertEqual(found["mcp chrome-devtools"], "ok")
        self.assertEqual(found["mcp chrome-devtools-owner"], "ok")
        self.assertEqual(found["mcp chrome-devtools-headless"], "ok")

    def test_an_install_from_before_the_agent_profile_warns_until_rerun(self):
        found = self.findings("brave", {"chrome-devtools": self.OLD_PROXY, "chrome-devtools-headless": self.HEADLESS})
        self.assertEqual(found["mcp chrome-devtools"], "warn")
        self.assertEqual(found["mcp chrome-devtools-owner"], "warn")

    def test_opted_in_but_entry_bypasses_the_proxy(self):
        found = self.findings("brave", {"chrome-devtools": self.HEADLESS, "chrome-devtools-headless": self.HEADLESS})
        self.assertEqual(found["mcp chrome-devtools"], "warn")

    def test_opted_in_without_a_fallback(self):
        self.assertEqual(self.findings("brave", {"chrome-devtools": self.PROXY})["mcp chrome-devtools-headless"], "warn")

    def test_switch_off_with_a_leftover_proxy_warns(self):
        self.assertEqual(self.findings("off", {"chrome-devtools": self.PROXY})["mcp chrome-devtools"], "warn")

    def test_switch_off_with_a_leftover_owner_entry_warns(self):
        found = self.findings("off", {"chrome-devtools": self.HEADLESS, "chrome-devtools-owner": self.OWNER})
        self.assertEqual(found["mcp chrome-devtools-owner"], "warn")

    def test_switch_off_on_the_headless_profile_is_ok(self):
        self.assertEqual(self.findings("off", {"chrome-devtools": self.HEADLESS})["mcp chrome-devtools"], "ok")


class BrokerCases(unittest.TestCase):
    """Sessions share one server through the broker, so the owner's browser asks Allow once."""

    def setUp(self):
        import threading
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "fake.py").write_text(FAKE_SERVER, encoding="utf-8")
        self.calls = self.dir / "calls.jsonl"
        self.env = {**os.environ, "SWITCHBOARD_OWNER_BROWSER_STATE": str(self.dir), "SWITCHBOARD_MACHINE": "INVENTED",
                    "SWITCHBOARD_LOCAL_DIR": str(self.dir / "local"), "CLAUDE_CODE_SESSION_ATTENDED": "1"}
        self.env.pop("SWITCHBOARD_WORKSTREAM_SLUG", None)
        self.path = ob.broker_path(self.env)
        self.threading = threading

    def start(self, behavior: str = "") -> "ob.Broker":
        broker = ob.Broker([sys.executable, str(self.dir / "fake.py"), str(self.calls), behavior], self.env, self.path,
                           version="0")
        thread = self.threading.Thread(target=broker.serve, daemon=True)
        thread.start()
        for _ in range(100):
            if self.path.exists():
                break
            import time
            time.sleep(0.05)
        self.addCleanup(lambda: (broker.gone.set(), broker.proc.kill(), thread.join(5)))
        self.thread = thread
        return broker

    def session(self, client: list[dict], lease_holder: str = "me", delay: float = 0.25) -> list[dict]:
        out = io.BytesIO()
        lease = ob.Lease(self.dir / "lease.json", lease_holder, "the invented worker", os.getpid())
        proxy = ob.Proxy(["never-started"], self.env, "C:/invented", BlockingLines(client, delay), out, lease,
                         connect=lambda: ob.connect_broker("0", self.env))
        proxy.run()
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def starts(self) -> int:
        marker = Path(str(self.calls) + ".starts")
        return len(marker.read_text().splitlines()) if marker.exists() else 0

    def test_two_sessions_share_one_server_and_keep_their_own_ids(self):
        self.start()
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        first = self.session([init, ProxyCases.call(1, "new_page", url="https://example.com")])
        second = self.session([init, ProxyCases.call(1, "list_pages")])
        self.assertEqual(self.starts(), 1)
        self.assertEqual(sorted(a["id"] for a in first), [0, 1])
        self.assertEqual(sorted(a["id"] for a in second), [0, 1])
        self.assertNotIn("private.example.invalid", json.dumps(second))
        names = [c[0] for c in ProxyCases.forwarded(self)]
        self.assertEqual(names.count("close_page"), 1)  # the first session closed its own tab on exit
        self.assertTrue(ob.pid_alive(json.loads(self.path.read_text())["pid"]))

    def test_a_wrong_token_gets_nothing(self):
        import socket
        self.start()
        port = json.loads(self.path.read_text())["port"]
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b'{"token": "invented"}\n' + json.dumps(ProxyCases.call(1, "list_pages")).encode() + b"\n")
        sock.settimeout(5)
        self.assertEqual(sock.recv(100), b"")
        sock.close()
        self.assertEqual(ProxyCases.forwarded(self), [])

    def test_a_reconnect_tells_every_other_session(self):
        import time
        self.start("reconnect")
        other = ob._broker_connect(self.path, "0")
        self.addCleanup(other.wait)
        time.sleep(0.3)
        self.session([ProxyCases.call(1, "list_pages")])
        line = next(iter(other.stdout))
        self.assertEqual(json.loads(line)["method"], ob.RECONNECTED_METHOD)

    def test_the_reconnect_notice_drops_held_tab_ids(self):
        proxy = ob.Proxy(["never-started"], self.env, "C:/invented", None, io.BytesIO(),
                         ob.Lease(self.dir / "lease.json", "me", "x", os.getpid()))
        proxy.policy.opened.add(7)
        proxy.handle_child_line(json.dumps({"jsonrpc": "2.0", "method": ob.RECONNECTED_METHOD}).encode())
        self.assertEqual(proxy.policy.opened, set())

    def test_no_broker_falls_back_to_a_session_connection(self):
        def broken():
            raise TimeoutError("invented")
        out = io.BytesIO()
        proxy = ob.Proxy([sys.executable, str(self.dir / "fake.py"), str(self.calls), ""], self.env, "C:/invented",
                         BlockingLines([ProxyCases.call(1, "list_pages")]), out,
                         ob.Lease(self.dir / "lease.json", "me", "x", os.getpid()), connect=broken)
        proxy.run()
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertFalse(answers[0]["result"].get("isError"))

    def test_set_off_asks_the_broker_over_its_socket(self):
        self.start()
        self.assertTrue(ob.stop_broker(self.env))
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.path.exists())

    def test_a_stale_record_is_never_trusted_and_nothing_is_killed(self):
        import socket
        impostor = socket.socket()
        impostor.bind(("127.0.0.1", 0))
        impostor.listen(1)
        self.addCleanup(impostor.close)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"port": impostor.getsockname()[1], "token": "invented", "pid": os.getpid(),
                                         "version": "0", "code": ob.code_stamp()}))
        with mock.patch.object(ob, "end_tree") as ended:
            self.assertIsNone(ob._broker_connect(self.path, "0"))
            self.assertFalse(ob.stop_broker(self.env))
        ended.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_a_new_script_or_version_replaces_the_broker(self):
        self.start()
        record = json.loads(self.path.read_text())
        self.assertIsNone(ob._broker_connect(self.path, "9.9.9"))
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertNotEqual(record.get("version"), "9.9.9")

    def test_another_checkouts_current_broker_is_shared_not_replaced(self):
        other = self.dir / "other_checkout_owner_browser.py"
        other.write_text("# an invented other checkout\n", encoding="utf-8")
        record = {"version": "0", "code": ob.code_stamp(other), "script": str(other)}
        self.assertFalse(ob.broker_stale(record, "0"))
        other.write_text("# that checkout was upgraded\n", encoding="utf-8")
        self.assertTrue(ob.broker_stale(record, "0"))
        self.assertTrue(ob.broker_stale({"version": "0", "code": "invented"}, "0"))  # an older record names no script
        self.assertTrue(ob.broker_stale({**record, "version": "1"}, "0"))
        self.assertFalse(ob.broker_stale({"version": "0", "code": ob.code_stamp()}, "0"))

    def test_a_reconnect_seen_by_a_departed_session_still_reaches_the_rest(self):
        import time
        broker = self.start("reconnect")
        other = ob._broker_connect(self.path, "0")
        self.addCleanup(other.wait)
        time.sleep(0.3)
        with broker.lock:  # an answer whose session already left
            broker.routes[999] = (object(), 1)
        line = json.dumps({"jsonrpc": "2.0", "id": 999,
                           "result": {"content": [{"type": "text", "text": "the browser was restarted or reconnected"}]}})
        with mock.patch.object(broker.proc, "stdout", [line.encode() + b"\n"]):
            broker._read_child()
        self.assertEqual(json.loads(next(iter(other.stdout)))["method"], ob.RECONNECTED_METHOD)

    def test_a_session_survives_a_broker_restart(self):
        import time
        broker = self.start()
        state = {"broker": broker}

        def connect():
            link = ob._broker_connect(self.path, "0")
            if link:
                return link
            state["broker"] = self.start()
            return ob._broker_connect(self.path, "0")

        out = io.BytesIO()
        lease = ob.Lease(self.dir / "lease.json", "me", "the invented worker", os.getpid())
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}

        class Lines(io.RawIOBase):
            def __iter__(self_inner):
                yield (json.dumps(init) + "\n").encode()
                time.sleep(0.5)
                yield (json.dumps(ProxyCases.call(1, "list_pages")) + "\n").encode()
                time.sleep(0.5)
                ob.stop_broker(self.env)  # the broker ends under a live session
                time.sleep(2.0)
                yield (json.dumps(ProxyCases.call(2, "list_pages")) + "\n").encode()
                time.sleep(0.8)

        proxy = ob.Proxy(["never-started"], self.env, "C:/invented", Lines(), out, lease, connect=connect)
        proxy.run()
        answers = {a["id"]: a for a in map(json.loads, out.getvalue().splitlines()) if "id" in a}
        self.assertFalse(answers[2]["result"].get("isError"), answers[2])
        self.assertIsNot(state["broker"], broker)

    def test_a_session_that_stops_reading_does_not_stall_the_others(self):
        broker = self.start()
        broker.SESSION_BACKLOG = 3
        stuck = object()
        import queue as q
        broker.clients[stuck] = q.Queue()
        with mock.patch.object(broker, "_drop", wraps=lambda c: broker.clients.pop(c, None)) as dropped:
            for _ in range(6):
                broker._client_send(stuck, {"jsonrpc": "2.0", "method": "notifications/invented"})
        dropped.assert_called()
        self.assertNotIn(stuck, broker.clients)


if __name__ == "__main__":
    unittest.main()
