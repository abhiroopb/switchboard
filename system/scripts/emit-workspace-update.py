#!/usr/bin/env python3

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Sibling import kept importlib-safe: test harnesses load this file via
# spec_from_file_location, which does not put the script dir on sys.path.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from events_stream import append_event_line  # noqa: E402
import review_surface  # noqa: E402


# Workstream tabs should stay actionable by default. Once a workstream settles
# into a waiting-like state, auto-close its cmux tab unless the workstream opts
# into explicit watchlist behavior via `keep_open_when_waiting: true`.
AUTO_CLOSE_STATUSES = {"waiting", "paused", "scheduled", "needs follow-up", "done", "resolved", "cancelled", "archived"}

# Startup-hydration provenance. start-day.sh /
# ensure-p0-workspaces.sh mark each machine-seeded startup turn with a
# hydration marker; the review-lane plugin re-persists it as a turn-scoped
# hydration-turn-<ws>.json for the duration of that turn. The generic launch
# prompt teaches agents to emit `--status "to review"` for any completed work,
# so during a hydration turn a plain "to review" is startup noise, not a
# genuine escalation: demote it to "done" (preserving requested_status) so it
# cannot paint the tab purple or move a P0 into the To Review lane. Explicit
# escalations (blocked / needs action / needs review, or new_actions with
# requires_user=true) pass through untouched.
HYDRATION_DEMOTED_STATUSES = {"to review"}
# Must match HYDRATION_TTL_MS in system/amp-plugins/review-lane.ts.
HYDRATION_TTL_SECONDS = 6 * 60 * 60


