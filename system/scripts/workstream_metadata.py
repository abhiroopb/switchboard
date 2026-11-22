#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


STATUS_LABELS = {
    "active": "Active",
    "paused": "Paused",
    "waiting": "Waiting",
    "blocked": "Blocked",
    "needs follow-up": "Needs Follow-Up",
    "needs action": "Needs Action",
    "needs review": "Needs Review",
    "new": "New",
    "reference": "Reference",
    "done": "Done",
    "resolved": "Resolved",
    "cancelled": "Cancelled",
    "archived": "Archived",
}

TERMINAL_STATUSES = {"done", "resolved", "cancelled", "archived"}
PRESENTATION_FORMATS = {"scan-card", "control-panel", "brief-appendix"}
GENERIC_MATCH_TERMS = {
    "active",
    "check",
    "doc",
    "docs",
    "follow",
    # "mcp" is org-generic (Example MCP, example-dwh-mcp, Canva MCP, Figma MCP); as a
    # slug bit it paired with "example" (inside every "acme/" repo) and sent
    # unrelated PRs to example-northwind.
    "mcp",
    "online",
    "ordering",
    "profile",
    "profiles",
    "project",
    "prototype",
    "review",
    "customer",
    "customers",
    "strategy",
    "update",
    "workstream",
}


def titleize_slug(slug: str) -> str:
    special = {
        "ai": "AI",
        "ios": "iOS",
        "pr": "PR",
    }
    parts = slug.replace("_", "-").split("-")
    return " ".join(special.get(part.lower(), part.capitalize()) for part in parts)


def normalize_status(raw: str | None) -> str:
    if not raw:
        return "active"

    value = raw.strip().lower()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"\s+", " ", value)

    if value in {"complete", "completed", "closed"} or value.startswith("closed ("):
        return "done"
    if value.startswith("closed /"):
        return "archived"

    aliases = {
        "in progress": "active",
        "in flight": "active",
        "rolling out": "active",
        "ready": "active",
        "discovery": "active",
        "discovery escalating": "active",
        "waiting on": "waiting",
        "waiting on legal rollout date": "waiting",
        "blocked airtable fields": "blocked",
        "needs follow up": "needs follow-up",
        "needs followup": "needs follow-up",
        "review": "needs review",
        "needs review": "needs review",
        "needs action": "needs action",
        "reference": "reference",
        "new": "new",
        "paused": "paused",
        "done": "done",
        "resolved": "resolved",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "archived": "archived",
    }

    if value in aliases:
        return aliases[value]
    if "waiting" in value:
        return "waiting"
    if "blocked" in value:
        return "blocked"
    if "follow" in value:
        return "needs follow-up"
    if "review" in value:
        return "needs review"
    if "action" in value:
        return "needs action"
    if "pause" in value:
        return "paused"
    if "reference" in value:
        return "reference"
    if "done" in value:
        return "done"
    if "resolve" in value:
        return "resolved"
    if "cancel" in value:
        return "cancelled"
    return "active"


def format_status_label(status: str, paused_until: str | None = None) -> str:
    if status == "paused" and paused_until:
        try:
            dt = datetime.fromisoformat(paused_until)
            if dt.date() > datetime.now().date():
                return f"Paused until {dt.strftime('%b')} {dt.day}"
        except ValueError:
            pass
    return STATUS_LABELS.get(status, titleize_slug(status))


def normalize_presentation_format(raw: Any) -> str:
    value = str(raw or "scan-card").strip().lower()
    value = value.replace("_", "-").replace(" ", "-")
    if value not in PRESENTATION_FORMATS:
        return "scan-card"
    return value


def normalize_watched_docs(raw: Any) -> list[dict[str, str]]:
    """Return watched Google files with stable IDs and display metadata."""
    if not isinstance(raw, list):
        return []
    docs: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in raw:
        item = value if isinstance(value, dict) else {"url": value}
        url = str(item.get("url") or "").strip()
        match = re.search(r"/d/([^/?#]+)", url)
        if not match or match.group(1) in seen:
            continue
        file_id = match.group(1)
        seen.add(file_id)
        docs.append(
            {
                "file_id": file_id,
                "url": url,
                "label": str(item.get("label") or "").strip(),
            }
        )
    return docs


def first_sentence(text: str | None) -> str:
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""
    match = re.match(r"(.+?[.!?])(\s|$)", compact)
    if match:
        return match.group(1).strip()
    return compact


# What new-workstream.py scaffolds into config.yaml and CONTEXT.md before anyone has written a
# real step. The CoS queue must not rank these as work (for example a top card reading
# "Complete and verify the initial request" left from a smoke test).
SCAFFOLD_NEXT_STEP = re.compile(
    r"^(?:\d+\.\s*)?(?:complete and verify the initial request"
    r"|record decisions, dependencies, and follow-through here"
    r"|owns durable follow-through for this originating request)\b",
    re.IGNORECASE,
)
NO_OPEN_STEP = "Review CONTEXT.md and decide the next concrete move"


