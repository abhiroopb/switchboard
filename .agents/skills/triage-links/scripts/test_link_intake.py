import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = pathlib.Path(__file__).with_name('link_intake.py')


def load_module():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    spec = importlib.util.spec_from_file_location('link_intake', SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError('Could not load link_intake.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


link_intake = load_module()


class LinkIntakeTests(unittest.TestCase):
    def test_extract_urls_preserves_order(self):
        text = 'See https://example.com and https://github.com/foo/bar/pull/1 and https://example.com again'
        self.assertEqual(
            link_intake.extract_urls(text),
            ['https://example.com', 'https://github.com/foo/bar/pull/1'],
        )

    def test_normalize_slack_permalink(self):
        normalized = link_intake.normalize_url('https://example.slack.com/archives/CXXXXXXXXXX/p1700000000000001')
        self.assertEqual(normalized.system, 'slack')
        self.assertEqual(normalized.entity_type, 'thread-message')
        self.assertEqual(normalized.entity_id, 'CXXXXXXXXXX:1700000000000001')

    def test_normalize_google_doc(self):
        normalized = link_intake.normalize_url('https://docs.google.com/document/d/abc123/edit')
        self.assertEqual(normalized.system, 'gdrive')
        self.assertEqual(normalized.entity_type, 'google-doc')
        self.assertEqual(normalized.read_path, 'skill:gdrive')

    def test_infer_validate_mode(self):
        mode = link_intake.infer_task_mode('Check if this help-center content is accurate and update it if needed')
        self.assertEqual(mode, 'validate')

    def test_web_validation_starts_read_only_regardless_of_browser_discovery(self):
        primary = link_intake.normalize_url('https://docs.example.com/help/article')
        for status in ('active', 'local', 'archived', 'missing', None):
            with self.subTest(status=status):
                capabilities = {} if status is None else {
                    'agent-browser': link_intake.Capability('agent-browser', 'skill', status),
                }
                surface = link_intake.select_execution_surface(primary, 'validate', capabilities)
                self.assertEqual(surface['best_surface'], 'web-review')
                self.assertEqual(surface['best_tool_path'], 'read_web_page')

    def test_build_plan_emits_two_actions_for_slack_validate(self):
        with mock.patch.object(
            link_intake,
            'discover_capabilities',
            return_value={'slack': link_intake.Capability('slack', 'skill', 'active')},
        ):
            plan = link_intake.build_plan(
                'Review this thread and validate the help-center page https://example.slack.com/archives/CXXXXXXXXXX/p1700000000000001'
            )
        self.assertEqual(plan['task_mode'], 'validate')
        self.assertEqual(plan['actions'][0]['id'], 'A1')
        self.assertEqual(plan['actions'][1]['id'], 'A2')
        self.assertIn('approve all', plan['approval_commands'])

    def test_build_plan_promotes_discovered_web_link_as_execution_target(self):
        with mock.patch.object(
            link_intake,
            'discover_capabilities',
            return_value={'agent-browser': link_intake.Capability('agent-browser', 'skill', 'archived')},
        ):
            plan = link_intake.build_plan(
                'Review this thread and validate the help-center page https://example.slack.com/archives/CXXXXXXXXXX/p1700000000000001',
                discovered_urls=['https://docs.example.com/help/article'],
            )
        self.assertEqual(plan['primary']['system'], 'slack')
        self.assertEqual(plan['execution_target']['system'], 'web')
        self.assertEqual(plan['execution_surface']['best_surface'], 'web-review')
        self.assertEqual(plan['actions'][0]['target'], 'https://docs.example.com/help/article')
        self.assertEqual(plan['actions'][1]['target'], 'https://example.slack.com/archives/CXXXXXXXXXX/p1700000000000001')

    def test_slack_one_pager_request_targets_workstream_draft_not_reply(self):
        with mock.patch.object(
            link_intake,
            'discover_capabilities',
            return_value={'slack': link_intake.Capability('slack', 'skill', 'active')},
        ):
            plan = link_intake.build_plan(
                'Can you get us a draft 1 pager summary of all onboarding projects by tomorrow? https://example.slack.com/archives/CXXXXXXXXXX/p1700000000000001'
            )
        self.assertEqual(plan['task_mode'], 'draft-artifact')
        self.assertEqual(plan['execution_surface']['best_surface'], 'workstream-draft')
        self.assertEqual(plan['actions'][0]['type'], 'prepare-draft')
        self.assertNotEqual(plan['actions'][0]['type'], 'post-reply')
        self.assertIsNone(plan['brief']['reply_draft'])

    def test_build_plan_renders_richer_execution_brief(self):
        with mock.patch.object(
            link_intake,
            'discover_capabilities',
            return_value={'agent-browser': link_intake.Capability('agent-browser', 'skill', 'archived')},
        ):
            plan = link_intake.build_plan(
                'Review this thread and validate the help-center page https://example.slack.com/archives/CXXXXXXXXXX/p1700000000000001',
                discovered_urls=['https://docs.example.com/help/article'],
                evidence_notes=['The help-center page still uses the old eligibility wording.'],
                affected_sections=['Eligibility', 'Support guidance'],
                planned_edits=['Replace the old eligibility sentence with the current policy wording.'],
                reply_bullets=['Checked the help-center page against the latest policy.', 'Prepared the exact wording update for approval.'],
            )
        self.assertIn('old eligibility wording', plan['brief']['evidence_checked'][0])
        self.assertEqual(plan['brief']['affected_sections'][0], 'Eligibility')
        self.assertIn('Replace the old eligibility sentence', plan['brief']['planned_edits'][0])
        self.assertIn('Prepared the exact wording update', plan['brief']['reply_draft'])

    def test_load_investigation_bundle_from_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle_path = pathlib.Path(temp_dir) / 'bundle.json'
            bundle_path.write_text(
                '{"discovered_urls":["https://docs.example.com/help/article"],"evidence_notes":["Found stale copy."],"affected_sections":["Eligibility"],"planned_edits":["Replace stale sentence."],"reply_bullets":["Checked the help-center page."],"reply_draft":"Draft reply body"}'
            )
            bundle = link_intake.load_investigation_bundle(str(bundle_path))
        self.assertEqual(bundle.discovered_urls[0], 'https://docs.example.com/help/article')
        self.assertEqual(bundle.reply_draft, 'Draft reply body')

    def test_load_investigation_bundle_from_markdown(self):
        markdown = """# Discovered links
- https://docs.example.com/help/article

# Evidence checked
- Found stale copy.

# Affected sections
- Eligibility

# Planned edits
- Replace stale sentence.

# Reply bullets
- Checked the help-center page.

# Draft reply
Thread reply draft
"""
        bundle = link_intake.parse_markdown_bundle(markdown)
        self.assertEqual(bundle.discovered_urls[0], 'https://docs.example.com/help/article')
        self.assertEqual(bundle.affected_sections[0], 'Eligibility')
        self.assertIn('Thread reply draft', bundle.reply_draft)


if __name__ == '__main__':
    unittest.main()
