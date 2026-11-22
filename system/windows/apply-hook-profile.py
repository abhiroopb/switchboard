#!/usr/bin/env python3
"""Windows hook profile: drop the Claude Code hooks that do nothing on this machine.

    python3 system/windows/apply-hook-profile.py            # apply and print what changed
    python3 system/windows/apply-hook-profile.py --check    # exit 1 when not applied
    python3 system/windows/apply-hook-profile.py --undo     # restore both

Why: a kernel leak on some Windows machines keeps every exited process until reboot, and
Claude Code starts each hook through bash: bash, a conhost, a forked bash, then the program. A Bash tool call ran
eight hooks, about 38 leaked processes, before its own command. Two of them have no effect here:

- cmux-heartbeat.py exits at once when wmux's own Claude hooks are installed (its docstring says so). It is removed
  from the installed copy of the pm-os plugin only while settings.json holds those hooks. The repo's hooks.json is
  what macOS reads, and it does not change.
- The hookify plugin runs python on every tool call to read its rule files. It is turned off only when no rule file
  exists. Writing a rule later means turning it back on with --undo.

A plugin reinstall restores the heartbeat hooks, so install-claude-code.sh runs this again and
check-windows-install.py reports when it has lapsed. Windows only; elsewhere it does nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HOOKIFY = "hookify@claude-plugins-official"
PM_OS_PLUGIN = "pm-os@pm-os-local"
HEARTBEAT = "cmux-heartbeat.py"
WMUX_HOOK = "wmux-hook.js"


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def plugin_hooks_file(base: Path) -> Path | None:
    try:
        installed = read_json(base / "plugins" / "installed_plugins.json")
    except (OSError, ValueError):
        return None
    for entry in installed.get("plugins", {}).get(PM_OS_PLUGIN, []):
        candidate = Path(entry.get("installPath", "")) / "hooks" / "hooks.json"
        if candidate.is_file():
            return candidate
    return None


def wmux_hooks_installed(settings: dict) -> bool:
    return WMUX_HOOK in json.dumps(settings.get("hooks") or {})


def has_heartbeat(hooks: dict) -> bool:
    """Any heartbeat hook command; the file's description also names the script, so never search the text."""
    return any(HEARTBEAT in str(hook.get("command", ""))
               for groups in hooks.get("hooks", {}).values() for group in groups for hook in group.get("hooks", []))


def without_heartbeat(hooks: dict) -> dict:
    """The hooks document with every heartbeat command removed, and any group left empty dropped."""
    out = json.loads(json.dumps(hooks))
    events = out.get("hooks", {})
    for event in list(events):
        groups = []
        for group in events[event]:
            kept = [h for h in group.get("hooks", []) if HEARTBEAT not in str(h.get("command", ""))]
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            events[event] = groups
        else:
            del events[event]
    return out


def has_hookify_rules(base: Path, repo: Path) -> bool:
    for root in (base, repo / ".claude"):
        if root.is_dir() and any(root.glob("hookify.*.local.md")):
            return True
    return False


def repo_root() -> Path:
    return Path(os.environ.get("PM_OS_ROOT") or Path(__file__).resolve().parents[2])


def status(base: Path, repo: Path) -> list[str]:
    """What still needs applying; empty when the profile is in place."""
    todo = []
    settings = read_json(base / "settings.json")
    hooks_file = plugin_hooks_file(base)
    if hooks_file and wmux_hooks_installed(settings) and has_heartbeat(read_json(hooks_file)):
        todo.append("heartbeat hooks present in the installed pm-os plugin")
    if settings.get("enabledPlugins", {}).get(HOOKIFY) and not has_hookify_rules(base, repo):
        todo.append("hookify enabled with no rule files")
    return todo


def apply(base: Path, repo: Path) -> list[str]:
    done = []
    settings_path = base / "settings.json"
    settings = read_json(settings_path)
    hooks_file = plugin_hooks_file(base)
    if hooks_file and wmux_hooks_installed(settings) and has_heartbeat(read_json(hooks_file)):
        write_json(hooks_file, without_heartbeat(read_json(hooks_file)))
        done.append(f"removed heartbeat hooks from {hooks_file}")
    if settings.get("enabledPlugins", {}).get(HOOKIFY) and not has_hookify_rules(base, repo):
        settings["enabledPlugins"][HOOKIFY] = False
        write_json(settings_path, settings)
        done.append(f"turned off {HOOKIFY} (no rule files)")
    return done


def undo(base: Path, repo: Path) -> list[str]:
    done = []
    settings_path = base / "settings.json"
    settings = read_json(settings_path)
    if settings.get("enabledPlugins", {}).get(HOOKIFY) is False:
        settings["enabledPlugins"][HOOKIFY] = True
        write_json(settings_path, settings)
        done.append(f"turned on {HOOKIFY}")
    hooks_file = plugin_hooks_file(base)
    source = repo / "system" / "claude-marketplace" / "plugins" / "pm-os" / "hooks" / "hooks.json"
    if hooks_file and source.is_file():
        shutil.copyfile(source, hooks_file)
        done.append(f"restored {hooks_file}")
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--undo", action="store_true")
    args = ap.parse_args(argv)
    if os.name != "nt" and not os.environ.get("PM_OS_HOOK_PROFILE_ANY_OS"):
        print("hook profile: Windows only, nothing to do")
        return 0
    base, repo = claude_dir(), repo_root()
    if not (base / "settings.json").is_file():
        print(f"hook profile: no {base / 'settings.json'}")
        return 0
    if args.check:
        todo = status(base, repo)
        print("hook profile: " + ("; ".join(todo) if todo else "applied"))
        return 1 if todo else 0
    done = undo(base, repo) if args.undo else apply(base, repo)
    print("hook profile: " + ("; ".join(done) if done else "nothing to change"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