def is_scaffold_next_step(text: Any) -> bool:
    """True for scaffold text that names no real work (see SCAFFOLD_NEXT_STEP)."""
    return bool(SCAFFOLD_NEXT_STEP.match(str(text or "").strip()))


def parse_context(context_path: Path) -> dict[str, Any]:
    text = context_path.read_text() if context_path.exists() else ""
    parsed: dict[str, Any] = {
        "status": None,
        "last_worked_on": None,
        "next_step": None,
        "context_title": None,
        "context_text": text,
    }

    title_match = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
    if title_match:
        parsed["context_title"] = title_match.group(1).strip()

    status_match = re.search(r"^##\s+Status:\s*(.+)$", text, flags=re.MULTILINE)
    if status_match:
        parsed["status"] = status_match.group(1).strip()

    last_worked_match = re.search(r"^\*\*Last worked:\*\*\s*(.+)$", text, flags=re.MULTILINE)
    if last_worked_match:
        parsed["last_worked_on"] = last_worked_match.group(1).strip()

    for heading in (
        "## Open / Next Steps",
        "## Next Steps",
        "## Open Questions",
        "## Re-check",
    ):
        section_match = re.search(
            rf"^{re.escape(heading)}\n+(.*?)(?=^##\s|\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        if not section_match:
            continue
        section = section_match.group(1)
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("[x]") or stripped.startswith("- [x]"):
                continue
            if "~~" in stripped or stripped.startswith("✅"):
                continue
            stripped = re.sub(r"^[-*]\s+", "", stripped)
            stripped = re.sub(r"^\d+\.\s+", "", stripped)
            stripped = re.sub(r"^\[ \]\s*", "", stripped)
            stripped = re.sub(r"^✅\s*", "", stripped)
            stripped = stripped.strip()
            if "~~" in stripped:
                continue
            if not stripped or is_scaffold_next_step(stripped):
                continue
            # Plan items open with a bold lead; a queue card title is plain text.
            parsed["next_step"] = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped)
            return parsed

    return parsed


def parse_date(value: str | None, fallback_timestamp: float) -> tuple[str | None, str]:
    if value:
        cleaned = value.strip()
        for fmt in ("%Y-%m-%d", "%b %d", "%b %-d"):
            try:
                if fmt == "%Y-%m-%d":
                    dt = datetime.strptime(cleaned, fmt)
                    return dt.strftime("%Y-%m-%d"), f"{dt.strftime('%b')} {dt.day}"
            except ValueError:
                continue
        iso_match = re.match(r"(\d{4}-\d{2}-\d{2})", cleaned)
        if iso_match:
            dt = datetime.strptime(iso_match.group(1), "%Y-%m-%d")
            return iso_match.group(1), f"{dt.strftime('%b')} {dt.day}"
        month_day_match = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})", cleaned)
        if month_day_match:
            display = f"{month_day_match.group(1)} {int(month_day_match.group(2))}"
            return None, display

    fallback_dt = datetime.fromtimestamp(fallback_timestamp)
    return fallback_dt.strftime("%Y-%m-%d"), f"{fallback_dt.strftime('%b')} {fallback_dt.day}"


