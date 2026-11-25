"""Real SQLite and worker lifecycle; controlled faults, not real AI alternatives."""
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from agent.plan_store import PlanStore
from agent.plan_recovery import copy_database
from agent.plan_continuation import ContinuationRequired
from agent.task_attempts import RetryBlockedError
from agent.task_worker import execute_task


class GeneralFailureCeilingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'p.sqlite3';self.store=PlanStore(self.path)
        self.plan=self.store.create_plan('Ceiling',[{'task_type':'workspace_status'},{'task_type':'workspace_status'}])
        self.store.run_next_step(self.plan['id'])  # Production completed result must never replay.
        self.first=self.store.get_plan(self.plan['id'])['steps'][0]
        self.step=self.plan['steps'][1]['id']

    def fail_five(self,error=None):
        with patch('agent.task_worker.execute_task',side_effect=error or TimeoutError()) as worker:
            for index in range(5):
                if index:self.store.retry_failed_step(self.plan['id'],self.step,'synthetic_fault_condition_repaired')
                result=self.store.run_next_step(self.plan['id'])
                self.assertEqual('paused',result['status'])
            self.assertEqual(5,worker.call_count)
        return result['task_id']

    def approve(self,store=None,**changes):
        store=store or self.store;s=store.get_failure_checkpoint(self.plan['id'])
        args={'plan_id':self.plan['id'],'expected_updated_at':s['updated_at'],
              'expected_failure_count':s['recorded_failures'],'decision_id':str(uuid.uuid4()),**changes}
        return store.continue_after_decision(**args),args

    def test_fifth_wait_restart_explicit_decision_then_success_no_replay_restore(self):
        tid=self.fail_five();state=self.store.get_failure_checkpoint(self.plan['id'])
        self.assertEqual((5,0,5,True),(state['recorded_failures'],state['approved_through'],state['failures_since_continue'],state['continuation_required']))
        restarted=PlanStore(self.path)
        with patch('agent.task_worker.execute_task') as worker:
            self.assertEqual('paused',restarted.supervise(self.plan['id'])['status'])
            with self.assertRaises(ContinuationRequired):restarted.retry_failed_step(self.plan['id'],self.step,'asserted_repair')
            self.assertEqual(state,restarted.get_failure_checkpoint(self.plan['id']));worker.assert_not_called()
            receipt,args=self.approve(restarted)
            self.assertFalse(receipt['dispatched']);self.assertEqual('failed',restarted.queue.get(tid)['status'])
            self.assertEqual('paused',restarted.get_plan(self.plan['id'])['status'])
            with self.assertRaises(ValueError):restarted.continue_after_decision(**args)
            worker.assert_not_called()
        restarted.retry_failed_step(self.plan['id'],self.step)
        with patch('agent.task_worker.execute_task',wraps=execute_task) as worker:
            self.assertEqual('completed',restarted.run_next_step(self.plan['id'])['plan_status'])
            self.assertEqual(1,worker.call_count)
            self.assertEqual('completed',PlanStore(self.path).run_until_done(self.plan['id'])['status'])
            self.assertEqual(1,worker.call_count)
        self.assertEqual(self.first,restarted.get_plan(self.plan['id'])['steps'][0])
        backup=Path(self.tmp.name)/'backup.sqlite3';copy_database(self.path,backup)
        restored=PlanStore(backup)
        self.assertEqual(restarted.get_failure_checkpoint(self.plan['id']),restored.get_failure_checkpoint(self.plan['id']))
        self.assertEqual(restarted.queue.list_plan_attempts(tid),restored.queue.list_plan_attempts(tid))
        with patch('agent.task_worker.execute_task') as worker:
            self.assertEqual('completed',restored.run_until_done(self.plan['id'])['status']);worker.assert_not_called()

    def test_waiting_backup_restore_cancel_and_corrupt_decision_refused(self):
        self.fail_five();snapshot=Path(self.tmp.name)/'waiting.sqlite3';copy_database(self.path,snapshot)
        restored=PlanStore(snapshot);state=restored.get_failure_checkpoint(self.plan['id'])
        self.assertTrue(state['continuation_required'])
        with patch('agent.task_worker.execute_task') as worker:
            with self.assertRaises(ContinuationRequired):restored.retry_failed_step(self.plan['id'],self.step,'asserted_repair')
            restored.cancel_plan(self.plan['id'],state['updated_at'])
            self.assertEqual('cancelled',restored.run_until_done(self.plan['id'])['status']);worker.assert_not_called()
        self.approve()
        with sqlite3.connect(self.path) as db:db.execute('UPDATE plan_continuations SET failure_count=6')
        with self.assertRaises(ValueError):copy_database(self.path,Path(self.tmp.name)/'bad.sqlite3')

    def test_continuation_never_waives_permission_repair_gate(self):
        tid=self.fail_five(PermissionError());self.approve()
        with patch('agent.task_worker.execute_task') as worker:
            with self.assertRaises(RetryBlockedError):self.store.retry_failed_step(self.plan['id'],self.step)
            self.assertEqual('failed',self.store.queue.get(tid)['status']);worker.assert_not_called()
        self.store.retry_failed_step(self.plan['id'],self.step,'specific_operator_permission_repair_assertion')

    def test_stale_invalid_concurrent_decisions_and_cancel(self):
        self.fail_five();state=self.store.get_failure_checkpoint(self.plan['id'])
        for changes in ({'expected_updated_at':'old'},{'expected_failure_count':4},{'expected_failure_count':True},{'decision_id':'notuuid'}):
            with self.assertRaises(ValueError):self.approve(**changes)
        def approve(_):
            try:return PlanStore(self.path).continue_after_decision(self.plan['id'],state['updated_at'],5,str(uuid.uuid4()))['dispatched']
            except ValueError:return 'refused'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(approve,range(2)))
        self.assertEqual(1,results.count(False));self.assertEqual(1,results.count('refused'))
        current=self.store.get_plan(self.plan['id']);self.store.cancel_plan(self.plan['id'],current['updated_at'])
        with patch('agent.task_worker.execute_task') as worker:
            self.assertEqual('cancelled',PlanStore(self.path).run_until_done(self.plan['id'])['status'])
            with self.assertRaises(ValueError):self.approve()
            worker.assert_not_called()
        self.assertEqual(self.first,self.store.get_plan(self.plan['id'])['steps'][0])

    def test_second_five_requires_new_decision_and_no_plan_cross_contamination(self):
        self.fail_five();self.approve()
        other=self.store.create_plan('Other',[{'task_type':'workspace_status'}])
        self.assertFalse(self.store.get_failure_checkpoint(other['id'])['continuation_required'])
        with patch('agent.task_worker.execute_task',side_effect=TimeoutError()) as worker:
            for _ in range(5):
                self.store.retry_failed_step(self.plan['id'],self.step);self.store.run_next_step(self.plan['id'])
            self.assertEqual(5,worker.call_count)
        with self.assertRaises(ContinuationRequired):self.store.retry_failed_step(self.plan['id'],self.step)
        self.assertEqual(10,self.store.get_failure_checkpoint(self.plan['id'])['recorded_failures'])
        self.approve()

    def test_prior_journal_schema_backup_no_mutation_and_upgrade_unknown_history(self):
        with sqlite3.connect(self.path) as db:db.execute('DROP TABLE plan_continuations')
        backup=Path(self.tmp.name)/'old.sqlite3';copy_database(self.path,backup)
        with sqlite3.connect(self.path) as db:self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='plan_continuations'").fetchone())
        self.assertEqual(0,PlanStore(backup).get_failure_checkpoint(self.plan['id'])['recorded_failures'])


if __name__=='__main__':unittest.main()
