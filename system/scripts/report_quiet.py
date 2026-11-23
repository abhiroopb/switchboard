#!/usr/bin/env python3
"""Quiet-report predicates for report-to-cos.sh.

    report_quiet.py locality-ack "<summary text>"     exit 0 when the text is
                                                      only a locality acknowledgment

A locality acknowledgment is the reply an owner tab gives to the start-of-day
seed ("Understood. Staying local on this machine, waiting for the workstream
instructions."). The workspace-report plugin mirrors it like any human-looking
turn, so it becomes a done report with no deliverable. Such a report is logged
and settled but never reaches CoS, directly or through the next-up Updates
section, the same shape as dispatch.quiet_done_workstreams.

The predicate is deliberately narrow: short body, no URL other than the Amp
thread link the mirror appends, an acknowledgment opener, a locality phrase,
and a waiting-for-instructions phrase. A real deliverable that happens to run
"on this machine" keeps its delivery.
"""

from __future__ import annotations

import re
import sys

MAX_BODY_CHARS = 400

_THREAD_LINK = re.compile(r"\s*⏎?\s*\[Open link\]\(https://ampcode\.com/threads/T-[0-9a-fA-F-]+\)\s*$")
_BARE_THREAD = re.compile(r"https://ampcode\.com/threads/T-[0-9a-fA-F-]+")
_CLAUDE_POINTER = re.compile(r"(?:·\s*)?(?:claude --resume|Claude session) [0-9a-fA-F-]{36}")
_URL = re.compile(r"https?://\S+")
_REPORT_FRAME = re.compile(r"^\[[^\]]*\]\s*(?:(?:done|blocked|failed|deferred|cancelled):\s*)?", re.I)
# Three cues, all required: an acknowledgment opener (including the older
# "Read the live switchboard registry entry …" / "Registry entry switchboard loaded" seed
# replies), a locality phrase, and a no-deliverable phrase (waiting for
# instructions, standing by, no files changed, or the "no Orb, remote VM, SSH"
# recital). Requiring all three keeps ordinary reports from matching.
_OPENER = re.compile(
    r"\b(understood|acknowledged|ready|standing by|got it|confirmed|ok|okay|roger|loaded|read the live switchboard registry)\b", re.I)
_LOCAL = re.compile(r"on this (?:machine|mac)\b|staying local|running locally|stay(?:ing)? local", re.I)
_WAITING = re.compile(
    r"\b(?:waiting|wait|ready|standing by)\b[^.;]{0,60}\binstructions\b|\bstanding by\b"
    r"|\bno (?:files|changes) (?:changed|made)\b|\bno orbs?\b", re.I)


def strip_report_frame(text: str) -> str:
    """Drop the transport frame: `[id slug] status:`, the `• ` bullet, and the
    Amp thread link or Claude session pointer the workspace-report mirror appends. The `<emoji> <Title> —`
    prefix stays; it is short and the length cap covers it."""
    body = (text or "").strip()
    body = _REPORT_FRAME.sub("", body, count=1)
    body = re.sub(r"^•\s*", "", body)
    body = _THREAD_LINK.sub("", body)
    body = _BARE_THREAD.sub("", body)
    body = _CLAUDE_POINTER.sub("", body)
    return body.strip()


def is_locality_ack(text: str) -> bool:
    body = strip_report_frame(text)
    if not body or len(body) > MAX_BODY_CHARS:
        return False
    if _URL.search(body):
        return False   # anything with a link is a deliverable, not an acknowledgment
    if not _OPENER.search(body):
        return False
    return bool(_LOCAL.search(body) and _WAITING.search(body))


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "locality-ack":
        return 0 if is_locality_ack(" ".join(argv[1:])) else 1
    sys.stderr.write(__doc__.strip().splitlines()[0] + "\n\n    report_quiet.py locality-ack \"<text>\"\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
