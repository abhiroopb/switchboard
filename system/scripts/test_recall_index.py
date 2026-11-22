import importlib.util
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCRIPT_PATH = pathlib.Path(__file__).with_name('query-recall-index.py')


def load_module():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    spec = importlib.util.spec_from_file_location('query_recall_index', SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError('Could not load query-recall-index.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


query_recall_index = load_module()


class RecallIndexTests(unittest.TestCase):
    def test_build_query_candidates_prefers_phrase_then_and_then_or(self):
        candidates = query_recall_index.build_query_candidates('workflow-system-upgrade')
        self.assertEqual(candidates[0], '"workflow system upgrade"')
        self.assertEqual(candidates[1], '"workflow" AND "system" AND "upgrade"')
        self.assertEqual(candidates[2], '"workflow" OR "system" OR "upgrade"')

    def test_query_index_matches_hyphenated_terms_without_broad_noise(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = pathlib.Path(temp_dir) / 'recall.sqlite3'
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE VIRTUAL TABLE recall_documents USING fts5(path, title, content, tokenize='porter unicode61')"
            )
            conn.execute(
                'INSERT INTO recall_documents(path, title, content) VALUES (?, ?, ?)',
                ('workstreams/workflow-system-upgrade/spec.md', 'Workflow System Upgrade', 'workflow system upgrade cutover build')
            )
            conn.execute(
                'INSERT INTO recall_documents(path, title, content) VALUES (?, ?, ?)',
                ('knowledge/app-releases.md', 'App Releases', 'release train and ship cadence')
            )
            conn.commit()
            conn.close()

            conn = query_recall_index.open_db(db_path)

            rows = query_recall_index.query_index(conn, 'workflow-system-upgrade', 5)
            conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['path'], 'workstreams/workflow-system-upgrade/spec.md')

    def test_missing_index_does_not_create_parent_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = pathlib.Path(temp_dir) / 'missing' / 'recall.sqlite3'
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), 'anything', '--db', str(db_path)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn('import-memory-corpus.py --force', result.stderr)
            self.assertFalse(db_path.parent.exists())

    def test_bm25_ranks_more_relevant_asymmetric_input_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = pathlib.Path(temp_dir) / 'recall.sqlite3'
            conn = sqlite3.connect(db_path)
            conn.execute('CREATE VIRTUAL TABLE recall_documents USING fts5(path, title, content)')
            conn.executemany(
                'INSERT INTO recall_documents(path, title, content) VALUES (?, ?, ?)',
                [
                    ('z-noisy.md', 'Noise', 'alpha ' + 'noise ' * 200 + 'beta'),
                    ('a-focused.md', 'Focused', 'alpha beta alpha beta'),
                ],
            )
            conn.commit()
            conn.close()
            ro = query_recall_index.open_db(db_path)
            rows = query_recall_index.query_index(ro, 'alpha beta', 5)
            ro.close()
        self.assertEqual(rows[0]['path'], 'a-focused.md')

    def test_phrase_results_backfill_all_terms_without_or_noise_or_duplicates(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE VIRTUAL TABLE recall_documents USING fts5(path, title, content)')
        conn.executemany(
            'INSERT INTO recall_documents VALUES (?, ?, ?)',
            [('phrase', 'Exact', 'alpha beta'), ('and', 'Related', 'alpha middle beta'),
             ('or', 'Noise', 'alpha alone')],
        )
        rows = query_recall_index.query_index(conn, 'alpha beta', 3)
        self.assertEqual([row['path'] for row in rows], ['phrase', 'and'])
        fallback = query_recall_index.query_index(conn, 'missing beta', 3)
        self.assertEqual({row['path'] for row in fallback}, {'phrase', 'and'})
        self.assertEqual(query_recall_index.query_index(conn, 'alpha beta', 1)[0]['path'], 'phrase')
        self.assertEqual(query_recall_index.query_index(conn, 'alpha beta', 0), [])
        self.assertEqual(query_recall_index.query_index(conn, 'alpha beta', -1), [])


if __name__ == '__main__':
    unittest.main()