def review_lane_state_dir() -> Path:
    override = os.environ.get("REVIEW_LANE_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "amp" / "plugin-state" / "review-lane"


def current_turn_is_startup_hydration() -> bool:
    """True when this emit runs inside a machine-seeded startup turn.

    Checks the turn-scoped marker the review-lane plugin persists at
    agent.start, and falls back to the launcher's original marker for
    headless runs where the plugin is inert and never consumes it. Stale
    markers (past TTL) are debris from failed launches, not live provenance.
    """
    workspace_id = os.environ.get("CMUX_WORKSPACE_ID")
    if not workspace_id:
        return False
    state_dir = review_lane_state_dir()
    for name in (f"hydration-turn-{workspace_id}.json", f"hydration-{workspace_id}.json"):
        path = state_dir / name
        if not path.exists():
            continue
        try:
            at_ms = float(json.loads(path.read_text()).get("at") or 0)
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue
        if at_ms > 0 and (datetime.now(timezone.utc).timestamp() - at_ms / 1000) <= HYDRATION_TTL_SECONDS:
            return True
    return False


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def write_json_file(path: Path, payload: object) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def with_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def parse_new_action(raw: str) -> dict[str, object]:
    parts = [part.strip() for part in raw.split("|")]
    title = parts[0]
    estimate = int(parts[1]) if len(parts) > 1 and parts[1] else 15
    priority = parts[2].lower() if len(parts) > 2 and parts[2] else "medium"
    requires_user = None
    if len(parts) > 3 and parts[3]:
        lowered = parts[3].lower()
        if lowered in {"1", "true", "yes", "user", "human", "requires_user", "requires-user"}:
            requires_user = True
        elif lowered in {"0", "false", "no", "agent", "agent-executable", "agent_executable"}:
            requires_user = False
    return {"title": title, "estimate_min": estimate, "priority": priority, "requires_user": requires_user}


def parse_todo_update(raw: str) -> dict[str, str | None]:
    parts = [part.strip() for part in raw.split("|")]
    todo_id = parts[0]
    status = parts[1].lower() if len(parts) > 1 and parts[1] else "open"
    when = parts[2] if len(parts) > 2 and parts[2] else None
    return {"id": todo_id, "status": status, "when": when}


def run_todo_cli(todo_args: list[str]) -> None:
    # Todo write-backs must not fail silently: a
    # --todo-update/--linked-todos flag that quietly no-ops leaves the todo
    # system out of sync with the emitted status.
    todo_cli = Path.home() / ".agents" / "skills" / "todo" / "scripts" / "todo_cli.py"
    result = subprocess.run(
        [sys.executable, str(todo_cli), *todo_args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no output").strip()[:200]
        print(
            f"[emit-workspace-update] WARNING: todo_cli {' '.join(todo_args)} "
            f"exited {result.returncode}: {detail}",
            file=sys.stderr,
        )


def linked_todo_ids_for_source(repo_root: Path, source: str) -> list[str]:
    if not source.startswith("workstream:"):
        return []
    slug = source.split(":", 1)[1]
    todo_path = repo_root / "system" / "state" / "todo.json"
    if not todo_path.exists():
        return []
    try:
        payload = json.loads(todo_path.read_text())
    except json.JSONDecodeError:
        return []
    ids = []
    for item in payload.get("items", []):
        if str(item.get("workstream") or "").strip() != slug:
            continue
        if str(item.get("status") or "").strip().lower() in {"done", "completed", "complete", "closed", "resolved"}:
            continue
        item_id = item.get("id")
        if item_id:
            ids.append(str(item_id))
    return ids


def apply_todo_updates(repo_root: Path, args: argparse.Namespace) -> None:
    explicit_updates = [parse_todo_update(raw) for raw in args.todo_update]
    for update in explicit_updates:
        if update["status"] == "done":
            run_todo_cli(["complete", "--id", str(update["id"])])
            continue

        command = ["set-status", "--id", str(update["id"]), "--status", str(update["status"])]
        if update["status"] == "waiting" and update["when"]:
            command.extend(["--review-on", str(update["when"])])
        elif update["status"] == "scheduled" and update["when"]:
            command.extend(["--snooze-until", str(update["when"])])
        run_todo_cli(command)

    if not args.linked_todos:
        return

    linked_ids = linked_todo_ids_for_source(repo_root, args.source)
    for todo_id in linked_ids:
        if args.linked_todos == "done":
            run_todo_cli(["complete", "--id", todo_id])
            continue

        command = ["set-status", "--id", todo_id, "--status", args.linked_todos]
        if args.linked_todos == "waiting" and args.linked_review_on:
            command.extend(["--review-on", args.linked_review_on])
        elif args.linked_todos == "scheduled" and args.linked_snooze_until:
            command.extend(["--snooze-until", args.linked_snooze_until])
        run_todo_cli(command)


def has_manual_open_workstream(repo_root: Path, slug: str) -> bool:
    manual_path = repo_root / "system" / "state" / "sources" / "manual-open-workstreams.json"
    if not manual_path.exists():
        return False

    try:
        payload = json.loads(manual_path.read_text())
    except json.JSONDecodeError:
        return False

    entry = (payload.get("workstreams") or {}).get(slug)
    if not isinstance(entry, dict):
        return False

    expires_at = parse_iso(entry.get("expires_at"))
    if expires_at and expires_at >= datetime.now(timezone.utc):
        return True

    return False


def has_surfaced_p0_for_workstream(repo_root: Path, slug: str) -> bool:
    """True when a surfaced P0 todo links this workstream.

    Such a workstream owns a canonical "🔥 P0 · <display>" tab that must stay
    visible on the board all day; a waiting/paused status from the seeded
    agent must never auto-close it. Uses the shared classifier
    (system/scripts/lib/p0-todos.py) so "surfaced" means exactly what
    ensure-p0-workspaces.sh and cmux-helpers.sh mean by it.
    """
    todo_path = repo_root / "system" / "state" / "todo.json"
    classifier = repo_root / "system" / "scripts" / "lib" / "p0-todos.py"
    if not todo_path.exists() or not classifier.exists():
        return False
    result = subprocess.run(
        [sys.executable, str(classifier), str(todo_path), "--slug", slug],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def should_keep_workstream_open(repo_root: Path, slug: str) -> bool:
    config_path = repo_root / "workstreams" / slug / "config.yaml"
    if config_path.exists():
        text = config_path.read_text()
        if bool(re.search(r"^\s*keep_open_when_waiting\s*:\s*true\s*$", text, flags=re.IGNORECASE | re.MULTILINE)):
            return True

    if has_surfaced_p0_for_workstream(repo_root, slug):
        return True

    return has_manual_open_workstream(repo_root, slug)


def clear_manual_open_workstream(repo_root: Path, slug: str) -> None:
    manual_path = repo_root / "system" / "state" / "sources" / "manual-open-workstreams.json"
    lock_path = manual_path.with_name(f".{manual_path.name}.lock")
    with with_file_lock(lock_path):
        if not manual_path.exists():
            return

        try:
            payload = json.loads(manual_path.read_text())
        except json.JSONDecodeError:
            return

        entries = payload.get("workstreams")
        if not isinstance(entries, dict) or slug not in entries:
            return

        entries = {key: value for key, value in entries.items() if key != slug}
        payload["workstreams"] = entries
        payload["updated_at"] = now_iso()
        write_json_file(manual_path, payload)


# Maps a workstream source key to the scheduled-jobs it owns. When that
# workstream emits a terminal status (done/resolved/etc), the matching
# entries in system/job-state.json are stamped with today's date so the
# scheduler doesn't re-fire the same job on the next CoS run. This makes
# the close-out write deterministic instead of relying on the agent
# remembering to call mark-scheduled-jobs-run.py in its prompt.
WORKSTREAM_SCHEDULED_JOBS: dict[str, list[str]] = {}
TERMINAL_STATUSES = {"done", "resolved", "cancelled", "archived"}


def mark_scheduled_jobs_for_workstream(repo_root: Path, source: str, status: str) -> None:
    if not source.startswith("workstream:"):
        return
    if str(status or "").strip().lower() not in TERMINAL_STATUSES:
        return
    slug = source.split(":", 1)[1]
    jobs = WORKSTREAM_SCHEDULED_JOBS.get(slug)
    if not jobs:
        return
    subprocess.run(
        [sys.executable, str(repo_root / "system" / "scripts" / "mark-scheduled-jobs-run.py"), *jobs],
        check=False,
    )


def resolve_workspace_ref(repo_root: Path, source: str) -> str:
    if not source.startswith("workstream:"):
        return ""
    slug = source.split(":", 1)[1]
    helpers_path = repo_root / "system" / "cmux-helpers.sh"
    result = subprocess.run(
        [
            "bash",
            "-lc",
            'source "$1" && find_workspace_ref_for_source_key workstream "$2"',
            "_",
            str(helpers_path),
            slug,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip().splitlines()[-1].strip() if (result.stdout or "").strip() else ""


def maybe_close_waiting_workspace(
    repo_root: Path,
    source: str,
    status: str,
    close_workspace: bool = False,
    workspace_ref: str = "",
) -> bool:
    if not source.startswith("workstream:"):
        return True

    normalized_status = str(status or "").strip().lower()
    if normalized_status not in AUTO_CLOSE_STATUSES:
        return True

    slug = source.split(":", 1)[1]
    if not close_workspace and should_keep_workstream_open(repo_root, slug):
        return True

    helpers_path = repo_root / "system" / "cmux-helpers.sh"
    target_ref = workspace_ref or resolve_workspace_ref(repo_root, source)
    if not target_ref:
        return False
    result = subprocess.run(
        [
            "bash",
            "-lc",
            'source "$1" && close_workspace_ref_verified "$2"',
            "_",
            str(helpers_path),
            target_ref,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    closed = result.returncode == 0
    event = {
        "timestamp": now_iso(),
        "type": "workspace_autoclosed" if closed else "workspace_close_failed",
        "source": source,
        "status": normalized_status,
        "workspace_ref": target_ref,
        "reason": "explicit lifecycle close" if close_workspace else "waiting-status auto-close (emit-workspace-update)",
    }
    if not closed:
        event["error"] = (result.stderr or result.stdout or "cmux close failed").strip()
    try:
        append_event_line(repo_root / "system" / "state", event)
    except OSError:
        pass
    return closed


def current_update_matches(
    payload: dict[str, object],
    source: str,
    expected_update_id: str | None,
    expected_timestamp: str | None,
    expected_status: str | None,
) -> bool:
    if not expected_update_id and not expected_timestamp and not expected_status:
        return True
    updates = payload.get("updates")
    if not isinstance(updates, dict):
        return False
    current = updates.get(source)
    if not isinstance(current, dict):
        return False
    if expected_update_id and current.get("update_id") != expected_update_id:
        return False
    # A legacy snapshot has only a second-granularity timestamp. If a newer
    # emitter has added an opaque ID—even in the same second—it must win.
    if expected_timestamp and not expected_update_id and current.get("update_id"):
        return False
    if expected_timestamp and current.get("timestamp") != expected_timestamp:
        return False
    if expected_status and str(current.get("status") or "").strip().lower() != expected_status.strip().lower():
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="e.g. workstream:book-club")
    parser.add_argument("--status", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--workspace")
    parser.add_argument("--recommended-next-action", dest="recommended_next_action")
    parser.add_argument("--new-action", action="append", default=[])
    parser.add_argument(
        "--review-artifact",
        nargs="?",
        const=True,
        default=None,
        metavar="POINTER",
        help=(
            "Declare that this update carries a concrete artifact awaiting "
            "the owner's review. Lets a genuine 'to review' pass through the "
            "startup-hydration demotion and land in the purple To Review "
            "lane (do NOT escalate to 'needs review' for this — that status "
            "paints the red urgent lane). Pass the review note as the value: "
            "a Markdown note inside the switchboard Obsidian vault (repo-relative or "
            "absolute path, or obsidian://open?vault=switchboard&file=<path>), or a "
            "Google Doc / hosted page URL when the owner explicitly asked for that "
            "surface. A .md that does not exist or lives outside the vault is "
            "rejected (review-surface gate). A note that so far "
            "exists only in the calling task's worktree is accepted "
            "(--task-id, $SWITCHBOARD_WORKER_TASK_ID, $SWITCHBOARD_WORKTREE, or the cwd's "
            "worktree)."
        ),
    )
    parser.add_argument(
        "--task-id",
        default=os.environ.get("SWITCHBOARD_WORKER_TASK_ID") or None,
        help="Reporting task whose worktree may hold the review note before its branch lands (default: $SWITCHBOARD_WORKER_TASK_ID)",
    )
    parser.add_argument("--todo-update", action="append", default=[], help="id|status|when(optional)")
    parser.add_argument("--linked-todos", choices=["open", "waiting", "scheduled", "someday", "done"])
    parser.add_argument("--linked-review-on")
    parser.add_argument("--linked-snooze-until")
    parser.add_argument("--clear-manual-open", action="store_true", help="Clear the temporary explicit-launch keep-open marker before close logic runs")
    parser.add_argument("--close-workspace", action="store_true", help="Explicitly close the workstream tab after this update")
    parser.add_argument("--only-if-current-update-id", help="Emit only if the source's current update still has this opaque ID")
    parser.add_argument("--only-if-current-timestamp", help="Legacy guard for updates created before update IDs were added")
    parser.add_argument("--only-if-current-status", help="Emit only if the source's current update still has this status")
    parser.add_argument("--skip-auto-close", action="store_true", help="Publish the update without auto-closing a waiting workstream")
    parser.add_argument("--skip-refresh", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    state_dir = repo_root / "system" / "state"
    source_dir = state_dir / "sources"
    state_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)

    # Review-surface gate: a review deliverable for the owner must be a
    # Markdown note inside the switchboard Obsidian vault (normalised to an obsidian://
    # URI) or, when explicitly requested, an approved live URL. Nonexistent or
    # out-of-vault Markdown fails closed BEFORE any state write so a dead pointer
    # never reaches workspace-updates.json, the review lane, or the digest.
    review_pointer = args.review_artifact if isinstance(args.review_artifact, str) else None
    verdict = review_surface.evaluate(
        [args.summary, args.recommended_next_action or "", *args.new_action],
        status=args.status,
        pointer=review_pointer,
        review_flag=args.review_artifact is True,
        task_id=args.task_id,
    )
    if not verdict.ok:
        append_event_line(
            state_dir,
            {
                "type": "review_surface_rejected",
                "timestamp": now_iso(),
                "source": args.source,
                "boundary": "emit-workspace-update",
                "status": args.status,
                "reason": verdict.reason,
                "offending": verdict.offending,
            },
        )
        print(f"[emit-workspace-update] BLOCKED ({verdict.reason}) for {args.source}: {verdict.guidance}", file=sys.stderr)
        sys.exit(2)

    # Startup-hydration demotion: a plain "to review" emitted during a
    # machine-seeded startup turn is startup noise, not an the owner-facing
    # escalation. Record the requested status for provenance, publish "done".
    effective_status = args.status
    requested_status = None
    hydration_turn = current_turn_is_startup_hydration()
    if (
        hydration_turn
        and not args.review_artifact
        and str(args.status or "").strip().lower() in HYDRATION_DEMOTED_STATUSES
    ):
        requested_status = args.status
        effective_status = "done"
        print(
            f"[emit-workspace-update] startup-hydration turn: demoted status "
            f"{args.status!r} -> 'done' for {args.source}. If this update "
            "carries a concrete artifact awaiting the owner's review, re-emit "
            "with --status 'to review' --review-artifact (purple To Review "
            "lane). Reserve blocked / needs action / needs review for "
            "urgent escalations — they paint the red urgent lane.",
            file=sys.stderr,
        )

    event = {
        # Typed like every other events.jsonl fact (queue_refreshed,
        # workspace_autoclosed, notification) so consumers can filter without
        # relying on "no type means workspace update".
        "type": "workspace_update",
        "update_id": str(uuid4()),
        "timestamp": now_iso(),
        "source": args.source,
        "workspace": args.workspace,
        "status": effective_status,
        "summary": args.summary,
        "recommended_next_action": args.recommended_next_action,
        "new_actions": [parse_new_action(raw) for raw in args.new_action],
        "todo_updates": [parse_todo_update(raw) for raw in args.todo_update],
        "linked_todos": args.linked_todos,
        "linked_review_on": args.linked_review_on,
        "linked_snooze_until": args.linked_snooze_until,
    }
    if hydration_turn:
        # Durable provenance so consumers (publish-workspace-status.sh,
        # audits) can distinguish startup-seeded updates from the owner-routed
        # work without re-deriving marker state.
        event["provenance"] = "startup_hydration"
    if requested_status is not None:
        event["requested_status"] = requested_status
    if args.review_artifact:
        # Lets review-lane.ts treat a hydration-turn "to review" as a genuine
        # escape-hatch signal (pin the purple To Review lane).
        event["review_artifact"] = True
    if verdict.review_url:
        # Keep the URI as structured data; render its path and standalone link
        # separately so shortening prose cannot damage the destination.
        event["review_url"] = verdict.review_url
        event["summary"] = review_surface.report_text(args.summary, review_url=verdict.review_url)

    # Resolve before state refresh removes a completed workstream from active
    # queue surfaces. The ref remains stable through the close operation.
    workspace_ref_to_close = resolve_workspace_ref(repo_root, args.source) if args.close_workspace else ""

    updates_path = source_dir / "workspace-updates.json"
    updates_lock_path = updates_path.with_name(f".{updates_path.name}.lock")
    with with_file_lock(updates_lock_path):
        if updates_path.exists():
            try:
                payload = json.loads(updates_path.read_text())
            except json.JSONDecodeError:
                payload = {"updated_at": None, "updates": {}}
        else:
            payload = {"updated_at": None, "updates": {}}

        # Agent-end lifecycle reconciliation uses this compare-and-set guard.
        # If the agent emitted a newer status during the turn, preserve it
        # rather than overwriting it with the fallback waiting state.
        if not current_update_matches(
            payload,
            args.source,
            args.only_if_current_update_id,
            args.only_if_current_timestamp,
            args.only_if_current_status,
        ):
            # Intended no-op (a newer update won the compare-and-set), but say
            # so: a silently skipped emit is indistinguishable from a lost one.
            print(
                f"[emit-workspace-update] skipped: {args.source} already has a newer update than the guard expected",
                file=sys.stderr,
            )
            return

        append_event_line(state_dir, event)

        payload.setdefault("updates", {})[args.source] = event
        payload["updated_at"] = now_iso()
        write_json_file(updates_path, payload)

    apply_todo_updates(repo_root, args)

    mark_scheduled_jobs_for_workstream(repo_root, args.source, args.status)

    if args.clear_manual_open and args.close_workspace and args.source.startswith("workstream:"):
        clear_manual_open_workstream(repo_root, args.source.split(":", 1)[1])

    refresh_failed = False
    if not args.skip_refresh:
        # The update itself is already durable (events.jsonl + source entry).
        # Refresh failure is a warning, not failure of the saved update. A caller
        # must still be able to deliver its callback when the todo gate is down.
        rebuild = subprocess.run(
            [
                sys.executable,
                str(repo_root / "system" / "scripts" / "rebuild-switchboard-state.py"),
                "--write",
                "--event-source",
                args.source,
            ],
            check=False,
        )
        if rebuild.returncode != 0:
            refresh_failed = True
            print(
                f"[emit-workspace-update] WARNING: rebuild-switchboard-state.py exited {rebuild.returncode}; "
                "the update was saved but queue.json/now.json may be stale "
                "(repair: python3 system/scripts/rebuild-switchboard-state.py --write)",
                file=sys.stderr,
            )
        publish = subprocess.run(
            # Through bash: Windows cannot execute a .sh file directly (WinError 193).
            ["bash", str(repo_root / "system" / "scripts" / "publish-switchboard-status.sh")],
            check=False,
        )
        if publish.returncode != 0:
            print(
                f"[emit-workspace-update] WARNING: publish-switchboard-status.sh exited {publish.returncode}; "
                "the sidebar tile may be stale (state files are up to date)",
                file=sys.stderr,
            )

    # Close logic runs regardless of --skip-refresh: an explicit
    # --close-workspace is a lifecycle contract ("a state update without a
    # verified close is incomplete"), not a refresh side effect.
    # --skip-auto-close only suppresses the implicit waiting-status close; it
    # must never cancel an explicit --close-workspace.
    if args.close_workspace and args.skip_auto_close:
        print("[emit-workspace-update] --close-workspace overrides --skip-auto-close", file=sys.stderr)
    if args.close_workspace or not args.skip_auto_close:
        closed = maybe_close_waiting_workspace(
            repo_root,
            args.source,
            args.status,
            close_workspace=args.close_workspace,
            workspace_ref=workspace_ref_to_close,
        )
        if args.close_workspace and not closed:
            raise RuntimeError(f"Failed to close the workspace for {args.source}")

    if refresh_failed:
        print(json.dumps({"source": args.source, "saved": True, "refresh_failed": True}))


if __name__ == "__main__":
    main()
