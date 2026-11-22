import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


SCRIPT_PATH = pathlib.Path(__file__).with_name('write_investigation_bundle.py')


def load_module():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    spec = importlib.util.spec_from_file_location('write_investigation_bundle', SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError('Could not load write_investigation_bundle.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


write_investigation_bundle = load_module()


class WriteInvestigationBundleTests(unittest.TestCase):
    def test_write_bundle_creates_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / 'bundle.json'
            exit_code = write_investigation_bundle.main([
                '--output',
                str(output),
                '--discovered-link',
                'https://docs.example.com/help/article',
                '--evidence-note',
                'Found stale copy.',
                '--affected-section',
                'Eligibility',
                '--planned-edit',
                'Replace stale sentence.',
                '--reply-bullet',
                'Checked the help-center page.',
            ])
            payload = json.loads(output.read_text())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload['discovered_urls'][0], 'https://docs.example.com/help/article')
        self.assertEqual(payload['affected_sections'][0], 'Eligibility')

    def test_seed_file_merges_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            seed = pathlib.Path(temp_dir) / 'seed.json'
            seed.write_text(json.dumps({
                'discovered_urls': ['https://docs.example.com/help/article'],
                'evidence_notes': ['Found stale copy.'],
                'affected_sections': ['Eligibility'],
                'planned_edits': ['Replace stale sentence.'],
                'reply_bullets': ['Checked the help-center page.'],
            }))
            output = pathlib.Path(temp_dir) / 'bundle.json'
            write_investigation_bundle.main([
                '--output',
                str(output),
                '--seed-file',
                str(seed),
                '--discovered-link',
                'https://docs.example.com/help/article',
                '--affected-section',
                'Support guidance',
            ])
            payload = json.loads(output.read_text())
        self.assertEqual(payload['discovered_urls'], ['https://docs.example.com/help/article'])
        self.assertEqual(payload['affected_sections'], ['Eligibility', 'Support guidance'])


if __name__ == '__main__':
    unittest.main()
