"""Real SQLite general-plan attempt history and explicit repair journey."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.plan_store import PlanStore
from agent.plan_recovery import copy_database
from agent.task_attempts import fingerprints, classify, digest
from agent.task_worker import execute_task


class GeneralAttemptJourneyTests(unittest.TestCase):
    def test_permission_wait_explicit_repair_restart_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'plans.sqlite3';store=PlanStore(db)
            plan=store.create_plan('Attempt evidence',[{'task_type':'workspace_status'}])
            with patch('agent.task_worker.execute_task',side_effect=PermissionError('private path never journaled')) as worker:
                failed=store.run_next_step(plan['id'])
                self.assertEqual('paused',failed['status']);self.assertEqual(1,worker.call_count)
                self.assertEqual('paused',store.run_next_step(plan['id'])['status'])
                self.assertEqual(1,worker.call_count)
            tid=failed['task_id'];first=store.queue.list_plan_attempts(tid)
            self.assertEqual(1,len(first));self.assertEqual(('permission',0,'PERMISSION_DENIED'),(first[0]['failure_class'],first[0]['retryable'],first[0]['error_code']))
            self.assertNotIn('private path',json.dumps(first))
            with patch('agent.task_worker.execute_task',wraps=execute_task) as worker:
                reopened=PlanStore(db)
                reopened.retry_failed_step(plan['id'],plan['steps'][0]['id'],'operator_repaired_read_permission')
                result=reopened.run_next_step(plan['id']);self.assertEqual('step_succeeded',result['status'])
                self.assertEqual(1,worker.call_count)
                self.assertEqual('completed',PlanStore(db).run_until_done(plan['id'])['status'])
                self.assertEqual(1,worker.call_count)
            history=store.queue.list_plan_attempts(tid)
            self.assertEqual(['failed','succeeded'],[r['status'] for r in history])
            self.assertEqual(history[0]['method_fingerprint'],history[1]['method_fingerprint'])
            self.assertEqual(history[0]['input_fingerprint'],history[1]['input_fingerprint'])
            self.assertEqual('operator_repaired_read_permission',history[1]['retry_reason'])
            encoded=json.dumps(result['result'],separators=(',',':'),sort_keys=True)
            self.assertEqual(digest(encoded),history[1]['result_sha256'])
            backup=Path(tmp)/'backup.sqlite3';restored=Path(tmp)/'restored.sqlite3'
            copy_database(db,backup);copy_database(backup,restored)
            restored_store=PlanStore(restored)
            self.assertEqual(history,restored_store.queue.list_plan_attempts(tid))
            with patch('agent.task_worker.execute_task') as worker:
                self.assertEqual('completed',restored_store.run_until_done(plan['id'])['status']);worker.assert_not_called()

    def test_fingerprint_canonicalization_and_different_inputs(self):
        self.assertEqual(fingerprints('memory_recent',{'limit':5}),fingerprints('memory_recent',{'limit':5}))
        self.assertNotEqual(fingerprints('memory_recent',{'limit':5})[1],fingerprints('memory_recent',{'limit':6})[1])
        self.assertNotEqual(fingerprints('memory_recent',{})[0],fingerprints('workspace_status',{})[0])
        self.assertEqual(('transient',True,'TRANSIENT_IO'),classify(TimeoutError()))
        self.assertEqual(('permission',False,'SOURCE_SCOPE_UNAVAILABLE'),classify(ValueError('code_source_not_approved')))
        self.assertFalse(classify(RuntimeError())[1])

    def test_stale_claim_cannot_finalize_attempt_or_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=PlanStore(Path(tmp)/'p.sqlite3');p=store.create_plan('Fence',[{'task_type':'workspace_status'}])
            with patch('agent.task_worker.execute_task',side_effect=TimeoutError()):failed=store.run_next_step(p['id'])
            tid=failed['task_id'];store.retry_failed_step(p['id'],p['steps'][0]['id'])
            claim=store.queue.claim_plan_task(tid);bad={**claim,'locked_at':'old'}
            before=store.queue.list_plan_attempts(tid)
            with self.assertRaises(ValueError):store.queue.finish_plan_task(bad,result={})
            self.assertEqual(before,store.queue.list_plan_attempts(tid));self.assertEqual('running',store.queue.get(tid)['status'])

    def test_legacy_backup_and_cancel_not_forced_to_migrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'old.sqlite3';store=PlanStore(db);store.create_plan('Old',[{'task_type':'workspace_status'}])
            with sqlite3.connect(db) as c:c.execute('DROP TABLE task_attempts')
            copy_database(db,Path(tmp)/'old-backup.sqlite3')
            with sqlite3.connect(db) as c:self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='task_attempts'").fetchone())


if __name__=='__main__':unittest.main()
