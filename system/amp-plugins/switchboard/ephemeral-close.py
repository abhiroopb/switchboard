#!/usr/bin/env python3
"""Close only this completed supplemental worker or acknowledged Router 2+ turn."""
import argparse
from datetime import datetime
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "system" / "scripts"))
    import flock_compat as fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[3]
CMUX = os.environ.get('CMUX_BIN') or os.environ.get('CMUX_AMP_CMUX_BIN') or '/Applications/cmux.app/Contents/Resources/bin/cmux'


def stamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError, TypeError):
        return 0


def candidate(tasks, receipts, thread, workspace, worker, since):
    # Every task executing here must be settled; a reused numeric ref is not
    # authority. The caller also verifies UUID + surface + thread + turn.
    own = [t for t in tasks if (t.get('executor') or {}).get('thread_id') == thread
           or (t.get('executor') or {}).get('workspace_ref') == workspace]
    if any(t.get('status') not in ('done', 'cancelled') for t in own):
        return None
    if worker:
        matches = []
        for task in own:
            if task.get('status') != 'done' or stamp(task.get('completed_at')) < since:
                continue
            if task.get('external_write_outcome') == 'unknown' or task.get('report_delivery_unknown'):
                continue
            if not task.get('summary') or not task.get('completed_at'):
                continue
            executor = task.get('executor') or {}
            if executor.get('thread_id') != thread or executor.get('workspace_ref') != workspace:
                continue
            for receipt in receipts:
                if (receipt.get('worker_id') == worker and receipt.get('task_id') == task.get('id')
                        and receipt.get('attempt_id') == task.get('attempt_id')
                        and receipt.get('thread_id') == thread and receipt.get('workspace_ref') == workspace
                        and receipt.get('owner_workspace_ref') not in (None, workspace)
                        and receipt.get('owner_thread_id') not in (None, thread)
                        and receipt.get('workspace_id')
                        and receipt.get('acknowledged_at')
                        and not receipt.get('launch_outcome_unknown')):
                    matches.append(task)
        return matches[0] if len(matches) == 1 else None
    for task in tasks:
        if task.get('router_workspace_ref') != workspace or task.get('status') in ('done', 'cancelled'):
            continue
        executor = task.get('executor') or {}
        if (not task.get('received_at') or executor.get('role') != 'worker'
                or executor.get('thread_id') in (None, thread)
                or executor.get('workspace_ref') in (None, workspace)):
            return None
    routers = [t for t in tasks if t.get('router_thread_id') == thread
               and t.get('router_workspace_ref') == workspace and stamp(t.get('received_at')) >= since]
    if not routers or any(not re.fullmatch(r'Router (?:[2-9]|[1-9][0-9]+)', t.get('router', '')) for t in routers):
        return None
    for task in routers:
        executor = task.get('executor') or {}
        if (task.get('external_write_outcome') == 'unknown' or task.get('report_delivery_unknown')
                or executor.get('role') != 'worker' or executor.get('thread_id') in (None, thread)
                or not executor.get('workspace_ref') or executor.get('workspace_ref') == workspace
                or stamp(task.get('received_at')) < since):
            return None
    return routers[-1]


def close_self(args, probe, load, hook, close, sentinel):
    tasks, receipts = load()
    proof = probe.inspect(args.workspace)
    if not (proof.get('available') is True and proof.get('local') is True
            and proof.get('workspace_id') == args.workspace_id
            and proof.get('surface_id') == args.surface_id and proof.get('thread_id') == args.thread):
        return {'closed': False, 'reason': 'not-idle-or-identity-changed'}
    record = hook()
    if ((record.get('activePromptTurnId') or record.get('lastPromptTurnId')) != args.turn
            or record.get('workspaceId') != args.workspace_id or record.get('surfaceId') != args.surface_id):
        return {'closed': False, 'reason': 'turn-changed'}
    task = candidate(tasks, receipts, args.thread, args.workspace, args.worker, args.since)
    if task is None:
        return {'closed': False, 'reason': 'task-not-settled-or-handoff-not-acknowledged'}
    if args.worker:
        receipt = next(r for r in receipts if r.get('worker_id') == args.worker
                       and r.get('attempt_id') == task.get('attempt_id'))
        if receipt['workspace_id'] != args.workspace_id:
            return {'closed': False, 'reason': 'receipt-workspace-changed'}
    elif proof.get('title') != task['router']:
        return {'closed': False, 'reason': 'not-router-workspace'}
    # Do not consume a changed ledger/receipt or close across a new user turn.
    if load() != (tasks, receipts) or hook() != record:
        return {'closed': False, 'reason': 'state-changed'}
    sentinel(args.workspace_id)
    return {'closed': close(args.workspace_id), 'task_id': task['id'], 'thread': args.thread}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('workspace', 'workspace-id', 'surface-id', 'thread', 'turn'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--worker', default='')
    parser.add_argument('--since', type=float, required=True)
    args = parser.parse_args()
    # Use the same lock as Router dispatch. Never steal or repair its lock.
    lock = Path(os.environ.get('ROUTER_LOCK_DIR', os.environ.get('TMPDIR', '/tmp/').rstrip('/') + '/switchboard-router-dispatch.lock'))
    try:
        lock.mkdir()
    except FileExistsError:
        print(json.dumps({'closed': False, 'reason': 'router-dispatch-in-progress'}))
        return
    (lock / 'pid').write_text(str(os.getpid()))
    try:
        spec = importlib.util.spec_from_file_location('owner_probe', ROOT / 'system/scripts/local_owner_probe.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def load():
            tasks = json.loads((ROOT / 'system/state/running-tasks.json').read_text())['tasks']
            receipts = [json.loads(p.read_text()) for p in sorted((ROOT / 'system/state/worker-dispatch').glob('*.json'))]
            return tasks, receipts

        def hook():
            data = json.loads((Path.home() / '.cmuxterm/amp-hook-sessions.json').read_text())
            return data['sessions'][args.thread]

        def sentinel(workspace):
            path = ROOT / '.state/cmux-skip-record'
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write(workspace + '\n')

        def close(workspace):
            subprocess.run([CMUX, 'workspace-action', '--action', 'unpin', '--workspace', workspace], check=True, capture_output=True, timeout=10)
            # --force: cmux 0.65 refuses a workspace with a running process (the Amp CLI) without it.
            subprocess.run([CMUX, 'close-workspace', '--workspace', workspace, '--force'], check=True, capture_output=True, timeout=10)
            return module.Probe().session(args.workspace).get('workspace_present') is False

        print(json.dumps(close_self(args, module.Probe(), load, hook, close, sentinel)))
    except Exception as error:
        # No retry after uncertain close transport. Never fall back to another tab.
        print(json.dumps({'closed': False, 'reason': type(error).__name__}))
    finally:
        (lock / 'pid').unlink()
        lock.rmdir()


if __name__ == '__main__':
    main()
