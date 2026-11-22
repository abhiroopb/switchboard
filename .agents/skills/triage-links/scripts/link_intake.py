#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[4]
HOME = Path.home()
SKILL_ROOTS = [
    REPO_ROOT / ".agents" / "skills",
    HOME / ".agents" / "skills",
    HOME / ".agents" / "skills-archive",
]
URL_RE = re.compile(r"https?://[^\s)\]>]+")
SLACK_MESSAGE_RE = re.compile(r"/archives/(?P<channel>[A-Z0-9]+)/p(?P<message>\d{16})")


@dataclass
class Capability:
    name: str
    kind: str
    status: str


@dataclass
class NormalizedLink:
    raw_url: str
    system: str
    entity_type: str
    entity_id: str | None
    title_hint: str | None
    read_path: str
    corroboration_sources: list[str]


@dataclass
class InvestigationBundle:
    discovered_urls: list[str]
    evidence_notes: list[str]
    affected_sections: list[str]
    planned_edits: list[str]
    reply_bullets: list[str]
    reply_draft: str | None


def empty_bundle() -> InvestigationBundle:
    return InvestigationBundle(
        discovered_urls=[],
        evidence_notes=[],
        affected_sections=[],
        planned_edits=[],
        reply_bullets=[],
        reply_draft=None,
    )


def dedupe_links(links: list[NormalizedLink]) -> list[NormalizedLink]:
    seen: set[str] = set()
    deduped: list[NormalizedLink] = []
    for link in links:
        if link.raw_url in seen:
            continue
        seen.add(link.raw_url)
        deduped.append(link)
    return deduped


def extract_urls(text: str) -> list[str]:
    seen: list[str] = []
    for match in URL_RE.findall(text):
        url = match.rstrip('.,')
        if url not in seen:
            seen.append(url)
    return seen