YAML_ERROR_KEY = "yaml_error"
_FLAT_KEY = re.compile(r"^([A-Za-z_][\w-]*):(?:\s+(.*))?$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def recover_flat_yaml(text: str) -> dict[str, Any]:
    """Best-effort read of a config whose YAML does not parse.

    Only top-level `key: scalar`, `key: |`/`>` blocks, and simple `- item`
    lists survive; anything nested is dropped. Free text is taken verbatim,
    so an unquoted `next_step: ... non-urgent: watch ...` keeps its meaning
    instead of killing the whole Megadesk rebuild.
    """
    data: dict[str, Any] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        match = _FLAT_KEY.match(line)
        if not match:
            continue
        key, value = match.group(1), (match.group(2) or "").strip()
        if value in {"|", ">", "|-", ">-"}:
            block: list[str] = []
            while index < len(lines) and (not lines[index].strip() or lines[index].startswith((" ", "\t"))):
                block.append(lines[index].strip())
                index += 1
            joined = "\n" if value.startswith("|") else " "
            data[key] = joined.join(part for part in block if part or value.startswith("|")).strip()
        elif not value:
            items: list[str] = []
            while index < len(lines) and lines[index].startswith((" ", "\t")):
                item = lines[index].strip()
                index += 1
                if item.startswith("- "):
                    items.append(_unquote(item[2:]))
            if items:
                data[key] = items
        elif value.startswith("#"):
            continue
        else:
            lowered = value.lower()
            if lowered in {"true", "false"}:
                data[key] = lowered == "true"
            elif lowered in {"null", "~"}:
                data[key] = None
            elif re.fullmatch(r"-?\d+", value):
                data[key] = int(value)
            else:
                data[key] = _unquote(value)
    return data


def load_yaml(path: Path) -> dict[str, Any]:
    """Parse a config; a malformed file degrades to a flat read plus `yaml_error`.

    One hand-edited `workstreams/*/config.yaml` with an unquoted `: ` inside
    `next_step` raises ScannerError and must not abort
    rebuild-megadesk-state.py for every workstream. Readers warn and keep
    going; `lint-workstream-metadata.py` surfaces the file to fix.
    """
    if not path.exists():
        return {}
    text = path.read_text()
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as error:
            mark = getattr(error, "problem_mark", None)
            where = f" line {mark.line + 1} column {mark.column + 1}" if mark else ""
            problem = getattr(error, "problem", None) or str(error).splitlines()[0]
            message = f"{path}:{where} {problem}; quote free text that contains ': '"
            print(f"[workstream-metadata] WARNING: invalid YAML, using flat fallback: {message}", file=sys.stderr)
            data = recover_flat_yaml(text)
            data[YAML_ERROR_KEY] = message
            return data
        return data or {}

    result = subprocess.run(
        [
            "ruby",
            "-ryaml",
            "-rjson",
            "-e",
            "print JSON.generate(YAML.safe_load(STDIN.read, aliases: true))",
        ],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        if result.returncode != 0:
            message = f"{path}: {(result.stderr or 'ruby YAML parse failed').strip().splitlines()[0]}"
            print(f"[workstream-metadata] WARNING: invalid YAML, using flat fallback: {message}", file=sys.stderr)
            return {**recover_flat_yaml(text), YAML_ERROR_KEY: message}
        return {}

    data = json.loads(result.stdout)
    return data or {}


def load_workstream_metadata(repo_root: Path) -> list[dict[str, Any]]:
    workstreams_root = repo_root / "workstreams"
    items: list[dict[str, Any]] = []

    for directory in sorted(workstreams_root.iterdir()):
        if not directory.is_dir() or directory.name == "shared":
            continue
        if directory.name == "_archive":
            continue

        context_path = directory / "CONTEXT.md"
        if not context_path.exists():
            continue

        config_path = directory / "config.yaml"
        config = load_yaml(config_path)
        context = parse_context(context_path)

        config_status = normalize_status(config.get("status")) if config.get("status") else None
        context_status = normalize_status(context.get("status")) if context.get("status") else None
        terminal_status = next(
            (candidate for candidate in (config_status, context_status) if candidate in TERMINAL_STATUSES),
            None,
        )
        status = terminal_status or config_status or context_status or "active"
        last_worked_on, last_worked_display = parse_date(
            str(config.get("last_worked_on") or context.get("last_worked_on") or "").strip() or None,
            context_path.stat().st_mtime,
        )
        real_step = next(
            (
                step
                for step in (
                    config.get("next_step"),
                    context.get("next_step"),
                    first_sentence(config.get("startup_instruction")),
                    first_sentence(config.get("description")),
                )
                if step and not is_scaffold_next_step(step)
            ),
            None,
        )

        estimate_min = config.get("estimate_min")
        if estimate_min is None:
            if status in {"waiting", "blocked", "needs follow-up"}:
                estimate_min = 10
            elif str(config.get("priority", "medium")).lower() == "high":
                estimate_min = 25
            else:
                estimate_min = 20

        items.append(
            {
                "slug": directory.name,
                "display_name": config.get("name") or context.get("context_title") or titleize_slug(directory.name),
                "status": status,
                "status_label": format_status_label(status, config.get("paused_until")),
                "priority": str(config.get("priority", "medium")).lower(),
                "queue_score_bonus": int(config.get("queue_score_bonus", 0)),
                "todo_match_terms": [str(term).strip().lower() for term in config.get("todo_match_terms", []) if str(term).strip()],
                "signal_match_terms": [str(term).strip().lower() for term in config.get("signal_match_terms", []) if str(term).strip()],
                "watched_docs": normalize_watched_docs(config.get("watched_docs")),
                "description": config.get("description") or "",
                "startup_instruction": config.get("startup_instruction") or "",
                "presentation_format": normalize_presentation_format(config.get("presentation_format")),
                "next_step": real_step or NO_OPEN_STEP,
                "has_open_step": real_step is not None,
                "estimate_min": int(estimate_min),
                "last_worked_on": last_worked_on,
                "last_worked_display": last_worked_display,
                "stale_after_days": int(config.get("stale_after_days", 14)),
                "paused_until": config.get("paused_until"),
                "auto_open": bool(config.get("auto_open", False)),
                # Ongoing/operational workstreams (registries, maintenance, knowledge bases)
                # have no finish line; the board keeps them out of the kanban.
                "ongoing": bool(config.get("ongoing", False)),
                "config_path": str(config_path.relative_to(repo_root)) if config_path.exists() else None,
                "context_path": str(context_path.relative_to(repo_root)),
                "mtime": context_path.stat().st_mtime,
            }
        )

    return items


def archived_workstream_names(repo_root: Path) -> list[str]:
    archive_root = repo_root / "workstreams" / "_archive"
    if not archive_root.exists():
        return []
    return sorted(path.name for path in archive_root.iterdir() if path.is_dir())
