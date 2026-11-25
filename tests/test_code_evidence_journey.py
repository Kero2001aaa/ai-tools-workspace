"""Actual repository-source quote selection across checkpoint and restored plan.

No generated answer, no persisted evidence/plan execution provenance claim.
"""
import hashlib
from pathlib import Path
import tempfile
import unittest
from agent.code_evidence import read_code_excerpt
from agent.plan_store import PlanStore
from agent.plan_recovery import copy_database


class CodeEvidenceJourneyTests(unittest.TestCase):
    def test_explicit_source_exact_line_quote_then_checkpoint_reconstruction(self):
        repository = Path(__file__).resolve().parents[1]
        approved = ['agent/mcp_server.py']
        source = read_code_excerpt(repository, approved, approved[0], max_lines=100)
        # Known test question: which function returns supplied text statistics?
        lines = source['text'].splitlines(keepends=True)
        index = next(number for number, line in enumerate(lines, 1) if line.startswith('def text_stats('))
        quote = read_code_excerpt(repository, approved, approved[0], start_line=index, max_lines=1)
        self.assertEqual(lines[index-1], quote['text'])
        self.assertEqual((index, index), (quote['start_line'], quote['end_line']))
        self.assertEqual(source['file_sha256'], quote['file_sha256'])
        self.assertEqual(hashlib.sha256(quote['text'].encode()).hexdigest(), quote['excerpt_sha256'])
        self.assertIn('def text_stats(text:', quote['text'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PlanStore(root / 'source.sqlite3')
            plan = store.create_plan('Local read context, not research execution', [{'task_type': 'workspace_status'}, {'task_type': 'workspace_status'}])
            first = store.run_next_step(plan['id'])
            checkpoint = store.get_plan(plan['id'])
            copy_database(store.db_path, root / 'restored.sqlite3')
            restored = PlanStore(root / 'restored.sqlite3')
            # Caller has to resupply approved path/range. Backup stores no source.
            reconstructed = read_code_excerpt(repository, approved, approved[0], start_line=index, max_lines=1)
            self.assertEqual(quote, reconstructed)
            self.assertNotIn(quote['text'], str(restored.get_plan(plan['id'])))
            restored.run_until_done(plan['id'])
            self.assertEqual(checkpoint['steps'][0], restored.get_plan(plan['id'])['steps'][0])
            self.assertEqual(store.queue.get(first['task_id']), restored.queue.get(first['task_id']))
            self.assertEqual(checkpoint, store.get_plan(plan['id']))

    def test_changed_source_cannot_preserve_old_file_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);file = root / 'source.py'
            file.write_text('def original():\n    pass\n')
            original = read_code_excerpt(root, ['source.py'], 'source.py')
            file.write_text('def altered():\n    pass\n')
            changed = read_code_excerpt(root, ['source.py'], 'source.py')
            self.assertNotEqual(original['file_sha256'], changed['file_sha256'])
            self.assertNotEqual(original['excerpt_sha256'], changed['excerpt_sha256'])
            self.assertNotIn('original', changed['text'])
