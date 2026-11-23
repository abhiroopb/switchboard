#!/usr/bin/env python3
"""Which agent CLI every switchboard launch starts: Claude Code or Amp.

Claude Code is the global default for every switchboard launch, and switching back to
Amp is one obvious knob. Dispatch workers, routines, start-day and the daemon
jobs all resolve the agent here, so none of them opens Amp from a Claude CoS
session by accident.

Precedence (one resolver; cmux-helpers.sh `switchboard_agent`, dispatch.py
`agent_kind`, start-day.sh and the headless daemon jobs all call it):

  1. SWITCHBOARD_AGENT env var, when it is `claude` or `amp` (case and spaces ignored)
  2. the top-level `agent:` key in system/config/router.yaml
  3. `claude`

An empty or unrecognised value at a level falls through to the next one. To
switch back to Amp, set `agent: amp` in router.yaml (or export SWITCHBOARD_AGENT=amp
for one process).

The config file is read with a line parser, not PyYAML, so launchd jobs running
under a python without yaml resolve the same answer. ROUTER_CONFIG points the
resolver at another file (tests, fixtures), as cmux-helpers.sh already does for
`cos_mode_trial`.

CLI: `python3 system/scripts/switchboard_agent.py [--config PATH] [--explain]` prints
`claude` or `amp` (with --explain, also the level that decided it).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

AGENTS = ("claude", "amp")
DEFAULT_AGENT = "claude"
_TOP_LEVEL_AGENT = re.compile(r"^agent\s*:\s*(.*)$")
AMP_THREAD_URL = "https://ampcode.com/threads/"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SESSION_ID = re.compile(_UUID)
_BARE_SESSION_LINK = re.compile(re.escape(AMP_THREAD_URL) + f"({_UUID})")
_MARKDOWN_SESSION_LINK = re.compile(r"\[[^\[\]\n]*\]\(" + re.escape(AMP_THREAD_URL) + f"({_UUID})" + r"\)")


def _normalise(value: object) -> str:
    text = str(value or "").strip().strip("'\"").strip().lower()
    return text if text in AGENTS else ""


def default_config_path() -> Path:
    override = os.environ.get("ROUTER_CONFIG")
    if override:
        return Path(override)
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from repo_paths import main_checkout  # same main-checkout rule as dispatch.py
        root = main_checkout(Path(os.environ.get("REPO_ROOT") or scripts.parents[1]))
    except Exception:
        root = Path(os.environ.get("REPO_ROOT") or scripts.parents[1])
    return root / "system" / "config" / "router.yaml"


def config_agent(path: Path | None = None) -> str:
    """The `agent:` value from router.yaml, or '' when missing or unrecognised."""
    path = path or default_config_path()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        match = _TOP_LEVEL_AGENT.match(line)  # top level only: no indent
        if match:
            return _normalise(match.group(1).split("#", 1)[0])
    return ""


def resolve(env: dict[str, str] | None = None, config_path: Path | None = None) -> tuple[str, str]:
    """(agent, source) where source is `env`, `config` or `default`."""
    env = os.environ if env is None else env
    from_env = _normalise(env.get("SWITCHBOARD_AGENT"))
    if from_env:
        return from_env, "env"
    from_config = config_agent(config_path)
    if from_config:
        return from_config, "config"
    return DEFAULT_AGENT, "default"


def agent(env: dict[str, str] | None = None, config_path: Path | None = None) -> str:
    return resolve(env, config_path)[0]


def thread_pointer(thread_id: object, agent: object = "") -> str:
    """How to reach a worker's conversation, or "" when there is no honest answer.

    An Amp thread has a web link. A Claude Code session has no web page, so it is
    named by the command that resumes it, never an ampcode.com link that leads
    nowhere. The ledger row's `agent` decides; a row
    with no agent falls back to the shape of the id, as cmux-shim.py does."""
    tid = str(thread_id or "").strip()
    amp_id = tid.startswith("T-")
    kind = _normalise(agent) or ("amp" if amp_id else "claude" if _SESSION_ID.fullmatch(tid) else "")
    if kind == "amp" and amp_id:
        return f"{AMP_THREAD_URL}{tid}"
    if kind == "claude" and tid and not amp_id:
        return f"claude --resume {tid}"
    return ""


def fix_thread_links(text: str) -> str:
    """Rewrite an ampcode.com link built from a Claude session id into the resume
    command. A report typed from an older launch prompt still carries one. Real Amp
    links (T-...) are left alone."""
    text = _MARKDOWN_SESSION_LINK.sub(lambda m: f"claude --resume {m.group(1)}", text)
    return _BARE_SESSION_LINK.sub(lambda m: f"claude --resume {m.group(1)}", text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="router.yaml to read (default: main checkout's)")
    parser.add_argument("--explain", action="store_true", help="also print which level decided")
    args = parser.parse_args(argv)
    chosen, source = resolve(config_path=args.config)
    print(f"{chosen} ({source})" if args.explain else chosen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
