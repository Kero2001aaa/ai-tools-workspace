"""Real SQLite clean-path recovery, namespace exclusion and no-replay tests."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.plan_recovery import copy_database
from agent.plan_store import PlanStore
from agent.task_worker import execute_task


class PlanRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.sqlite3'
        self.store = PlanStore(self.source)
        self.plan = self.store.create_plan('Recovery', [{'task_type': 'workspace_status'}, {'task_type': 'workspace_status'}])
        self.backup = self.root / 'backup.sqlite3'
        self.restored = self.root / 'restored.sqlite3'

    def test_checkpoint_backup_restore_clean_path_preserves_output_and_identity(self):
        first = self.store.run_next_step(self.plan['id'])
        before = self.store.get_plan(self.plan['id'])
        summary = copy_database(self.source, self.backup)
        self.assertEqual({'plans': 1, 'plan_steps': 2, 'tasks': 1}, summary['rows'])
        self.assertFalse(summary['workers_started'])
        self.assertFalse(summary['states_rewritten'])
        copy_database(self.backup, self.restored)
        recovered = PlanStore(self.restored)
        self.assertEqual(before, recovered.get_plan(self.plan['id']))
        self.assertEqual(self.store.queue.get(first['task_id']), recovered.queue.get(first['task_id']))
        with patch('agent.task_worker.execute_task', wraps=execute_task) as worker:
            recovered.run_until_done(self.plan['id'])
            recovered.run_until_done(self.plan['id'])
            self.assertEqual(1, worker.call_count)
        self.assertEqual(before['steps'][0], recovered.get_plan(self.plan['id'])['steps'][0])
        self.assertEqual(before, self.store.get_plan(self.plan['id']))
        self.assertEqual(0o600, self.restored.stat().st_mode & 0o777)

    def test_wal_committed_pages_included(self):
        db = sqlite3.connect(self.source)
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('UPDATE plans SET title=? WHERE id=?', ('Committed in WAL', self.plan['id']))
        db.commit()
        self.assertTrue(Path(str(self.source) + '-wal').exists())
        copy_database(self.source, self.backup)
        self.assertEqual('Committed in WAL', PlanStore(self.backup).get_plan(self.plan['id'])['title'])

    def test_unknown_running_retained_no_dispatch_after_restore(self):
        with patch.object(self.store.queue, 'claim_plan_task', side_effect=RuntimeError('interrupt')):
            with self.assertRaises(RuntimeError):
                self.store.run_next_step(self.plan['id'])
        task = self.store.queue.list_tasks()[0]
        claim = self.store.queue.claim_plan_task(task['id'])
        copy_database(self.source, self.backup)
        recovered = PlanStore(self.backup)
        with patch('agent.task_worker.execute_task') as worker:
            self.assertEqual('running', recovered.run_next_step(self.plan['id'])['status'])
            worker.assert_not_called()
        self.assertEqual(claim, recovered.queue.get(claim['id']))

    def test_committed_success_before_projection_restores_without_replay(self):
        with patch.object(self.store.queue, 'claim_plan_task', side_effect=RuntimeError('interrupt')):
            with self.assertRaises(RuntimeError):
                self.store.run_next_step(self.plan['id'])
        task = self.store.queue.list_tasks()[0]
        claim = self.store.queue.claim_plan_task(task['id'])
        self.store.queue.finish_plan_task(claim, result={'saved': True})
        copy_database(self.source, self.backup)
        with patch('agent.task_worker.execute_task') as worker:
            outcome = PlanStore(self.backup).run_next_step(self.plan['id'])
            self.assertEqual({'saved': True}, outcome['result'])
            worker.assert_not_called()

    def test_existing_target_symlink_source_and_missing_source_refused(self):
        self.backup.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):
            copy_database(self.source, self.backup)
        self.assertEqual(b'existing', self.backup.read_bytes())
        self.backup.unlink()
        self.backup.symlink_to(self.source)
        with self.assertRaises(FileExistsError):
            copy_database(self.source, self.backup)
        with self.assertRaises(ValueError):
            copy_database(self.backup, self.restored)
        with self.assertRaises(ValueError):
            copy_database(self.root / 'absent', self.restored)
        self.assertFalse(self.restored.exists())

    def test_racing_destination_is_not_overwritten(self):
        def race(source, destination):
            Path(destination).write_bytes(b'raced')
            raise FileExistsError('race')
        with patch('agent.plan_recovery.os.link', side_effect=race):
            with self.assertRaises(FileExistsError):
                copy_database(self.source, self.backup)
        self.assertEqual(b'raced', self.backup.read_bytes())
        self.assertEqual([], list(self.root.glob('.plan-snapshot-*')))

    def test_unrelated_namespace_and_active_schema_refused(self):
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE adult_private(secret TEXT)')
        with self.assertRaises(ValueError):
            copy_database(self.source, self.backup)
        self.assertFalse(self.backup.exists())
        with sqlite3.connect(self.source) as db:
            db.execute('DROP TABLE adult_private')
            db.execute('CREATE VIEW extra AS SELECT * FROM plans')
        with self.assertRaises(ValueError):
            copy_database(self.source, self.backup)
        self.assertFalse(self.backup.exists())

    def test_identity_tampering_and_disallowed_type_refused(self):
        self.store.run_next_step(self.plan['id'])
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE tasks SET id='tampered'")
        with self.assertRaises(ValueError):
            copy_database(self.source, self.backup)
        with sqlite3.connect(self.source) as db:
            db.execute("DELETE FROM tasks")
            db.execute("UPDATE plan_steps SET task_type='image.generate'")
        with self.assertRaises(ValueError):
            copy_database(self.source, self.backup)
        self.assertFalse(self.backup.exists())

    def test_uncommitted_changes_not_included(self):
        db = sqlite3.connect(self.source)
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("UPDATE plans SET title='uncommitted'")
        copy_database(self.source, self.backup)
        self.assertEqual('Recovery', PlanStore(self.backup).get_plan(self.plan['id'])['title'])
        db.rollback()

    def test_known_failure_stays_paused_then_explicit_retry_reuses_identity(self):
        with patch('agent.task_worker.execute_task', side_effect=RuntimeError('known failure')):
            self.store.run_next_step(self.plan['id'])
        old = self.store.queue.list_tasks()[0]
        copy_database(self.source, self.backup)
        recovered = PlanStore(self.backup)
        with patch('agent.task_worker.execute_task') as worker:
            self.assertEqual('paused', recovered.run_next_step(self.plan['id'])['status'])
            worker.assert_not_called()
        recovered.retry_failed_step(self.plan['id'], self.plan['steps'][0]['id'])
        with patch('agent.task_worker.execute_task', return_value={'safe': True}):
            result = recovered.run_next_step(self.plan['id'])
        self.assertEqual(old['id'], result['task_id'])
        self.assertEqual(2, recovered.queue.get(old['id'])['attempts'])

    def test_legacy_unknown_running_not_replayed(self):
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE plan_steps SET status='running' WHERE position=1")
        copy_database(self.source, self.backup)
        with patch('agent.task_worker.execute_task') as worker:
            self.assertEqual('running', PlanStore(self.backup).run_next_step(self.plan['id'])['status'])
            worker.assert_not_called()

    def test_unbound_running_queue_refused_to_avoid_lease_replay_on_restore(self):
        task = self.store.queue.enqueue('workspace_status')
        self.assertEqual(task['id'], self.store.queue.claim_next()['id'])
        with self.assertRaises(ValueError):
            copy_database(self.source, self.backup)
        self.assertFalse(self.backup.exists())
        self.assertEqual('running', self.store.queue.get(task['id'])['status'])

    def test_invalid_lifecycle_refused(self):
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE plans SET status='invented'")
        with self.assertRaises(ValueError):
            copy_database(self.source, self.backup)
        self.assertFalse(self.backup.exists())

    def test_corrupt_or_oversize_refused_and_temporary_cleaned(self):
        bad = self.root / 'bad.sqlite3'
        bad.write_bytes(b'not sqlite')
        with self.assertRaises(sqlite3.DatabaseError):
            copy_database(bad, self.backup)
        with patch('agent.plan_recovery.MAX_BYTES', 1):
            with self.assertRaises(ValueError):
                copy_database(self.source, self.backup)
        self.assertEqual([], list(self.root.glob('.plan-snapshot-*')))
        self.assertFalse(self.backup.exists())


if __name__ == '__main__':
    unittest.main()
