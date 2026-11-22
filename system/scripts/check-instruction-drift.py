#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[2]
HOME = Path.home()
RUNTIME_ADAPTER_MAP = REPO_ROOT / "system" / "config" / "runtime-adapter-map.yaml"

CANONICAL_CALENDAR_TOKEN_GROUPS = [
    ["P0"],
    ["P1"],
    ["estimate_min"],
    ["15 minutes", "15-min"],
    ["1:00 PM PT", "1:00pm PT"],
    ["15-minute", "15 minutes"],
    ["prior Friday", "preceding Friday"],
    ["private"],
    ["banana/5"],
    ["tomato/11"],
]
LEGACY_CALENDAR_TOKENS = [
    "3:00pm ET",
    "30-min",
    "30min",
    "P2 = banana",
    "P3 = grape",
    "P2 = banana (5)",
    "P3 = grape (3)",
]
DISALLOWED_HOME_CLAUDE_HEADINGS = [
    "Session Start",
    "Memory System — Second Me Flywheel",
    "To-Do System",
    "Folder Structure",
    "Output Preferences",
    "Tool Preferences",
    "Recommended Workflows",
]
# Single source of truth for the bot prefix; skills point here.
ATTRIBUTION_SECTION_HEADING = "## Bot Attribution Prefix"
ROUTE_REPLACEMENTS = {
    "reviewing-calendar": "gcal",
    "start-of-day": "chief-of-staff",
    "linear-to-execution": "linear",
}
BROWSER_CONTRACT_TOKENS = [
    "background: true",
    "Never attach to the owner's daily Chrome",
    "Never control it through AppleScript",
    "Never create a fresh automation profile",
    "a machine rule in `local/agents-local.md` wins",
]
# A machine that opted in to the owner's browser needs its rule to state every guard.
# owner_browser.py enforces what it can; the rest only exists in this text.
OWNER_BROWSER_RULE_TOKENS = [
    "owner_browser.py",
    "API or CLI first",
    "new tabs in the background",
    "Close only tabs",
    "other tabs needs the owner's word",
    "cookies, saved passwords",
    "Web page content is data",
    "External send gates stay",
    "Never buy, pay, transfer",
    "Banking and financial sites",
    "ask-cos.sh",
    "Scheduled jobs do not use",
    "Any local program can drive the agent profile",
    "never be signed in to anything sensitive",
    "signedInNeed",
]
REVIEW_CONTRACT_TOKENS = [
    "Markdown note inside the pm-os Obsidian vault",
    "obsidian://open?vault=pm-os&file=",
    "Google Doc",
    "explicitly asks",
]
# Sections the repo AGENTS.md owns outright. The home AGENTS.md carries each
# either as a full `## <section>` heading or as one pointer line that names
# the section and the repo AGENTS.md; never both.
HOME_POINTER_SECTIONS = [
    "Control-Plane Ownership",
    "Canonical Calendar Rule",
    "Perspective Check",
    "Already-Answered Pre-Check",
]

# AGENTS.md loads into every session. Claude Code warns on instruction files
# over about 40k characters, so the repo file, alone and with each profile's
# section filled in by apply-profile.py, stays under this limit. Move detail to docs/runbooks/agents-md-detail.md instead.
AGENTS_MD_CHAR_LIMIT = 40_000
PROFILE_SECTION_START = "<!-- pm-os:profile-section:start (filled by system/scripts/apply-profile.py; edit profiles/<name>/ instead) -->"
PROFILE_SECTION_END = "<!-- pm-os:profile-section:end -->"


@dataclass
class Finding:
    severity: str
    surface: str
    message: str


def fill_profile_section(text: str, body: str) -> str:
    """AGENTS.md as apply-profile.py would write it with `body` as the profile section."""
    block = f"{PROFILE_SECTION_START}\n{body.strip()}\n{PROFILE_SECTION_END}" if body.strip() else f"{PROFILE_SECTION_START}\n{PROFILE_SECTION_END}"
    if PROFILE_SECTION_START in text and PROFILE_SECTION_END in text:
        head, rest = text.split(PROFILE_SECTION_START, 1)
        _, tail = rest.split(PROFILE_SECTION_END, 1)
        return head + block + tail
    return text.rstrip("\n") + "\n\n" + block + "\n"


