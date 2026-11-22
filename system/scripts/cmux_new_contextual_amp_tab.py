#!/usr/bin/env python3
"""Launch a fresh, context-seeded Amp tab beside live sibling threads."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


THREAD_ID = re.compile(
    r"^T-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Policy: PM OS launches use only high/ultra, and ultra is the
# default for every new thread. high is honored only as an explicit config
# choice. Legacy medium/low configs coerce to ultra; missing/invalid modes
# default to ultra. Mirrors resolve_agent_mode in system/cmux-helpers.sh.
DEFAULT_MODE = "ultra"
VALID_MODES = {"high", "ultra"}
LEGACY_MODES = {"low", "medium"}
CONFIG_SCHEMA = "https://raw.githubusercontent.com/manaflow-ai/cmux/main/web/data/cmux.schema.json"

def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            result = os.replace(src, dst)
            if os.name == "nt":   # refresh worktree hard links of a shared file (lib/winlink.py)
                try:
                    from lib import winlink as _winlink
                    _winlink.relink_shared(dst)
                except Exception:  # noqa: BLE001
                    pass
            return result
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)

class PlanError(RuntimeError):
    """The new tab cannot be seeded without guessing."""

    def __init__(self, message: str, *, close_current: bool = True) -> None:
        super().__init__(message)
        self.close_current = close_current


@dataclass(frozen=True)
class LaunchPlan:
    cwd: Path
    mode: str
    pm_os_root: Path | None
    prompt: str
    sibling_thread_ids: tuple[str, ...]
    workstream_slug: str | None
    feedback_surface_id: str | None


def rpc(cmux: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [cmux, "rpc", method, json.dumps(params)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{method} failed")
    payload = json.loads(result.stdout)
    return payload if isinstance(payload, dict) else {}


def terminal_surfaces(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """surface.list is already in tab order; preserve it exactly."""
    return [surface for surface in surfaces if surface.get("type") == "terminal"]


def require_disposable_surface(
    surfaces: list[dict[str, Any]], current_surface_id: str | None
) -> None:
    """Never treat a live Amp tab as the launcher's disposable surface."""
    if not current_surface_id:
        raise PlanError(
            "Contextual Amp must start in a dedicated new terminal tab.",
            close_current=False,
        )
    current = next(
        (surface for surface in surfaces if surface.get("id") == current_surface_id),
        None,
    )
    if current is None:
        raise PlanError(
            "Could not identify the contextual launcher's terminal tab.",
            close_current=False,
        )
    binding = current.get("resume_binding")
    if isinstance(binding, dict) and binding.get("kind") == "amp":
        raise PlanError(
            "Refusing to run the contextual launcher inside a live Amp tab.",
            close_current=False,
        )


def amp_thread_ids(
    surfaces: list[dict[str, Any]], current_surface_id: str | None
) -> tuple[str, ...]:
    found: list[str] = []
    for surface in terminal_surfaces(surfaces):
        if surface.get("id") == current_surface_id:
            continue
        binding = surface.get("resume_binding")
        if not isinstance(binding, dict) or binding.get("kind") != "amp":
            continue
        checkpoint = binding.get("checkpoint_id")
        if (
            isinstance(checkpoint, str)
            and THREAD_ID.fullmatch(checkpoint)
            and checkpoint not in found
        ):
            found.append(checkpoint)
    return tuple(found)


def feedback_surface_id(
    surfaces: list[dict[str, Any]], current_surface_id: str | None
) -> str | None:
    candidates = [
        surface
        for surface in terminal_surfaces(surfaces)
        if surface.get("id") and surface.get("id") != current_surface_id
    ]
    for key in ("focused", "selected_in_pane"):
        for surface in candidates:
            if surface.get(key):
                return str(surface["id"])
    return str(candidates[0]["id"]) if candidates else None


def workspace_record(
    payload: dict[str, Any], workspace_id: str
) -> dict[str, Any]:
    for workspace in payload.get("workspaces", []):
        if workspace.get("id") == workspace_id or workspace.get("ref") == workspace_id:
            return workspace
    raise PlanError(f"Could not identify cmux workspace {workspace_id}.")


