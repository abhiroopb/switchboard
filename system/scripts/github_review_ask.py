"""Is this GitHub PR an explicit review ask for the owner? One answer for every surface.

A PR goes on the owner's review list only when the owner is explicitly asked to
review it.

A PR is a review ask only when the owner is explicitly requested on the live PR:
the owner's login is in `reviewRequests`, or a team listed in
`priority-channels.yaml github_pr_reviews.review_teams` is. Everything else
(author, assignee, team fan-out such as `acme/desktop-app`,
subscribed, mentioned in passing, a `review_requested` notification reason
on its own) is a status line, never a review or respond ask. Unverifiable
fails closed: not an ask.

The live read is `reply_check.check_ref` (cached 45 min, backoff-aware), so
the collectors, the to-do capture, verify-asks and the start-day plan share
one verdict per PR instead of each calling `gh`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reply_check  # noqa: E402

REVIEW_ROLE_ASK = reply_check.REVIEW_ROLE_ASK
review_framed = reply_check.review_framed
review_ask_status = reply_check.review_ask_status

# Notification reasons a collector may keep as "review" only after classify_pr says ask.
REASON_FANOUT = "team_review_requested"
REASON_UNVERIFIED = "review_requested_unverified"

Checker = Callable[[str], dict[str, Any]]


def classify_pr(ref: str, *, checker: Checker | None = None) -> dict[str, Any]:
    """{role, review_ask, verified, verdict, why} for a GitHub PR/issue URL.

    role        requested-reviewer | team-fanout | author | none | "" (unverified)
    review_ask  True only for requested-reviewer; False otherwise (fail closed)
    verified    the live PR (or its in-TTL cache entry) carried reviewRequests
    """
    ref = str(ref or "").strip()
    if reply_check.source_kind({"ref": ref}) != "github":
        return {"role": "", "review_ask": False, "verified": False, "verdict": "", "why": "not a GitHub PR/issue URL"}
    try:
        verdict = (checker or reply_check.check_ref)(ref)
    except Exception as exc:  # a classifier bug must never break a collector
        return {"role": "", "review_ask": False, "verified": False, "verdict": "unknown", "why": f"{type(exc).__name__}: {exc}"[:200]}
    evidence = verdict.get("evidence") or {}
    role = str(evidence.get("owner_role") or "")
    if role not in reply_check.REVIEW_ROLES:
        return {
            "role": "",
            "review_ask": False,
            "verified": False,
            "verdict": str(verdict.get("verdict") or "unknown"),
            "why": str(evidence.get("note") or evidence.get("why") or "live PR state not readable; fail closed"),
        }
    via = str(evidence.get("review_ask_via") or "")
    why = {
        REVIEW_ROLE_ASK: f"the owner is an explicitly requested reviewer ({via})",
        "team-fanout": "review requested from a team the owner is on (fan-out, not an ask)",
        "author": "the owner's own PR",
        "none": "the owner is not a requested reviewer",
    }[role]
    return {
        "role": role,
        "review_ask": role == REVIEW_ROLE_ASK,
        "verified": True,
        "verdict": str(verdict.get("verdict") or ""),
        "why": why,
    }


def demote_reason(reason: str, classified: dict[str, Any]) -> str:
    """The notification reason a collector should store for ``reason``.

    `review_requested` survives only when the PR is an explicit ask; otherwise
    it becomes `team_review_requested` (verified fan-out / author / none) or
    `review_requested_unverified` (gh unreadable). Other reasons pass through.
    """
    if str(reason or "").strip().lower() != "review_requested":
        return reason
    if classified.get("review_ask"):
        return "review_requested"
    return REASON_FANOUT if classified.get("verified") else REASON_UNVERIFIED
