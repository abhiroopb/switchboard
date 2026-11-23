#!/usr/bin/env python3
"""Build a recent cross-surface activity digest for a workstream.

Reads append-only jsonl sources under system/state/sources/ and writes a
markdown digest to workstreams/<name>/.signals/recent.md so the agent that
opens the workspace can see what changed while it was idle.

Sources scanned (when present):
    owner-channel-signals.jsonl   - priority Slack channel posts
    owner-mentions.jsonl          - @mentions and DMs
    owner-linear-inbox.jsonl      - Linear notifications to the owner
    owner-linear-authored.jsonl   - Linear comments the owner authored
    owner-github-inbox.jsonl      - GitHub review/mention notifications
    owner-jira-issues.jsonl       - Jira issues the owner touched
    owner-gmail-inbox.jsonl       - Inbound mail
    owner-gmail-sent.jsonl        - Outbound mail
    owner-drive-activity.jsonl    - Drive files the owner modified
    owner-notion-activity.jsonl   - Notion pages touched
    owner-tracker-updates.jsonl - Tracker status updates the owner wrote

Matching strategy (loose by design — false positives are fine, missed signals
are not):
    1. Channel signals/mentions: route via priority-channels.yaml
       default_workstream when the channel id matches.
    2. Everything else: keyword match against the workstream slug, name, and
       optional `keywords:` list in workstreams/<name>/config.yaml.

Usage:
    python3 build-workspace-signals.py <workstream-slug> [--days N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from _local_tz import LOCAL_TZ as LOCAL_TZ
from single_instance import LockBusyError, single_instance

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML required; pip install --user pyyaml\n")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_DIR = REPO_ROOT / "system" / "state" / "sources"
WORKSTREAMS_DIR = REPO_ROOT / "workstreams"
PRIORITY_CHANNELS = REPO_ROOT / "system" / "config" / "priority-channels.yaml"
LOG_FILE = REPO_ROOT / "system" / "logs" / "workspace-signals.log"

DEFAULT_DAYS = 3


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG_FILE.open("a") as f:
        f.write(f"[{ts}] {msg}\n")


def load_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_iso(raw: str | None, naive_tz: Any = None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip().replace("Z", "+00:00")
    # Try ISO 8601
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=naive_tz or timezone.utc)
        return dt
    except ValueError:
        pass
    # Try "YYYY-MM-DD HH:MM"
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt) + 4 if "%Y" in fmt else len(s)], fmt).replace(tzinfo=naive_tz or timezone.utc)
        except ValueError:
            continue
    return None


def best_timestamp(rec: dict[str, Any]) -> datetime | None:
    # Slack collectors emit `received_at` as naive PT-local (e.g. "2026-01-15 12:10").
    # Everything else is TZ-aware ISO. Apply per-key naive default so display
    # in LOCAL_TZ comes out right.
    naive_tz_for: dict[str, Any] = {"received_at": LOCAL_TZ}
    for key in ("received_at_iso", "received_at", "updated_at", "modified_time", "created_at", "captured_at"):
        v = rec.get(key)
        if not v:
            continue
        dt = parse_iso(v, naive_tz=naive_tz_for.get(key))
        if dt:
            return dt
    return None


def load_channel_routing() -> dict[str, str]:
    """Return {channel_id: default_workstream} from priority-channels.yaml."""
    out: dict[str, str] = {}
    if not PRIORITY_CHANNELS.exists():
        return out
    cfg = yaml.safe_load(PRIORITY_CHANNELS.read_text()) or {}
    for tier in ("tier_1", "tier_2"):
        for ch in cfg.get(tier, []) or []:
            cid = ch.get("id")
            dws = ch.get("default_workstream")
            if cid and dws:
                out[cid] = dws
    return out


def load_workstream_config(slug: str) -> dict[str, Any]:
    cfg_path = WORKSTREAMS_DIR / slug / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        return yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def collect_word_overlap_counts(this_slug: str) -> dict[str, int]:
    """Count how many OTHER workstream slugs contain each word.
    Used to drop overly-generic split-words (e.g. 'profiles' appears in 7
    workstream slugs and would cause cross-pollination of digests), while
    keeping mostly-unique split-words (e.g. 'onboarding' appears in 1)."""
    counts: dict[str, int] = {}
    if not WORKSTREAMS_DIR.exists():
        return counts
    for entry in WORKSTREAMS_DIR.iterdir():
        if not entry.is_dir() or entry.name.startswith(".") or entry.name == this_slug:
            continue
        if entry.name in {"_archive", "shared"}:
            continue
        for w in entry.name.split("-"):
            if len(w) >= 4:
                counts[w.lower()] = counts.get(w.lower(), 0) + 1
    return counts


# Auto-keywords drop a slug-word if more than this many OTHER workstreams use
# it. Higher = more permissive (more cross-pollination), lower = stricter.
MAX_WORD_OVERLAP = 2


def slug_to_keywords(slug: str, cfg: dict[str, Any]) -> list[str]:
    """Build a keyword list to filter non-channel sources.

    Strategy:
      - Always include the full slug and slug-with-spaces variants.
      - Always include the workstream `name:` from config.
      - Always include explicit `keywords:` from config.
      - Include split-words from the slug only when ≤ MAX_WORD_OVERLAP other
        workstreams use them (drops 'profiles' which appears in 7 slugs, but
        keeps 'onboarding' which only collides with one other workstream).
    """
    parts = [slug, slug.replace("-", " ")]
    overlap = collect_word_overlap_counts(slug)
    for w in slug.split("-"):
        if len(w) >= 4 and overlap.get(w.lower(), 0) <= MAX_WORD_OVERLAP:
            parts.append(w)
    name = cfg.get("name")
    if isinstance(name, str):
        parts.append(name.lower())
    extra = cfg.get("keywords")
    if isinstance(extra, list):
        parts.extend(str(x).lower() for x in extra)
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        p = p.lower().strip()
        if p and p not in seen and len(p) >= 3:
            seen.add(p)
            out.append(p)
    return out


def text_match(haystack: str, needles: list[str]) -> bool:
    h = haystack.lower()
    return any(n in h for n in needles)


def fmt_ts(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def truncate(text: str, n: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def build_section(
    title: str,
    rows: list[tuple[datetime, str]],
    limit: int,
) -> str:
    if not rows:
        return ""
    rows.sort(key=lambda r: r[0], reverse=True)
    out = [f"## {title}", ""]
    for _, line in rows[:limit]:
        out.append(line)
    out.append("")
    return "\n".join(out)


def canonical_knowledge_section(slug: str, days: int) -> str:
    """Embed the reconciled canonical-knowledge view for this workstream.

    The raw sections below show what *arrived*; this block shows what is
    *currently true* after dedup + supersession + freshness resolution, so
    the agent opening the workspace doesn't have to remember to query it.
    Fail-soft: returns "" on any error so signals never block on knowledge.
    """
    script = REPO_ROOT / "system" / "scripts" / "query-knowledge.py"
    if not script.exists():
        return ""
    cmd = [
        sys.executable, str(script),
        "--workstream", slug,
        "--days", str(days),
        "--limit", "12",
        "--include-historical",
    ]
    knowledge_dir = os.environ.get("SWITCHBOARD_KNOWLEDGE_DIR")
    if knowledge_dir:
        cmd += ["--knowledge-dir", knowledge_dir]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        return ""
    body = (out.stdout or "").strip()
    if out.returncode != 0 or not body:
        return ""
    return (
        "## 🧠 Canonical knowledge (reconciled — authoritative for current status)\n\n"
        + body + "\n"
    )


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            tmp = Path(handle.name)
            handle.write(text)
            handle.flush()
        tmp.replace(path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _run(args: argparse.Namespace) -> int:
    slug = args.workstream
    ws_dir = WORKSTREAMS_DIR / slug
    if not ws_dir.exists():
        log(f"workstream not found: {slug}")
        return 1

    cfg = load_workstream_config(slug)
    keywords = slug_to_keywords(slug, cfg)
    routing = load_channel_routing()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    # --- Slack channel signals (route by channel_id default_workstream) ---
    channel_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-channel-signals.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        cid = rec.get("channel_id") or ""
        dws = rec.get("default_workstream") or routing.get(cid)
        body = rec.get("body") or ""
        if dws != slug and not text_match(body, keywords):
            continue
        ch = rec.get("channel") or cid
        who = rec.get("from") or "?"
        link = rec.get("source_ref") or ""
        channel_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **#{ch}** · {who}: {truncate(body, 200)} [↗]({link})")
        )

    # --- Mentions / DMs ---
    mention_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-mentions.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        cid = rec.get("channel_id") or ""
        body = rec.get("body") or ""
        dws = routing.get(cid)
        if dws != slug and not text_match(body, keywords):
            continue
        ch = rec.get("channel") or ("DM" if rec.get("is_dm") else cid)
        who = rec.get("from") or "?"
        link = rec.get("source_ref") or ""
        mention_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **{ch}** · {who}: {truncate(body, 200)} [↗]({link})")
        )

    # --- First-party scheduled meeting briefs ---
    meeting_rows: list[tuple[datetime, str]] = []
    meetings_path = SOURCES_DIR / "meetings.json"
    if meetings_path.exists():
        try:
            meeting_items = (json.loads(meetings_path.read_text()) or {}).get("items", [])
        except (json.JSONDecodeError, OSError):
            meeting_items = []
        for rec in meeting_items:
            if not isinstance(rec, dict) or rec.get("collector") != "pre-brief":
                continue
            if rec.get("workstream") != slug:
                continue
            dt = best_timestamp(rec)
            if not dt or dt < cutoff:
                continue
            meeting = rec.get("meeting") or "meeting"
            prep = " ".join(rec.get("prep") or rec.get("active_signals") or [])
            meeting_rows.append(
                (
                    dt,
                    f"- `{fmt_ts(dt)}` **Meeting prep (as written {dt.astimezone(LOCAL_TZ).date()})** · "
                    f"{truncate(meeting, 100)}: {truncate(prep, 180)}",
                )
            )

    # --- Linear inbox + authored ---
    linear_rows: list[tuple[datetime, str]] = []
    for fname, label in (
        ("owner-linear-inbox.jsonl", "in"),
        ("owner-linear-authored.jsonl", "out"),
    ):
        for rec in load_jsonl(SOURCES_DIR / fname):
            dt = best_timestamp(rec)
            if not dt or dt < cutoff:
                continue
            blob = " ".join(
                str(rec.get(k) or "")
                for k in ("subject", "body", "linear_identifier", "issue_url", "source_ref")
            )
            if not text_match(blob, keywords):
                continue
            subj = rec.get("subject") or rec.get("linear_identifier") or "linear"
            link = rec.get("source_ref") or rec.get("issue_url") or ""
            preview = truncate(rec.get("body") or "", 160)
            linear_rows.append(
                (dt, f"- `{fmt_ts(dt)}` **linear/{label}** · {truncate(subj, 80)}: {preview} [↗]({link})")
            )

    # --- GitHub inbox ---
    github_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-github-inbox.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        blob = " ".join(
            str(rec.get(k) or "")
            for k in ("title", "subject", "repo", "url", "reason")
        )
        if not text_match(blob, keywords):
            continue
        title = rec.get("title") or rec.get("subject") or "github"
        link = rec.get("url") or rec.get("source_ref") or ""
        reason = rec.get("reason") or ""
        github_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **github/{reason}** · {truncate(title, 160)} [↗]({link})")
        )

    # --- Jira ---
    jira_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-jira-issues.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        blob = " ".join(
            str(rec.get(k) or "")
            for k in ("summary", "key", "url", "project")
        )
        if not text_match(blob, keywords):
            continue
        key = rec.get("key") or "jira"
        summary = rec.get("summary") or ""
        link = rec.get("url") or ""
        jira_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **{key}** · {truncate(summary, 160)} [↗]({link})")
        )

    # --- Gmail (in + out) ---
    gmail_rows: list[tuple[datetime, str]] = []
    for fname, label in (
        ("owner-gmail-inbox.jsonl", "in"),
        ("owner-gmail-sent.jsonl", "out"),
    ):
        for rec in load_jsonl(SOURCES_DIR / fname):
            dt = best_timestamp(rec)
            if not dt or dt < cutoff:
                continue
            blob = " ".join(
                str(rec.get(k) or "")
                for k in ("subject", "snippet", "body", "from", "to")
            )
            if not text_match(blob, keywords):
                continue
            subj = rec.get("subject") or "(no subject)"
            who = rec.get("from") if label == "in" else rec.get("to") or "?"
            link = rec.get("permalink") or rec.get("source_ref") or ""
            gmail_rows.append(
                (dt, f"- `{fmt_ts(dt)}` **gmail/{label}** · {who}: {truncate(subj, 160)} [↗]({link})")
            )

    # --- Drive activity ---
    drive_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-drive-activity.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        name = rec.get("file_name") or ""
        if not text_match(name, keywords):
            continue
        link = rec.get("web_view_link") or ""
        drive_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **drive** · {truncate(name, 160)} [↗]({link})")
        )

    # --- Notion ---
    notion_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-notion-activity.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        blob = " ".join(str(rec.get(k) or "") for k in ("title", "url"))
        if not text_match(blob, keywords):
            continue
        title = rec.get("title") or "notion"
        link = rec.get("url") or ""
        notion_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **notion** · {truncate(title, 160)} [↗]({link})")
        )

    # --- GitHub authored ---
    gh_authored_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-github-authored.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        blob = " ".join(str(rec.get(k) or "") for k in ("title", "repo", "labels", "source_ref"))
        if not text_match(blob, keywords):
            continue
        title = rec.get("title") or ""
        link = rec.get("source_ref") or ""
        kind = rec.get("kind") or "pr"
        repo = rec.get("repo") or ""
        state = rec.get("state") or ""
        gh_authored_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **{kind}/{state}** · {repo}#{rec.get('number')} {truncate(title, 140)} [↗]({link})")
        )

    # --- Doc comments ---
    doc_comment_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-doc-comments.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        blob = " ".join(str(rec.get(k) or "") for k in ("file_name", "anchor_text", "body"))
        if not text_match(blob, keywords):
            continue
        author = rec.get("author") or "?"
        if rec.get("authored_by_me"):
            author = f"{author} (you)"
        kind = rec.get("kind") or "comment"
        body = truncate(rec.get("body") or "", 180)
        link = rec.get("source_ref") or rec.get("file_link") or ""
        fname = truncate(rec.get("file_name") or "", 60)
        doc_comment_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **doc/{kind}** · {author} on _{fname}_: {body} [↗]({link})")
        )

    # --- Calendar authored ---
    cal_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-calendar-authored.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        blob = " ".join(str(rec.get(k) or "") for k in ("summary", "attendee_emails"))
        if not text_match(blob, keywords):
            continue
        link = rec.get("source_ref") or ""
        cal_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **scheduled** · {truncate(rec.get('summary') or '', 120)} ({rec.get('attendee_count')} ppl) [↗]({link})")
        )

    # --- Tracker ---
    tracker_rows: list[tuple[datetime, str]] = []
    for rec in load_jsonl(SOURCES_DIR / "owner-tracker-updates.jsonl"):
        dt = best_timestamp(rec)
        if not dt or dt < cutoff:
            continue
        blob = " ".join(str(rec.get(k) or "") for k in ("project_name", "body", "summary"))
        if not text_match(blob, keywords):
            continue
        proj = rec.get("project_name") or "tracker"
        link = rec.get("url") or ""
        body = truncate(rec.get("body") or rec.get("summary") or "", 200)
        tracker_rows.append(
            (dt, f"- `{fmt_ts(dt)}` **tracker** · {proj}: {body} [↗]({link})")
        )

    # --- Compose digest ---
    generated_at = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
    sections = []
    sections.append(
        f"# Recent signals — {slug}\n\n"
        f"_Generated {generated_at} · window = last {args.days}d · keywords: "
        f"{', '.join('`'+k+'`' for k in keywords[:8])}_\n\n"
        "> Timestamps are when each message/edit was captured, NOT when the facts\n"
        "> inside it happened. Prose here may describe old events; do not present a\n"
        "> dated fact as a current update without checking its event time against\n"
        "> the canonical knowledge layer (`system/scripts/query-knowledge.py`,\n"
        "> contract: `system/config/temporal-contract.md`).\n"
        ">\n"
        "> **Thread-resolution gate:** before drafting a reply or creating a todo\n"
        "> from any Slack item below, run\n"
        "> `python3 system/scripts/check-thread-resolution.py --ref \"<permalink>\" --json`\n"
        "> and act only on `open` verdicts. Resolved threads (the owner replied/reacted,\n"
        "> or a teammate answer closed it) get recorded as resolved with evidence,\n"
        "> never drafted.\n"
    )

    canonical_md = canonical_knowledge_section(slug, args.days)
    if canonical_md:
        sections.append(canonical_md)

    sections.append(build_section("📅 Meeting prep", meeting_rows, 8))
    sections.append(build_section("📬 Mentions / DMs", mention_rows, 12))
    sections.append(build_section("💬 Channels", channel_rows, 15))
    sections.append(build_section("📨 Email", gmail_rows, 8))
    sections.append(build_section("📐 Linear", linear_rows, 12))
    sections.append(build_section("🐙 GitHub (inbox)", github_rows, 8))
    sections.append(build_section("🐙 GitHub (authored)", gh_authored_rows, 8))
    sections.append(build_section("🧱 Jira", jira_rows, 8))
    sections.append(build_section("📂 Drive (files)", drive_rows, 8))
    sections.append(build_section("📝 Doc comments", doc_comment_rows, 12))
    sections.append(build_section("📅 Calendar (organized)", cal_rows, 6))
    sections.append(build_section("📓 Notion", notion_rows, 6))
    sections.append(build_section("📊 Tracker", tracker_rows, 6))

    body = "\n".join(s for s in sections if s.strip())

    total = sum(len(rows) for rows in (
        meeting_rows, channel_rows, mention_rows, linear_rows, github_rows,
        gh_authored_rows, jira_rows, gmail_rows, drive_rows, doc_comment_rows,
        cal_rows, notion_rows, tracker_rows
    ))
    if total == 0:
        body += "\n_No matching activity in window._\n"

    out_dir = ws_dir / ".signals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "recent.md"
    atomic_write_text(out_file, body)

    log(f"workstream={slug} signals={total} -> {out_file}")
    print(f"build-workspace-signals: {slug} → {total} signals → {out_file}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workstream")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = ap.parse_args(argv)
    try:
        with single_instance("build-workspace-signals"):
            return _run(args)
    except LockBusyError as exc:
        print(f"build-workspace-signals: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
