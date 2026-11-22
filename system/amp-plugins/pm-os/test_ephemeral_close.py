import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('closer', Path(__file__).with_name('ephemeral-close.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CloseTest(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(thread='T-worker', workspace='workspace:9', workspace_id='uuid-9',
                                    surface_id='surface-uuid', worker='worker-9', turn='turn-2', since=10)
        self.task = dict(id='task-9', attempt_id='a2', status='done', completed_at='1970-01-01T00:00:12Z',
                         summary='Result saved', executor={'thread_id': 'T-worker', 'workspace_ref': 'workspace:9', 'role': 'worker'})
        self.receipt = dict(task_id='task-9', attempt_id='a2', worker_id='worker-9', thread_id='T-worker',
                            workspace_ref='workspace:9', workspace_id='uuid-9', owner_workspace_ref='workspace:1',
                            owner_thread_id='T-owner', acknowledged_at='1970-01-01T00:00:11Z')
        self.proof = dict(available=True, local=True, workspace_id='uuid-9', surface_id='surface-uuid', thread_id='T-worker')
        self.hook = dict(lastPromptTurnId='turn-2', workspaceId='uuid-9', surfaceId='surface-uuid')
        self.actions = []

    def run_close(self, load=None, hook=None):
        return module.close_self(self.args, SimpleNamespace(inspect=lambda _: self.proof),
                                 load or (lambda: ([self.task], [self.receipt])), hook or (lambda: self.hook),
                                 lambda uuid: self.actions.append(('close', uuid)) or True,
                                 lambda uuid: self.actions.append(('sentinel', uuid)))

    def test_done_worker_closes_only_its_uuid_after_sentinel(self):
        self.assertTrue(self.run_close()['closed'])
        self.assertEqual(self.actions, [('sentinel', 'uuid-9'), ('close', 'uuid-9')])

    def test_preserves_blocked_unknown_outcome_old_attempt_and_owner(self):
        cases = [('task', 'status', 'blocked'), ('task', 'status', 'unexpected'),
                 ('task', 'report_delivery_unknown', True), ('task', 'external_write_outcome', 'unknown'),
                 ('task', 'completed_at', '1970-01-01T00:00:09Z'),
                 ('receipt', 'attempt_id', 'a1'), ('receipt', 'launch_outcome_unknown', True),
                 ('receipt', 'owner_workspace_ref', 'workspace:9'), ('receipt', 'owner_thread_id', 'T-worker'),
                 ('receipt', 'workspace_id', 'reused-uuid')]
        for target, key, value in cases:
            with self.subTest(target=target, key=key):
                self.setUp()
                getattr(self, target)[key] = value
                self.assertFalse(self.run_close()['closed'])
                self.assertEqual(self.actions, [])

    def test_draft_approval_identity_and_new_turn_never_close(self):
        for field, value in [('available', False), ('local', False), ('workspace_id', 'other'),
                             ('thread_id', 'T-other'), ('surface_id', 'other')]:
            with self.subTest(field=field):
                self.setUp()
                self.proof[field] = value
                self.assertFalse(self.run_close()['closed'])
                self.assertEqual(self.actions, [])
        self.setUp()
        self.hook['activePromptTurnId'] = 'turn-3'
        self.assertFalse(self.run_close()['closed'])
        self.assertEqual(self.actions, [])

    def test_ledger_or_hook_changed_during_check_never_close(self):
        for changed in ('ledger', 'hook'):
            self.setUp()
            reads = iter([([self.task], [self.receipt]), ([], [])])
            hooks = iter([self.hook, {**self.hook, 'lastPromptTurnId': 'turn-3'}])
            result = self.run_close(load=(lambda: next(reads)) if changed == 'ledger' else None,
                                    hook=(lambda: next(hooks)) if changed == 'hook' else None)
            self.assertFalse(result['closed'])
            self.assertEqual(self.actions, [])

    def test_router_two_closes_after_worker_ack_but_not_router_one(self):
        self.args.worker = ''
        self.task.update(router='Router 2', router_thread_id='T-worker', router_workspace_ref='workspace:9',
                         received_at='1970-01-01T00:00:11Z', status='active',
                         executor={'role': 'worker', 'thread_id': 'T-child', 'workspace_ref': 'workspace:12'})
        self.proof['title'] = 'Router 2'
        self.assertTrue(self.run_close()['closed'])
        self.actions.clear()
        pending = dict(id='pending', status='routing', router_workspace_ref='workspace:9')
        self.assertFalse(self.run_close(load=lambda: ([self.task, pending], [self.receipt]))['closed'])
        self.assertEqual(self.actions, [])
        for changes in ({'router': 'Router 1'}, {'received_at': None}, {'router_thread_id': 'T-old'},
                        {'executor': {'role': 'router', 'thread_id': 'T-child', 'workspace_ref': 'workspace:12'}}):
            before = copy.deepcopy(self.task)
            self.task.update(changes)
            self.actions.clear()
            self.assertFalse(self.run_close()['closed'])
            self.assertEqual(self.actions, [])
            self.task = before


if __name__ == '__main__':
    unittest.main()