def parse_markdown_bundle(text: str) -> InvestigationBundle:
    bundle = empty_bundle()
    section = None
    reply_lines: list[str] = []
    heading_map = {
        "discovered links": "discovered_urls",
        "evidence checked": "evidence_notes",
        "affected sections": "affected_sections",
        "planned edits": "planned_edits",
        "reply bullets": "reply_bullets",
        "draft reply": "reply_draft",
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if section == "reply_draft":
                reply_lines.append("")
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            section = heading_map.get(heading)
            continue
        if section == "reply_draft":
            reply_lines.append(raw_line.rstrip())
            continue
        if not line.startswith("- "):
            continue
        value = line[2:].strip()
        if section == "discovered_urls":
            bundle.discovered_urls.append(value)
        elif section == "evidence_notes":
            bundle.evidence_notes.append(value)
        elif section == "affected_sections":
            bundle.affected_sections.append(value)
        elif section == "planned_edits":
            bundle.planned_edits.append(value)
        elif section == "reply_bullets":
            bundle.reply_bullets.append(value)
    if reply_lines:
        bundle.reply_draft = "\n".join(reply_lines).strip() or None
    return bundle


def load_investigation_bundle(path: str | None) -> InvestigationBundle:
    if not path:
        return empty_bundle()
    bundle_path = Path(path).expanduser()
    text = bundle_path.read_text()
    if bundle_path.suffix.lower() == ".json":
        payload = json.loads(text)
        return InvestigationBundle(
            discovered_urls=list(payload.get("discovered_urls", []) or []),
            evidence_notes=list(payload.get("evidence_notes", []) or []),
            affected_sections=list(payload.get("affected_sections", []) or []),
            planned_edits=list(payload.get("planned_edits", []) or []),
            reply_bullets=list(payload.get("reply_bullets", []) or []),
            reply_draft=payload.get("reply_draft"),
        )
    return parse_markdown_bundle(text)


def discover_capabilities() -> dict[str, Capability]:
    names = [
        "slack",
        "gdrive",
        "linear",
        "roadmap-tracker",
        "notion",
        "glean",
        "agent-browser",
    ]
    capabilities: dict[str, Capability] = {
        "read_web_page": Capability("read_web_page", "builtin-tool", "active"),
        "read_thread": Capability("read_thread", "builtin-tool", "active"),
        "gmail": Capability("gmail", "builtin-tool", "active"),
        "jira-mcp": Capability("jira-mcp", "builtin-tool", "active"),
        "linear-mcp": Capability("linear-mcp", "builtin-tool", "active"),
    }
    for name in names:
        status = "missing"
        for root in SKILL_ROOTS:
            if (root / name / "SKILL.md").exists():
                if root.name == "skills-archive":
                    status = "archived"
                elif root == REPO_ROOT / ".agents" / "skills":
                    status = "local"
                else:
                    status = "active"
                break
        capabilities[name] = Capability(name, "skill", status)

    for command in ["agent-browser", "pmos", "gh", "python3"]:
        if shutil.which(command):
            capabilities[command] = Capability(command, "executable", "active")
    return capabilities


def normalize_url(url: str) -> NormalizedLink:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if "slack.com" in host:
        match = SLACK_MESSAGE_RE.search(path)
        if match:
            entity_id = f"{match.group('channel')}:{match.group('message')}"
            return NormalizedLink(url, "slack", "thread-message", entity_id, None, "skill:slack", ["local-context", "gdrive", "linear", "jira", "tracker", "glean"])
        return NormalizedLink(url, "slack", "channel-or-thread", path or url, None, "skill:slack", ["local-context", "gdrive", "linear", "jira"])

    if "docs.google.com" in host:
        if "/document/" in path:
            return NormalizedLink(url, "gdrive", "google-doc", path, None, "skill:gdrive", ["local-context", "slack", "linear", "jira"])
        if "/spreadsheets/" in path:
            return NormalizedLink(url, "gdrive", "google-sheet", path, None, "skill:gdrive", ["local-context", "slack", "linear"])
        if "/presentation/" in path:
            return NormalizedLink(url, "gdrive", "google-slide", path, None, "skill:gdrive", ["local-context", "slack"])

    if "drive.google.com" in host:
        return NormalizedLink(url, "gdrive", "drive-file", path, None, "skill:gdrive", ["local-context", "slack"])

    if "linear.app" in host:
        if "/issue/" in path:
            return NormalizedLink(url, "linear", "linear-issue", path, None, "skill:linear", ["local-context", "slack", "gdrive", "jira"])
        if "/project/" in path:
            return NormalizedLink(url, "linear", "linear-project", path, None, "skill:linear", ["local-context", "slack", "gdrive", "jira", "tracker"])
        return NormalizedLink(url, "linear", "linear-object", path, None, "skill:linear", ["local-context", "slack"])

    if host == "github.com":
        if "/pull/" in path:
            return NormalizedLink(url, "github", "github-pr", path, None, "tool:github", ["local-context", "slack", "linear", "jira"])
        if "/issues/" in path:
            return NormalizedLink(url, "github", "github-issue", path, None, "tool:github", ["local-context", "slack", "linear", "jira"])
        return NormalizedLink(url, "github", "github-object", path, None, "tool:github", ["local-context", "slack"])

    if "atlassian.net" in host:
        if "/browse/" in path:
            return NormalizedLink(url, "jira", "jira-issue", path, None, "tool:jira", ["local-context", "slack", "gdrive", "linear", "tracker"])
        if "/wiki/" in path:
            return NormalizedLink(url, "confluence", "confluence-page", path, None, "tool:confluence", ["local-context", "slack", "jira"])

    if "notion.so" in host:
        return NormalizedLink(url, "notion", "notion-page", path, None, "skill:notion", ["local-context", "slack", "linear", "jira"])

    # Optional roadmap-tracker adapter. Replace the example host and path with
    # your organization's roadmap or project registry, or leave them unmatched.
    if "tracker.example.com" in host and "/records/" in path:
        return NormalizedLink(url, "tracker", "tracker-record", path, None, "tool:tracker", ["local-context", "slack", "linear", "jira", "gdrive"])

    # Optional intranet short link resolver. Replace the example host with
    # your organization's short link host, or leave it unmatched.
    if "go.example.com" in host:
        return NormalizedLink(url, "shortlink", "shortlink", path, None, "tool:shortlink", ["local-context"])

    return NormalizedLink(url, "web", "web-page", path or url, host, "tool:web", ["local-context", "slack", "gdrive", "glean"])


def infer_task_mode(text: str) -> str:
    lowered = text.lower()
    artifact_terms = [
        "1 pager",
        "one pager",
        "one-pager",
        "prd",
        "project list",
        "decision doc",
        "planning doc",
        "summary doc",
        "writeup",
    ]
    drafting_terms = ["draft", "write", "create", "produce", "prepare", "generate"]
    if any(term in lowered for term in artifact_terms) and any(term in lowered for term in drafting_terms):
        return "draft-artifact"
    if any(term in lowered for term in ["validate", "accurate", "check this", "review the content", "is this right", "edit", "update"]):
        return "validate"
    if any(term in lowered for term in ["reply", "respond", "comment back", "post back"]):
        return "reply"
    if any(term in lowered for term in ["status", "summary", "recap", "what's going on"]):
        return "status"
    if any(term in lowered for term in ["file", "create", "log this", "add to backlog", "make a ticket"]):
        return "file-followup"
    if any(term in lowered for term in ["what should we do", "recommend", "think about", "how should we handle"]):
        return "decision-support"
    return "triage"


def select_primary_link(links: list[NormalizedLink]) -> NormalizedLink | None:
    if not links:
        return None
    priority = {
        "slack": 0,
        "gdrive": 1,
        "linear": 2,
        "jira": 3,
        "tracker": 4,
        "github": 5,
        "notion": 6,
        "web": 7,
        "shortlink": 8,
        "confluence": 9,
    }
    return sorted(links, key=lambda link: priority.get(link.system, 99))[0]


def select_execution_target(
    primary: NormalizedLink | None,
    discovered_links: list[NormalizedLink],
    task_mode: str,
) -> NormalizedLink | None:
    if primary is None and not discovered_links:
        return None
    if task_mode != "validate":
        return primary or select_primary_link(discovered_links)

    priority = {
        "web": 0,
        "gdrive": 1,
        "notion": 2,
        "confluence": 3,
        "tracker": 4,
        "linear": 5,
        "jira": 6,
        "github": 7,
        "slack": 8,
        "shortlink": 9,
    }
    candidates = discovered_links or ([primary] if primary else [])
    return sorted(candidates, key=lambda link: priority.get(link.system, 99))[0]


def select_execution_surface(target: NormalizedLink | None, task_mode: str, capabilities: dict[str, Capability]) -> dict[str, str]:
    if target is None:
        return {"best_surface": "none", "best_tool_path": "none", "execution_mode": "prepare"}

    if task_mode == "draft-artifact":
        return {"best_surface": "workstream-draft", "best_tool_path": "local-workstream", "execution_mode": "prepare"}

    # Start with a read/API path. A discovered skill or executable does not
    # establish an authenticated browser session. If this read is insufficient,
    # the caller follows the sanctioned Chrome DevTools fallback policy.
    if target.system == "web":
        return {"best_surface": "web-review", "best_tool_path": "read_web_page", "execution_mode": "prepare"}
    if target.system == "gdrive":
        return {"best_surface": "google-drive", "best_tool_path": "gdrive", "execution_mode": "prepare"}
    if target.system == "slack":
        return {"best_surface": "slack-thread", "best_tool_path": "slack", "execution_mode": "prepare"}
    if target.system == "linear":
        return {"best_surface": "linear", "best_tool_path": "linear", "execution_mode": "prepare"}
    if target.system == "tracker":
        return {"best_surface": "tracker", "best_tool_path": "roadmap-tracker", "execution_mode": "prepare"}
    if target.system == "notion":
        return {"best_surface": "notion", "best_tool_path": "notion", "execution_mode": "prepare"}
    return {"best_surface": "analysis", "best_tool_path": target.read_path, "execution_mode": "prepare"}


def build_actions(
    primary: NormalizedLink | None,
    execution_target: NormalizedLink | None,
    task_mode: str,
    execution_surface: dict[str, str],
) -> list[dict[str, str]]:
    if execution_target is None:
        return []
    actions: list[dict[str, str]] = []
    if task_mode == "validate":
        actions.append({
            "id": "A1",
            "type": "prepare-edit",
            "target": execution_target.raw_url,
            "surface": execution_surface["best_surface"],
            "approval_command": "approve A1",
        })
        if primary is not None and primary.system == "slack":
            actions.append({
                "id": "A2",
                "type": "post-reply",
                "target": primary.raw_url,
                "surface": "slack-thread",
                "approval_command": "approve A2",
            })
    elif task_mode == "reply":
        actions.append({
            "id": "A1",
            "type": "post-reply",
            "target": execution_target.raw_url,
            "surface": execution_surface["best_surface"],
            "approval_command": "approve A1",
        })
    elif task_mode == "file-followup":
        actions.append({
            "id": "A1",
            "type": "create-followup",
            "target": execution_target.raw_url,
            "surface": execution_surface["best_surface"],
            "approval_command": "approve A1",
        })
    elif task_mode == "draft-artifact":
        actions.append({
            "id": "A1",
            "type": "prepare-draft",
            "target": execution_target.raw_url,
            "surface": execution_surface["best_surface"],
            "approval_command": "approve A1",
        })
    else:
        actions.append({
            "id": "A1",
            "type": "execute-plan",
            "target": execution_target.raw_url,
            "surface": execution_surface["best_surface"],
            "approval_command": "approve A1",
        })
    return actions


def summarize_what_this_is(primary: NormalizedLink | None, execution_target: NormalizedLink | None, task_mode: str) -> str:
    if primary is None:
        return "Dropped artifact needs investigation."
    if task_mode == "draft-artifact" and primary.system == "slack":
        return "Slack artifact is the request wrapper; the real working target is the requested draft artifact."
    if execution_target is not None and execution_target.raw_url != primary.raw_url:
        return f"{primary.system} artifact is the context anchor, and the real working target is the linked {execution_target.system} artifact for `{task_mode}` work."
    return f"{primary.system} artifact is the main working target for `{task_mode}` follow-through."


def summarize_real_ask(primary: NormalizedLink | None, execution_target: NormalizedLink | None, task_mode: str) -> str:
    target = execution_target or primary
    if target is None:
        return "Determine the next action."
    if task_mode == "validate":
        return f"Validate the {target.system} target, prepare exact corrections, and queue any needed follow-up reply."
    if task_mode == "reply":
        return f"Prepare the response for the {target.system} target."
    if task_mode == "file-followup":
        return f"Prepare the follow-up artifact that should be created from the {target.system} target."
    if task_mode == "draft-artifact":
        return "Prepare the requested artifact first; treat the source message as context, not the execution surface."
    if task_mode == "status":
        return f"Synthesize the current status around the {target.system} target."
    return f"Prepare the next-step brief for the {target.system} target."


def summarize_recommended_action(execution_target: NormalizedLink | None, execution_surface: dict[str, str], task_mode: str) -> str:
    if execution_target is None:
        return "Investigate further before choosing an action."
    if task_mode == "validate":
        return f"Prepare exact edits for the {execution_target.system} target in `{execution_surface['best_surface']}`, then queue the related response or follow-up."
    if task_mode == "reply":
        return f"Draft the reply for the {execution_target.system} target and stop for approval before posting."
    if task_mode == "draft-artifact":
        return "Draft the requested artifact in the workstream first; only prepare a Slack reply after the draft exists or a blocking source is missing."
    return f"Use `{execution_surface['best_surface']}` as the working surface and stop at the approval boundary before any write."


def build_execution_plan(actions: list[dict[str, str]], affected_sections: list[str]) -> list[str]:
    plan_steps: list[str] = []
    if affected_sections:
        plan_steps.append(f"Review the affected sections: {', '.join(affected_sections)}.")
    for action in actions:
        if action["type"] == "prepare-edit":
            plan_steps.append(f"Prepare the exact edit in `{action['surface']}` for `{action['target']}`.")
        elif action["type"] == "post-reply":
            plan_steps.append(f"Prepare the reply for `{action['target']}` and hold it for approval.")
        elif action["type"] == "create-followup":
            plan_steps.append(f"Prepare the follow-up creation payload for `{action['target']}`.")
        elif action["type"] == "prepare-draft":
            plan_steps.append("Prepare the requested artifact in the workstream before drafting any Slack acknowledgement.")
        else:
            plan_steps.append(f"Prepare the execution plan for `{action['target']}` in `{action['surface']}`.")
    return plan_steps


def build_reply_draft(
    primary: NormalizedLink | None,
    task_mode: str,
    reply_draft: str | None,
    reply_bullets: list[str],
) -> str | None:
    if reply_draft:
        return reply_draft
    if primary is None or primary.system != "slack":
        return None
    if task_mode not in {"validate", "reply"}:
        return None
    if reply_bullets:
        bullet_lines = "\n".join(f"- {bullet}" for bullet in reply_bullets)
        return f"Planned thread reply:\n\n{bullet_lines}"
    return "Planned thread reply:\n\n- Confirm what was checked\n- Summarize the main correction\n- Note that the edit is prepared and waiting for approval"


def build_plan(
    text: str,
    investigation_bundle: InvestigationBundle | None = None,
    discovered_urls: list[str] | None = None,
    evidence_notes: list[str] | None = None,
    affected_sections: list[str] | None = None,
    planned_edits: list[str] | None = None,
    reply_bullets: list[str] | None = None,
    reply_draft: str | None = None,
) -> dict:
    bundle = investigation_bundle or empty_bundle()
    capabilities = discover_capabilities()
    links = dedupe_links([normalize_url(url) for url in extract_urls(text)])
    merged_discovered_urls = list(bundle.discovered_urls) + list(discovered_urls or [])
    discovered_links = dedupe_links([normalize_url(url) for url in merged_discovered_urls])
    primary = select_primary_link(links)
    task_mode = infer_task_mode(text)
    execution_target = select_execution_target(primary, discovered_links, task_mode)
    execution_surface = select_execution_surface(execution_target, task_mode, capabilities)
    actions = build_actions(primary, execution_target, task_mode, execution_surface)
    evidence_notes = list(bundle.evidence_notes) + list(evidence_notes or [])
    affected_sections = list(bundle.affected_sections) + list(affected_sections or [])
    planned_edits = list(bundle.planned_edits) + list(planned_edits or [])
    reply_bullets = list(bundle.reply_bullets) + list(reply_bullets or [])
    effective_reply_draft = reply_draft or bundle.reply_draft
    brief = {
        "what_this_is": summarize_what_this_is(primary, execution_target, task_mode),
        "real_ask": summarize_real_ask(primary, execution_target, task_mode),
        "recommended_action": summarize_recommended_action(execution_target, execution_surface, task_mode),
        "evidence_checked": evidence_notes,
        "affected_sections": affected_sections,
        "execution_plan": build_execution_plan(actions, affected_sections),
        "planned_edits": planned_edits,
        "reply_draft": build_reply_draft(primary, task_mode, effective_reply_draft, reply_bullets),
    }
    return {
        "raw_input": text,
        "task_mode": task_mode,
        "urls": [asdict(link) for link in links],
        "discovered_urls": [asdict(link) for link in discovered_links],
        "primary": asdict(primary) if primary else None,
        "execution_target": asdict(execution_target) if execution_target else None,
        "corroboration_budget": 3 if task_mode in {"validate", "status", "decision-support"} else 1,
        "capabilities": {name: asdict(capability) for name, capability in sorted(capabilities.items())},
        "execution_surface": execution_surface,
        "actions": actions,
        "brief": brief,
        "approval_commands": [action["approval_command"] for action in actions] + (["approve all"] if actions else []),
    }


def render_markdown(plan: dict) -> str:
    brief = plan["brief"]
    lines = [
        "# Dropped link intake plan",
        "",
        f"- Task mode: `{plan['task_mode']}`",
        f"- Corroboration budget: `{plan['corroboration_budget']}`",
    ]
    primary = plan.get("primary")
    if primary:
        lines.extend(
            [
                f"- Primary artifact: `{primary['system']}` / `{primary['entity_type']}`",
                f"- Primary read path: `{primary['read_path']}`",
                f"- Execution target: `{plan['execution_target']['system']}` / `{plan['execution_target']['entity_type']}`",
                f"- Best surface: `{plan['execution_surface']['best_surface']}`",
                f"- Best tool path: `{plan['execution_surface']['best_tool_path']}`",
                "",
                "## Corroboration sources",
            ]
        )
        lines.extend(f"- `{source}`" for source in primary["corroboration_sources"])
    if plan["discovered_urls"]:
        lines.extend(["", "## Discovered links"])
        for link in plan["discovered_urls"]:
            lines.append(f"- `{link['system']}` `{link['raw_url']}`")
    lines.extend(["", "## What this is", brief["what_this_is"], "", "## Real ask", brief["real_ask"], "", "## Recommended action", brief["recommended_action"]])
    if brief["evidence_checked"]:
        lines.extend(["", "## Evidence checked"])
        lines.extend(f"- {note}" for note in brief["evidence_checked"])
    if brief["affected_sections"]:
        lines.extend(["", "## Affected sections"])
        lines.extend(f"- {section}" for section in brief["affected_sections"])
    if brief["execution_plan"]:
        lines.extend(["", "## Execution plan"])
        lines.extend(f"- {step}" for step in brief["execution_plan"])
    if brief["planned_edits"]:
        lines.extend(["", "## Planned edits"])
        lines.extend(f"- {edit}" for edit in brief["planned_edits"])
    if brief["reply_draft"]:
        lines.extend(["", "## Draft reply", brief["reply_draft"]])
    if plan["actions"]:
        lines.extend(["", "## Planned actions"])
        for action in plan["actions"]:
            lines.append(f"- `{action['id']}` `{action['type']}` on `{action['surface']}` for `{action['target']}`")
        lines.extend(["", "## Approval commands"])
        lines.extend(f"- `{command}`" for command in plan["approval_commands"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize dropped links into an execution-ready investigation plan.")
    parser.add_argument("--input", required=True, help="Raw user input containing URLs and context.")
    parser.add_argument("--investigation-file", help="JSON or markdown bundle captured during the first-pass investigation.")
    parser.add_argument("--discovered-link", action="append", default=[], help="Link discovered after reading the primary artifact.")
    parser.add_argument("--evidence-note", action="append", default=[], help="Evidence note gathered during investigation.")
    parser.add_argument("--affected-section", action="append", default=[], help="Affected section in the target artifact.")
    parser.add_argument("--planned-edit", action="append", default=[], help="Exact planned edit for the target artifact.")
    parser.add_argument("--reply-bullet", action="append", default=[], help="Bullet to include in the draft reply.")
    parser.add_argument("--reply-draft", help="Full draft reply text.")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    args = parser.parse_args()

    bundle = load_investigation_bundle(args.investigation_file)
    plan = build_plan(
        args.input,
        investigation_bundle=bundle,
        discovered_urls=args.discovered_link,
        evidence_notes=args.evidence_note,
        affected_sections=args.affected_section,
        planned_edits=args.planned_edit,
        reply_bullets=args.reply_bullet,
        reply_draft=args.reply_draft,
    )
    if args.format == "markdown":
        print(render_markdown(plan), end="")
    else:
        print(json.dumps(plan, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