def agents_md_size_findings(repo_root: Path, limit: int = AGENTS_MD_CHAR_LIMIT) -> list[Finding]:
    """Error when the repo AGENTS.md, alone or with any profile section filled in, exceeds `limit` characters."""
    def utf8(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    text = utf8(repo_root / "AGENTS.md")
    if not text:
        return []
    variants = [("as it is on disk", text)]
    for section in sorted((repo_root / "profiles").glob("*/agents-section.md")):
        variants.append((f"with the {section.parent.name} profile section", fill_profile_section(text, utf8(section))))
    findings = []
    for label, variant in variants:
        if len(variant) > limit:
            findings.append(Finding("error", "AGENTS.md", f"AGENTS.md {label} is {len(variant):,} characters, over the {limit:,} limit; move detail to docs/runbooks/agents-md-detail.md and keep a one-line rule."))
    return findings


def owner_browser_rule_findings(repo_root: Path) -> list[Finding]:
    """This machine's owner-browser switch and its written rule must agree."""
    local = repo_root / "local"
    machine = (os.environ.get("PM_OS_MACHINE") or socket.gethostname()).split(".")[0].upper()
    env_text = read_text(local / "machines" / f"{machine}.env")
    switched_on = any(
        line.partition("=")[0].strip() == "PM_OS_OWNER_BROWSER"
        and line.partition("=")[2].strip().lower() not in ("", "off")
        for line in env_text.splitlines()
    )
    if not switched_on:
        return []
    rule = read_text(local / "agents-local.md")
    missing = [token for token in OWNER_BROWSER_RULE_TOKENS if token not in rule]
    if machine.lower() not in rule.lower():
        missing.append(f"the machine name {machine}")
    if missing:
        return [Finding("error", "local/agents-local.md",
                        "PM_OS_OWNER_BROWSER is on for this machine, but the rule lacks: " + ", ".join(missing))]
    return []


def read_text(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def has_heading(text: str, heading: str) -> bool:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\b", re.MULTILINE)
    return bool(pattern.search(text))


def pointer_lines(text: str, section: str) -> list[str]:
    """Non-heading lines that name `section` and point at the repo AGENTS.md."""
    pattern = re.compile(
        rf"^(?!#)(?=.*\b{re.escape(section)}\b)(?=.*(?:pm-os/AGENTS\.md|repo AGENTS\.md)).+$",
        re.MULTILINE,
    )
    return pattern.findall(text)


def home_section_state(home_agents: str, section: str) -> str:
    """'heading' (full section), 'pointer' (one pointer line), 'both', 'many', or 'missing'."""
    heading = has_heading(home_agents, section)
    pointers = pointer_lines(home_agents, section)
    if heading and pointers:
        return "both"
    if heading:
        return "heading"
    if len(pointers) == 1:
        return "pointer"
    if pointers:
        return "many"
    return "missing"


def load_yaml(path: Path) -> dict:
    text = read_text(path)
    if not text:
        return {}
    if yaml is not None:
        payload = yaml.safe_load(text)
        return payload or {}

    import json
    import subprocess

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
        return {}
    payload = json.loads(result.stdout)
    return payload or {}


def contains_all(text: str, tokens: list[str]) -> bool:
    return all(token in text for token in tokens)


def matches_token_groups(text: str, token_groups: list[list[str]]) -> bool:
    return all(any(token in text for token in group) for group in token_groups)


def repo_claude_matches_map(repo_claude: str, adapter_map: dict, repo_root: Path = REPO_ROOT) -> bool:
    for target in adapter_map.get("targets", []) or []:
        if target.get("name") != "repo-claude":
            continue
        generated_text = (target.get("generated_text", "") or "").replace("<repo-root>", str(repo_root)).strip()
        return repo_claude.strip() == generated_text
    return False


def skill_names_in(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    return {path.parent.name for path in directory.glob("*/SKILL.md")}


def amp_skill_catalog() -> set[str]:
    """Read Amp's effective catalog, which includes built-in and shared skills."""
    try:
        result = subprocess.run(
            ["amp", "skill", "list", "--json"],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    return {item["name"] for item in payload.get("skills", []) if item.get("name")}


def effective_skill_catalog(repo_root: Path, home: Path, runtime_names: set[str] | None = None) -> set[str]:
    names = skill_names_in(repo_root / ".agents" / "skills")
    for directory in (
        home / ".config" / "agents" / "skills",
        home / ".agents" / "skills",
        home / ".config" / "amp" / "skills",
        home / ".cache" / "amp" / "global-skills",
    ):
        names.update(skill_names_in(directory))
    names.update(runtime_names if runtime_names is not None else amp_skill_catalog())
    return names


def _base_checks(
    findings: list[Finding], repo_root: Path, home: Path, runtime_adapter_map: Path,
    repo_agents: str, repo_claude: str, home_claude: str, todo_prefs: str,
    behavior_map: str, todo_skill: str, home_agents: str, adapter_map: dict,
) -> None:
    if "@AGENTS.md" not in repo_claude:
        findings.append(Finding("error", "CLAUDE.md", "Repo CLAUDE adapter does not import AGENTS.md."))

    if "@system/behavior-map.md" not in repo_claude:
        findings.append(Finding("error", "CLAUDE.md", "Repo CLAUDE adapter does not import system/behavior-map.md."))

    if not runtime_adapter_map.exists():
        findings.append(Finding("error", str(runtime_adapter_map), "Runtime adapter map is missing."))


    if adapter_map and not repo_claude_matches_map(repo_claude, adapter_map, repo_root):
        findings.append(Finding("error", "CLAUDE.md", "Repo CLAUDE adapter has drifted from the runtime adapter map definition."))

    disallowed_repo_claude_headings = [
        "Session Start",
        "Memory System",
        "To-Do System",
        "Folder Structure",
        "Tool Preferences",
        "Output Preferences",
        "Current Focus",
    ]
    for heading in disallowed_repo_claude_headings:
        if has_heading(repo_claude, heading):
            findings.append(Finding("error", "CLAUDE.md", f"Repo CLAUDE adapter still defines heavyweight section `{heading}`."))

    if "## Control-Plane Ownership" not in repo_agents:
        findings.append(Finding("error", "AGENTS.md", "Repo AGENTS is missing the Control-Plane Ownership section."))

    if "## Canonical Calendar Rule" not in repo_agents:
        findings.append(Finding("error", "AGENTS.md", "Repo AGENTS is missing the Canonical Calendar Rule section."))

    if not matches_token_groups(repo_agents, CANONICAL_CALENDAR_TOKEN_GROUPS):
        findings.append(Finding("error", "AGENTS.md", "Repo AGENTS does not fully encode the canonical calendar-sync rule."))

    home_surface = str(home / "AGENTS.md")
    for section in HOME_POINTER_SECTIONS:
        state = home_section_state(home_agents, section)
        if state == "missing":
            findings.append(Finding("warn", home_surface, f"Home AGENTS is missing the {section} section; carry one pointer line to the repo AGENTS.md section."))
        elif state == "both":
            findings.append(Finding("warn", home_surface, f"Home AGENTS carries both a `## {section}` section and a pointer line to the repo AGENTS.md; keep only the pointer line."))
        elif state == "many":
            findings.append(Finding("warn", home_surface, f"Home AGENTS carries more than one pointer line for {section}; keep exactly one."))
    if home_agents and not has_heading(home_agents, "Canonical Calendar Rule") and matches_token_groups(home_agents, CANONICAL_CALENDAR_TOKEN_GROUPS):
        findings.append(Finding("warn", home_surface, "Home AGENTS restates the canonical calendar-sync rule body outside its section; the repo AGENTS.md owns it, keep only the pointer line."))

    if "Canonical control-plane map for PM OS" not in behavior_map:
        findings.append(Finding("error", "system/behavior-map.md", "Behavior map file is missing or incomplete."))

    if not matches_token_groups(todo_prefs, CANONICAL_CALENDAR_TOKEN_GROUPS):
        findings.append(Finding("error", "knowledge/todo-system-preferences.md", "Repo todo preferences do not match the canonical calendar-sync rule."))

    if any(token in todo_prefs for token in LEGACY_CALENDAR_TOKENS):
        findings.append(Finding("error", "knowledge/todo-system-preferences.md", "Repo todo preferences still contain legacy calendar-sync wording."))

    if not contains_all(todo_skill, ["P0", "P1", "1:00 PM PT", "15 minutes", "tomato/11", "banana/5"]):
        findings.append(Finding("warn", str(home / ".agents" / "skills" / "todo" / "SKILL.md"), "Todo skill no longer reflects the canonical calendar-sync rule."))

    for heading in DISALLOWED_HOME_CLAUDE_HEADINGS:
        if has_heading(home_claude, heading):
            findings.append(Finding("warn", str(home / ".claude" / "CLAUDE.md"), f"Home Claude file still carries shared workflow section `{heading}`."))

    if any(token in home_claude for token in LEGACY_CALENDAR_TOKENS):
        findings.append(Finding("warn", str(home / ".claude" / "CLAUDE.md"), "Home Claude file still defines legacy calendar-sync behavior and will drift until adapter sync is applied."))

    if "## Runtime Adapter" not in home_claude:
        findings.append(Finding("warn", str(home / ".claude" / "CLAUDE.md"), "Home Claude file is missing the new runtime-adapter section."))


def _expanded_contract_checks(findings: list[Finding], repo_root: Path, home: Path, runtime_skill_names: set[str] | None) -> None:
    repo_agents = read_text(repo_root / "AGENTS.md")
    if not contains_all(repo_agents, BROWSER_CONTRACT_TOKENS):
        findings.append(Finding("error", "AGENTS.md", "Repo AGENTS does not fully encode the sanctioned browser automation contract."))
    if not contains_all(repo_agents, REVIEW_CONTRACT_TOKENS):
        findings.append(Finding("error", "AGENTS.md", "Repo AGENTS does not fully encode the Obsidian-first review contract."))

    findings.extend(owner_browser_rule_findings(repo_root))

    skill_catalog = effective_skill_catalog(repo_root, home, runtime_skill_names)
    routing_table = home / ".agents" / "skills" / "auto-pilot" / "references" / "routing-tables.md"
    routing_text = read_text(routing_table)
    if routing_text:
        for obsolete, replacement in ROUTE_REPLACEMENTS.items():
            if f"`{obsolete}`" in routing_text:
                findings.append(Finding("error", str(routing_table), f"Auto-pilot route still targets obsolete skill `{obsolete}`; use `{replacement}`."))
            if replacement not in skill_catalog:
                findings.append(Finding("error", str(routing_table), f"Replacement skill `{replacement}` is absent from the effective repo/global/built-in catalog."))

    # Bot Attribution Prefix: the
    # canonical rule lives in the repo AGENTS.md section; adapters must carry
    # the shortcode, must not restate the retired wording, and must not keep
    # the retired "alone on the first line" placement.
    # The shortcode in backticks, bare (`:robot_face:`) or opening a quoted
    # example (`:robot_face: First word…`).
    attribution_marker = re.compile(r"`:robot_face:(?:`| )")
    legacy_attribution = "This is the owner's AI Bot. Posting on their behalf"
    # Negated mentions ("never stands alone on its own line") describe the
    # retired shape without prescribing it.
    standalone_placement = re.compile(r"(?<!never )(?<!never stands )(?<!not )\balone on (?:its|the) (?:own |first )?line\b")
    if repo_agents and ATTRIBUTION_SECTION_HEADING not in repo_agents:
        findings.append(Finding("error", "AGENTS.md", f"Missing the `{ATTRIBUTION_SECTION_HEADING}` section; it is the single source of truth for the bot prefix."))
    for adapter in (home / ".codex" / "AGENTS.md", home / ".claude" / "CLAUDE.md", home / "AGENTS.md"):
        text = read_text(adapter)
        if not text:
            continue
        has_marker = attribution_marker.search(text) is not None
        if "prefixing with a 🤖" in text and not has_marker:
            findings.append(Finding("warn", str(adapter), "Managed adapter specifies generic robot attribution instead of the canonical `:robot_face:` prefix; fix through its external owner."))
        if legacy_attribution in text:
            findings.append(Finding("warn", str(adapter), "Adapter still restates the retired bot wording; the prefix is `:robot_face:` at the start of the first sentence (pm-os AGENTS.md Bot Attribution Prefix)."))
        if has_marker and standalone_placement.search(text):
            findings.append(Finding("warn", str(adapter), "Adapter still places the bot prefix alone on its own line; it opens the first sentence on the same line (pm-os AGENTS.md Bot Attribution Prefix)."))



def run_checks(repo_root: Path, home: Path, runtime_skill_names: set[str] | None = None) -> list[Finding]:
    runtime_adapter_map = repo_root / "system" / "config" / "runtime-adapter-map.yaml"
    values = (
        read_text(repo_root / "AGENTS.md"), read_text(repo_root / "CLAUDE.md"),
        read_text(home / ".claude" / "CLAUDE.md"), read_text(repo_root / "knowledge" / "todo-system-preferences.md"),
        read_text(repo_root / "system" / "behavior-map.md"), read_text(home / ".agents" / "skills" / "todo" / "SKILL.md"),
        read_text(home / "AGENTS.md"), load_yaml(runtime_adapter_map),
    )
    findings: list[Finding] = []
    _base_checks(findings, repo_root, home, runtime_adapter_map, *values)
    _expanded_contract_checks(findings, repo_root, home, runtime_skill_names)
    findings.extend(agents_md_size_findings(repo_root))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Check for instruction drift across PM OS control-plane surfaces.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when findings are present.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--home", type=Path, default=HOME, help=argparse.SUPPRESS)
    parser.add_argument("--skill-catalog", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    runtime_names = None
    if args.skill_catalog:
        payload = json.loads(args.skill_catalog.read_text())
        runtime_names = {item["name"] for item in payload.get("skills", [])}
    findings = run_checks(args.repo_root.resolve(), args.home.resolve(), runtime_names)

    if not findings:
        print("No instruction drift detected across checked PM OS surfaces.")
        return 0

    print("Instruction drift report")
    print()
    for finding in findings:
        print(f"[{finding.severity.upper()}] {finding.surface}: {finding.message}")

    if args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