def cwd_candidates(
    surfaces: list[dict[str, Any]], workspace: dict[str, Any]
) -> list[Path]:
    terminals = terminal_surfaces(surfaces)
    ordered = [surface for surface in terminals if surface.get("focused")]
    ordered.extend(surface for surface in terminals if not surface.get("focused"))
    raw: list[str] = []
    for surface in ordered:
        binding = surface.get("resume_binding")
        if isinstance(binding, dict) and isinstance(binding.get("cwd"), str):
            raw.append(binding["cwd"])
        requested = surface.get("requested_working_directory")
        if isinstance(requested, str):
            raw.append(requested)
    if isinstance(workspace.get("current_directory"), str):
        raw.append(workspace["current_directory"])

    result: list[Path] = []
    for value in raw:
        path = Path(value).expanduser()
        if path.is_dir() and path not in result:
            result.append(path)
    return result


def find_pm_os_root(candidates: list[Path], script_path: Path) -> Path:
    roots: list[Path] = []
    env_root = os.environ.get("PM_OS_ROOT")
    if env_root:
        roots.append(Path(env_root).expanduser())
    roots.append(script_path.resolve().parent.parent.parent)
    roots.extend(candidates)
    roots.append(Path.home() / "Development" / "pm-os")

    visited: set[Path] = set()
    for start in roots:
        for candidate in (start, *start.parents):
            try:
                candidate = candidate.resolve()
            except OSError:
                continue
            if candidate in visited:
                continue
            visited.add(candidate)
            if (candidate / "workstreams").is_dir() and (candidate / "system").is_dir():
                return candidate
    raise PlanError("Could not locate the owning PM OS repository.")


