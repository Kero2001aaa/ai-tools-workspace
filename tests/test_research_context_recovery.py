"""Offline report reconstruction over actual restored plans, no evidence persistence."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from agent.plan_store import PlanStore
from agent.plan_recovery import copy_database
from agent.research_context import plan_evidence_envelope


class ResearchRecoveryTests(unittest.TestCase):
    def test_checkpoint_restores_context_and_no_completed_step_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PlanStore(root / 'source.sqlite3')
            plan = store.create_plan('Synthetic offline plan', [{'task_type': 'workspace_status'}, {'task_type': 'workspace_status'}])
            first = store.run_next_step(plan['id'])
            before = store.get_plan(plan['id'])
            source = {'title': 'Synthetic', 'url': 'https://example.com', 'snippet': 'Exact retained excerpt',
                'source': 'Exa', 'truncated': False, 'evidence_sha256': hashlib.sha256(b'Exact retained excerpt').hexdigest()}
            args = [plan['id'], plan['steps'][1]['id'], 'Synthetic question', [source], [{'source_index': 0, 'quote': 'Exact retained excerpt'}]]
            old = plan_evidence_envelope(store.db_path, *args)
            recovered_path = root / 'restored.sqlite3'
            copy_database(store.db_path, recovered_path)
            snapshot = recovered_path.read_bytes()
            recovered = plan_evidence_envelope(recovered_path, *args)
            self.assertEqual(old, recovered)
            self.assertEqual(snapshot, recovered_path.read_bytes())
            with sqlite3.connect(recovered_path) as db:
                self.assertEqual({'plans', 'plan_steps', 'tasks', 'task_attempts'}, {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")})
            restored = PlanStore(recovered_path)
            restored.run_until_done(plan['id'])
            self.assertEqual(before['steps'][0], restored.get_plan(plan['id'])['steps'][0])
            self.assertEqual(store.queue.get(first['task_id']), restored.queue.get(first['task_id']))
            after = plan_evidence_envelope(recovered_path, *args)
            self.assertEqual('succeeded', after['step_status_snapshot'])
            self.assertEqual(old['report'], after['report'])
            self.assertFalse(after['execution_provenance_verified'])
            self.assertFalse(after['persisted'])
            self.assertEqual(before, store.get_plan(plan['id']))
            # The caller has to resupply excerpt bytes; they are not in plan backup.
            self.assertNotIn('Exact retained excerpt', json.dumps(restored.get_plan(plan['id'])))

    def test_live_wal_committed_plan_visible_and_recovery_reconstructs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PlanStore(root / 'wal-source.sqlite3')
            with sqlite3.connect(store.db_path) as live:
                self.assertEqual('wal', live.execute('PRAGMA journal_mode=WAL').fetchone()[0])
                live.execute('PRAGMA wal_autocheckpoint=0')
                plan = store.create_plan('WAL source', [{'task_type': 'workspace_status'}])
                source = {'title': 'WAL source', 'url': 'https://example.com', 'snippet': 'Committed WAL excerpt',
                    'source': 'Exa', 'truncated': False, 'evidence_sha256': hashlib.sha256(b'Committed WAL excerpt').hexdigest()}
                args = [plan['id'], plan['steps'][0]['id'], 'Question', [source], [{'source_index': 0, 'quote': 'Committed WAL excerpt'}]]
                self.assertTrue(Path(str(store.db_path) + '-wal').exists())
                before = plan_evidence_envelope(store.db_path, *args)
                copy_database(store.db_path, root / 'restored.sqlite3')
                after = plan_evidence_envelope(root / 'restored.sqlite3', *args)
                self.assertEqual(before, after)
                self.assertEqual(plan, store.get_plan(plan['id']))
                self.assertFalse(after['persisted'])