def yaml_scalar(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    prefix = f"{key}:"
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped.split(":", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()
    return ""


def normalized_title(value: str) -> str:
    value = value.split(" - amp - ", 1)[0]
    tokens = unicodedata.normalize("NFKC", value).strip().split()
    if tokens and not re.search(r"[A-Za-z0-9]", tokens[0]):
        tokens.pop(0)
    if len(tokens) >= 2 and tokens[0].casefold() == "p0" and tokens[1] == "·":
        tokens = tokens[2:]
    return " ".join(tokens).casefold()


def detect_workstream(
    pm_os_root: Path, cwd_values: list[Path], workspace: dict[str, Any]
) -> tuple[str, Path]:
    workstreams = pm_os_root / "workstreams"
    explicit = os.environ.get("PM_OS_WORKSTREAM_SLUG", "").strip()
    if explicit:
        directory = workstreams / explicit
        if (directory / "CONTEXT.md").is_file():
            return explicit, directory

    for cwd in cwd_values:
        try:
            relative = cwd.resolve().relative_to(workstreams.resolve())
        except (OSError, ValueError):
            continue
        if relative.parts:
            directory = workstreams / relative.parts[0]
            if (directory / "CONTEXT.md").is_file():
                return relative.parts[0], directory

    raw_title = str(workspace.get("custom_title") or workspace.get("title") or "")
    title = normalized_title(raw_title)
    for directory in sorted(workstreams.iterdir()):
        if not directory.is_dir() or not (directory / "CONTEXT.md").is_file():
            continue
        name = yaml_scalar(directory / "config.yaml", "name")
        aliases = {directory.name, directory.name.replace("-", " ")}
        if name:
            aliases.add(name)
        if title in {normalized_title(alias) for alias in aliases}:
            return directory.name, directory
    raise PlanError("Could not map this workspace to a PM OS workstream.")


def resolve_mode(config_path: Path) -> str:
    mode = yaml_scalar(config_path, "agent_mode").casefold()
    if mode in VALID_MODES:
        return mode
    if mode in LEGACY_MODES:
        # Compatibility: medium/low never reach Amp.
        return DEFAULT_MODE
    return DEFAULT_MODE


def build_prompt(
    pm_os_root: Path,
    workstream_slug: str,
    workstream_dir: Path,
    sibling_ids: tuple[str, ...],
) -> str:
    paths = [workstream_dir / "CONTEXT.md"]
    for optional in (
        workstream_dir / "config.yaml",
        workstream_dir / ".signals" / "recent.md",
    ):
        if optional.exists():
            paths.append(optional)
    sources = workstream_dir / "sources"

    relative_paths = [path.relative_to(pm_os_root).as_posix() for path in paths]
    read_steps = "\n".join(f"- Read `{path}`." for path in relative_paths)
    if sources.is_dir():
        read_steps += f"\n- Read the relevant source material under `{sources.relative_to(pm_os_root).as_posix()}/` recursively."
    mentions = " ".join(f"@{thread_id}" for thread_id in sibling_ids)

    return f"""This is a parallel Amp tab for PM OS workstream `{workstream_slug}`. It is a fresh local thread, not a continuation of any live sibling.

Before acting:
{read_steps}
- Extract and check the latest progress in every sibling Amp thread, in tab order: {mentions}

Use sibling progress only as read-only context. Do not send coordination, status, or acknowledgement messages to sibling threads. Avoid duplicating work that a sibling has already completed or is actively doing. Never run `amp threads continue` against a live sibling. Stay on this machine: do not use an Amp Orb, a remote VM, SSH, cloud execution, or remote subagent.

After reading the workstream files, sources, and sibling progress, state what distinct parallel contribution you will take on, then proceed only if it does not duplicate sibling work."""


def build_generic_prompt(
    cwd: Path, workspace: dict[str, Any], sibling_ids: tuple[str, ...]
) -> str:
    title = str(workspace.get("custom_title") or workspace.get("title") or "Untitled")
    if sibling_ids:
        mentions = " ".join(f"@{thread_id}" for thread_id in sibling_ids)
        sibling_step = (
            "Before acting, extract and check the latest progress in every sibling "
            f"Amp thread, in tab order: {mentions}\n\n"
            "Use sibling progress only as read-only context. Do not send coordination, "
            "status, or acknowledgement messages to sibling threads. Avoid "
            "duplicating work that a sibling has already completed or is actively doing. "
        )
        closing = (
            "After checking sibling progress, state what distinct parallel contribution "
            "you will take on, then proceed only if it does not duplicate sibling work."
        )
    else:
        sibling_step = "No live sibling Amp threads were found; begin as a fresh thread.\n\n"
        closing = "Proceed with the user's next request from this fresh local thread."

    return f"""This is a fresh parallel Amp tab in cmux workspace `{title}`. It is not a continuation of another thread.

Use `{cwd}` as the working directory.

{sibling_step}Never run `amp threads continue` against a live sibling. Stay on this machine: do not use an Amp Orb, a remote VM, SSH, cloud execution, or remote subagent.

{closing}"""


def make_plan(
    surface_payload: dict[str, Any],
    workspace_payload: dict[str, Any],
    workspace_id: str,
    current_surface_id: str | None,
    script_path: Path,
) -> LaunchPlan:
    surfaces = surface_payload.get("surfaces", [])
    if not isinstance(surfaces, list):
        surfaces = []
    require_disposable_surface(surfaces, current_surface_id)
    siblings = amp_thread_ids(surfaces, current_surface_id)

    workspace = workspace_record(workspace_payload, workspace_id)
    candidates = cwd_candidates(surfaces, workspace)
    cwd = candidates[0] if candidates else Path.home()
    if not cwd.is_dir():
        cwd = Path.home()

    try:
        pm_os_root = find_pm_os_root(candidates, script_path)
        slug, workstream_dir = detect_workstream(pm_os_root, candidates, workspace)
    except PlanError:
        pm_os_root = None
        slug = None
        workstream_dir = None

    return LaunchPlan(
        cwd=cwd,
        mode=(
            resolve_mode(workstream_dir / "config.yaml")
            if workstream_dir is not None
            else DEFAULT_MODE
        ),
        pm_os_root=pm_os_root,
        prompt=(
            build_prompt(pm_os_root, slug, workstream_dir, siblings)
            if pm_os_root is not None and slug is not None and workstream_dir is not None
            else build_generic_prompt(cwd, workspace, siblings)
        ),
        sibling_thread_ids=siblings,
        workstream_slug=slug,
        feedback_surface_id=feedback_surface_id(surfaces, current_surface_id),
    )


def notify(
    cmux: str,
    workspace_id: str,
    surface_id: str | None,
    title: str,
    body: str,
) -> None:
    command = [cmux, "notify", "--title", title, "--body", body, "--workspace", workspace_id]
    if surface_id:
        command += ["--surface", surface_id]
    subprocess.run(command, capture_output=True, text=True)


def close_surface(cmux: str, workspace_id: str, surface_id: str | None) -> None:
    if not surface_id:
        return
    try:
        rpc(cmux, "surface.close", {"workspace_id": workspace_id, "surface_id": surface_id})
    except Exception:
        pass


def fail_closed(
    cmux: str,
    workspace_id: str,
    current_surface_id: str | None,
    feedback_id: str | None,
    message: str,
) -> int:
    notify(cmux, workspace_id, feedback_id, "Contextual Amp tab failed", message)
    close_surface(cmux, workspace_id, current_surface_id)
    return 1


def find_amp() -> str | None:
    configured = os.environ.get("AMP_BIN")
    if configured and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser())
    return shutil.which("amp") or (
        str(Path.home() / ".amp" / "bin" / "amp")
        if (Path.home() / ".amp" / "bin" / "amp").is_file()
        else None
    )


def split_action(
    helper_path: Path, direction: str, *, shortcut: str | None = None
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "command",
        "command": (
            f"/usr/bin/python3 {shlex.quote(helper_path.as_posix())} "
            f"--spawn-split {direction}"
        ),
        "target": "newTabInCurrentPane",
        "title": "New contextual Amp pane",
        "palette": False,
    }
    if shortcut:
        action["shortcut"] = shortcut
    return action


def spawn_split(
    cmux: str,
    workspace_id: str,
    current_surface_id: str,
    direction: str,
    helper_path: Path,
) -> int:
    feedback_id: str | None = None
    try:
        surfaces_payload = rpc(cmux, "surface.list", {"workspace_id": workspace_id})
        surfaces = surfaces_payload.get("surfaces", [])
        if not isinstance(surfaces, list):
            surfaces = []
        feedback_id = feedback_surface_id(surfaces, current_surface_id)
        launcher = (
            f"/usr/bin/python3 {shlex.quote(helper_path.as_posix())} "
            f"--exec {shlex.quote(workspace_id)}"
        )
        initial_command = f"/bin/zsh -lc {shlex.quote(f'sleep 0.2; exec {launcher}')}"
        split = rpc(
            cmux,
            "surface.split",
            {
                "workspace_id": workspace_id,
                "surface_id": current_surface_id,
                "direction": direction,
                "type": "terminal",
                "initial_command": initial_command,
                "focus": True,
            },
        )
        split_surface_id = split.get("surface_id")
        if not isinstance(split_surface_id, str) or not split_surface_id:
            raise RuntimeError("cmux did not identify the new split surface")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            spawned_payload = rpc(
                cmux, "surface.list", {"workspace_id": workspace_id}
            )
            spawned = next(
                (
                    surface
                    for surface in spawned_payload.get("surfaces", [])
                    if surface.get("id") == split_surface_id
                ),
                None,
            )
            binding = spawned.get("resume_binding") if isinstance(spawned, dict) else None
            if isinstance(binding, dict) and binding.get("kind") == "amp":
                break
            if spawned is None:
                raise RuntimeError("the seeded split closed before Amp started")
            time.sleep(0.1)
        else:
            close_surface(cmux, workspace_id, split_surface_id)
            raise RuntimeError("timed out waiting for Amp to start in the new split")
    except Exception as error:
        return fail_closed(
            cmux,
            workspace_id,
            current_surface_id,
            feedback_id,
            f"Could not create the contextual split: {error}",
        )
    close_surface(cmux, workspace_id, current_surface_id)
    return 0


def shortcut_action(helper_path: Path, shortcut: str) -> dict[str, Any]:
    return split_action(helper_path, "right", shortcut=shortcut)


def install_shortcuts(config_path: Path, helper_path: Path) -> Path | None:
    """Idempotently install cmux-only shortcuts without dropping other settings."""
    if config_path.exists():
        data = json.loads(config_path.read_text())
        if not isinstance(data, dict):
            raise PlanError(f"Expected a JSON object in {config_path}.")
    else:
        data = {"$schema": CONFIG_SCHEMA, "schemaVersion": 1}

    actions = data.setdefault("actions", {})
    shortcuts = data.setdefault("shortcuts", {})
    if not isinstance(actions, dict) or not isinstance(shortcuts, dict):
        raise PlanError("Existing cmux actions and shortcuts must be JSON objects.")
    bindings = shortcuts.setdefault("bindings", {})
    if not isinstance(bindings, dict):
        raise PlanError("Existing cmux shortcuts.bindings must be a JSON object.")

    migrated_vault = False
    vault = data.get("vault")
    if isinstance(vault, dict) and isinstance(vault.get("agents"), list):
        agents = vault["agents"]
        native_agents = [
            agent
            for agent in agents
            if not (isinstance(agent, dict) and agent.get("id") == "amp")
        ]
        if native_agents != agents:
            migrated_vault = True
            if native_agents:
                vault["agents"] = native_agents
            else:
                data.pop("vault")

    expected = {
        "cmux.newTerminal": shortcut_action(helper_path, "cmd+t"),
        "pm-os.newContextualAmpTabCtrl": shortcut_action(helper_path, "ctrl+t"),
        "cmux.splitRight": split_action(helper_path, "right"),
        "cmux.splitDown": split_action(helper_path, "down"),
    }
    migrated_action = actions.pop("pm-os.newContextualAmpTabCmd", None) is not None
    changed = migrated_vault or migrated_action or any(
        actions.get(key) != value for key, value in expected.items()
    )
    changed = changed or bindings.get("newSurface") != "disabled"
    if not changed:
        return None

    backup: Path | None = None
    if config_path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = config_path.with_name(f"{config_path.name}.bak.{stamp}")
        shutil.copy2(config_path, backup)

    actions.update(expected)
    bindings["newSurface"] = "disabled"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(f".{config_path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    _os_replace(temporary, config_path)
    return backup


def record_contextual_binding(cmux, workspace, surface_id, plan, stopped, role="worker"):
    """Observe only the new surface, never borrow a sibling thread's identity."""
    # The sidebar installer copies this launcher alone, so resolve the shared
    # writer from the PM OS checkout already discovered by make_plan.
    sys.path.insert(0, str(plan.pm_os_root / "system/scripts"))
    from workstream_thread_bindings import record_binding

    deadline = time.monotonic() + 60
    while not stopped.wait(0.2) and time.monotonic() < deadline:
        try:
            payload = rpc(cmux, "surface.list", {"workspace_id": workspace["id"]})
            surface = next((s for s in payload.get("surfaces", []) if s.get("id") == surface_id), {})
            binding = surface.get("resume_binding") or {}
            thread_id = binding.get("checkpoint_id") or ""
            if (binding.get("kind") == "amp" and THREAD_ID.fullmatch(thread_id)
                    and thread_id not in plan.sibling_thread_ids and not stopped.is_set()):
                record_binding(plan.pm_os_root, thread_id, plan.workstream_slug,
                               workspace["id"], workspace["ref"], role=role,
                               evidence={"surface_id": surface_id, "launcher": "contextual"})
                return
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # A loaded cmux may time out while Amp is already alive. The next
            # sample must still match this exact surface and a new thread.
            continue
        except (ValueError, KeyError) as error:
            print(f"Contextual binding not recorded: {error}", file=sys.stderr)
            return


def execute_owner(cmux, root, slug, command):
    """Observe from inside the created tab, independent of its parent's RPC."""
    try:
        workspace_id = os.environ["CMUX_WORKSPACE_ID"]
        surface_id = os.environ["CMUX_SURFACE_ID"]
        workspace = workspace_record(rpc(cmux, "workspace.list", {}), workspace_id)
        surfaces = rpc(cmux, "surface.list", {"workspace_id": workspace_id}).get("surfaces", [])
        if not any(s.get("id") == surface_id for s in surfaces):
            raise PlanError("Owner launcher surface is not observable.", close_current=False)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        # Binding is evidence, not a prerequisite for starting Amp. Without a
        # before-snapshot we cannot distinguish a new thread from stale metadata;
        # leave binding to the parent recovery/seed path rather than guessing.
        print(f"Owner binding observer unavailable: {error}", file=sys.stderr)
        return subprocess.run(command).returncode
    # Exclude every prior binding, including a crashed thread on this surface.
    plan = LaunchPlan(Path.cwd(), DEFAULT_MODE, root, "", amp_thread_ids(surfaces, None), slug, None)
    stopped = threading.Event()
    observer = threading.Thread(target=record_contextual_binding,
                                args=(cmux, workspace, surface_id, plan, stopped, "owner"), daemon=True)
    observer.start()
    try:
        # Inherit the launch prompt's stdin and the real terminal's stdout.
        return subprocess.run(command).returncode
    finally:
        stopped.set()
        observer.join(timeout=1)


def execute(workspace_id: str, cmux: str, current_surface_id: str | None) -> int:
    surfaces: list[dict[str, Any]] = []
    feedback_id: str | None = None
    try:
        deadline = time.monotonic() + 3
        while True:
            surface_payload = rpc(cmux, "surface.list", {"workspace_id": workspace_id})
            surfaces = surface_payload.get("surfaces", [])
            if not isinstance(surfaces, list):
                surfaces = []
            if not current_surface_id or any(
                surface.get("id") == current_surface_id for surface in surfaces
            ):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        feedback_id = feedback_surface_id(surfaces, current_surface_id)
        workspace_payload = rpc(cmux, "workspace.list", {})
        plan = make_plan(
            surface_payload,
            workspace_payload,
            workspace_id,
            current_surface_id,
            Path(__file__),
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        close_id = current_surface_id
        if isinstance(error, PlanError) and not error.close_current:
            close_id = None
        return fail_closed(
            cmux, workspace_id, close_id, feedback_id, str(error)
        )

    amp = find_amp()
    if not amp:
        return fail_closed(
            cmux, workspace_id, current_surface_id, plan.feedback_surface_id, "Amp executable not found."
        )

    if current_surface_id:
        try:
            rpc(
                cmux,
                "surface.focus",
                {"workspace_id": workspace_id, "surface_id": current_surface_id},
            )
        except Exception:
            pass

    environment = dict(os.environ)
    if plan.workstream_slug:
        environment["PM_OS_WORKSTREAM_SLUG"] = plan.workstream_slug
    else:
        environment.pop("PM_OS_WORKSTREAM_SLUG", None)
    stopped = threading.Event()
    observer = None
    if plan.workstream_slug and current_surface_id:
        workspace = workspace_record(workspace_payload, workspace_id)
        observer = threading.Thread(target=record_contextual_binding,
                                    args=(cmux, workspace, current_surface_id, plan, stopped), daemon=True)
        observer.start()
    try:
        result = subprocess.run(
            [amp, "-m", plan.mode],
            cwd=plan.cwd,
            env=environment,
            input=plan.prompt + "\n",
            text=True,
        )
    except OSError as error:
        return fail_closed(
            cmux, workspace_id, current_surface_id, plan.feedback_surface_id, f"Amp launch failed: {error}"
        )
    finally:
        stopped.set()
        if observer:
            observer.join(timeout=1)
    if result.returncode != 0:
        return fail_closed(
            cmux,
            workspace_id,
            current_surface_id,
            plan.feedback_surface_id,
            f"Amp rejected the initial seed (exit {result.returncode}); the empty tab was removed.",
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--exec", dest="workspace_id")
    action.add_argument("--spawn-split", choices=("right", "down"))
    action.add_argument("--install-config", action="store_true")
    action.add_argument("--launch-owner", metavar="SLUG")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config-path", type=Path, default=Path.home() / ".config" / "cmux" / "cmux.json")
    parser.add_argument("--helper-path", type=Path, default=Path(__file__).resolve())
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.install_config:
        backup = install_shortcuts(args.config_path, args.helper_path)
        if backup:
            print(f"Installed contextual Amp shortcuts (backup: {backup})")
        else:
            print("Contextual Amp shortcuts already installed")
        return 0
    cmux = os.environ.get(
        "CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux"
    )
    if args.launch_owner:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not args.root or not command:
            parser.error("--launch-owner requires --root and -- <amp command>")
        return execute_owner(cmux, args.root, args.launch_owner, command)
    if args.spawn_split:
        workspace_id = os.environ.get("CMUX_WORKSPACE_ID")
        surface_id = os.environ.get("CMUX_SURFACE_ID")
        if not workspace_id or not surface_id:
            raise PlanError("The contextual split must run inside a cmux terminal.")
        return spawn_split(
            cmux,
            workspace_id,
            surface_id,
            args.spawn_split,
            args.helper_path,
        )
    return execute(args.workspace_id, cmux, os.environ.get("CMUX_SURFACE_ID"))


if __name__ == "__main__":
    sys.exit(main())
