"""Real plan/queue identity, claim serialization and crash-recovery boundaries."""
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from agent.plan_store import PlanStore
from agent.task_queue import TaskQueue
from agent.task_worker import execute_task,run_once

class PlanQueueBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'plans.sqlite3';self.plans=PlanStore(self.path)
        self.plan=self.plans.create_plan('Bound plan',[{'task_type':'workspace_status'},{'task_type':'workspace_status'}])
    def identity(self,index=0):
        return str(uuid.uuid5(uuid.NAMESPACE_URL,'workspace-plan-step:'+self.plan['steps'][index]['id']))
    def test_one_durable_identity_worker_result_and_reopen(self):
        with patch('agent.task_worker.execute_task',wraps=execute_task) as worker:
            first=self.plans.run_next_step(self.plan['id'])
            self.assertEqual(self.identity(),first['task_id'])
            dispatched=worker.call_args.args[0]
            self.assertEqual(first['task_id'],dispatched['id'])
            self.assertEqual(first['step_id'],dispatched['plan_step_id'])
            task=self.plans.queue.get(first['task_id'])
            self.assertEqual(('succeeded',first['result']),(task['status'],task['result']))
            self.assertEqual('completed',PlanStore(self.path).run_until_done(self.plan['id'],max_steps=1)['status'])
            PlanStore(self.path).run_until_done(self.plan['id'],max_steps=1)
            self.assertEqual(2,worker.call_count)
            self.assertEqual(2,len(self.plans.queue.list_tasks()))
    def test_running_claim_never_replayed_even_if_old_and_generic_worker_excluded(self):
        entered=threading.Event();release=threading.Event();outputs=[]
        def worker(task):entered.set();self.assertTrue(release.wait(5));return {'safe':True}
        def run():outputs.append(self.plans.run_next_step(self.plan['id']))
        with patch('agent.task_worker.execute_task',side_effect=worker) as dispatch:
            thread=threading.Thread(target=run);thread.start();self.assertTrue(entered.wait(5))
            try:
                with self.plans._connect() as db:
                    db.execute("UPDATE plan_steps SET started_at='2000-01-01T00:00:00+00:00' WHERE id=?",(self.plan['steps'][0]['id'],))
                    db.execute("UPDATE tasks SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",(self.identity(),))
                second=PlanStore(self.path).run_next_step(self.plan['id'])
                self.assertEqual('running',second['status'])
                self.assertEqual('idle',run_once(self.plans.queue)['status'])
                self.assertEqual(1,dispatch.call_count)
            finally:release.set();thread.join(5)
            self.assertFalse(thread.is_alive());self.assertEqual(1,dispatch.call_count)
        self.assertEqual('step_succeeded',outputs[0]['status'])
    def test_success_committed_before_plan_update_reconciles_without_dispatch(self):
        step=self.plan['steps'][0]
        with self.plans._connect() as db:
            row=db.execute('SELECT * FROM plan_steps WHERE id=?',(step['id'],)).fetchone()
            self.plans.queue.bind_plan_step(db,row)
            db.execute("UPDATE plan_steps SET status='running' WHERE id=?",(step['id'],))
        claim=self.plans.queue.claim_plan_task(self.identity())
        self.plans.queue.finish_plan_task(claim,result={'committed':True})
        with patch('agent.task_worker.execute_task') as worker:
            result=PlanStore(self.path).run_next_step(self.plan['id'])
            self.assertEqual('step_succeeded',result['status']);self.assertEqual({'committed':True},result['result'])
            worker.assert_not_called()
        self.assertEqual('succeeded',self.plans.get_plan(self.plan['id'])['steps'][0]['status'])
    def test_explicit_retry_same_task_identity_and_old_claim_fenced(self):
        claims=[]
        def fail(task):claims.append(task);raise RuntimeError('known test failure')
        with patch('agent.task_worker.execute_task',side_effect=fail):
            outcome=self.plans.run_next_step(self.plan['id'])
        self.assertEqual('paused',outcome['status'])
        self.assertEqual('failed',self.plans.queue.get(self.identity())['status'])
        self.assertEqual('paused',self.plans.run_next_step(self.plan['id'])['status'])
        self.plans.retry_failed_step(self.plan['id'],self.plan['steps'][0]['id'], retry_reason="synthetic_fixture_repaired")
        with patch('agent.task_worker.execute_task',return_value={'retry':True}):
            result=self.plans.run_next_step(self.plan['id'])
        self.assertEqual(self.identity(),result['task_id'])
        self.assertEqual(2,self.plans.queue.get(self.identity())['attempts'])
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.plans.queue.finish_plan_task(claims[0],result={'stale':True})
        self.assertEqual(before,self.path.read_bytes())
    def test_unknown_running_queue_and_legacy_running_do_not_dispatch(self):
        with self.plans._connect() as db:
            step=db.execute('SELECT * FROM plan_steps WHERE id=?',(self.plan['steps'][0]['id'],)).fetchone()
            self.plans.queue.bind_plan_step(db,step)
        self.assertIsNotNone(self.plans.queue.claim_plan_task(self.identity()))
        with patch('agent.task_worker.execute_task') as worker:
            self.assertEqual('running',PlanStore(self.path).run_next_step(self.plan['id'])['status'])
            self.assertEqual('idle',run_once(self.plans.queue)['status'])
            worker.assert_not_called()
        with self.assertRaises(ValueError):self.plans.queue.complete(self.identity(),{'bypass':True})
        with self.assertRaises(ValueError):self.plans.queue.fail(self.identity(),'bypass')
    def test_two_readonly_types_only_and_adult_data_not_admitted(self):
        for name in ['text.analyze','image.generate','shell_command']:
            with self.assertRaises(ValueError):self.plans.create_plan('Forbidden',[{'task_type':name}])
        self.assertEqual([],self.plans.queue.list_tasks())
    def test_legacy_database_migrates_without_modifying_completed_output(self):
        old=Path(self.temp.name)/'old.sqlite3'
        with sqlite3.connect(old) as db:
            db.execute('''CREATE TABLE tasks(id TEXT PRIMARY KEY,task_type TEXT,payload TEXT,status TEXT,
            attempts INTEGER,max_attempts INTEGER,run_at TEXT,locked_at TEXT,result TEXT,last_error TEXT,created_at TEXT,updated_at TEXT)''')
            db.execute("INSERT INTO tasks VALUES('old','workspace_status','{}','succeeded',1,1,'x',NULL,'{}',NULL,'x','x')")
        queue=TaskQueue(old);self.assertEqual('succeeded',queue.get('old')['status'])
        self.assertIsNone(queue.get('old')['plan_step_id'])
    def test_plan_and_task_binding_survives_dispatch_interruption_without_new_identity(self):
        with patch.object(self.plans.queue,'claim_plan_task',side_effect=OSError('interrupted before claim')):
            with self.assertRaises(OSError):self.plans.run_next_step(self.plan['id'])
        pending=self.plans.queue.get(self.identity())
        self.assertEqual('pending',pending['status']);self.assertEqual(0,pending['attempts'])
        with patch('agent.task_worker.execute_task',return_value={'recovered':True}) as worker:
            result=PlanStore(self.path).run_next_step(self.plan['id'])
            self.assertEqual(self.identity(),result['task_id']);self.assertEqual(1,worker.call_count)
        self.assertEqual(1,len(self.plans.queue.list_tasks()))
    def test_lost_completion_response_reconciles_without_second_dispatch(self):
        finish=self.plans.queue.finish_plan_task
        def lost(*args,**kwargs):finish(*args,**kwargs);raise OSError('response lost after commit')
        with patch.object(self.plans.queue,'finish_plan_task',side_effect=lost),patch('agent.task_worker.execute_task',return_value={'saved':True}) as worker:
            result=self.plans.run_next_step(self.plan['id'])
            self.assertEqual('step_succeeded',result['status']);self.assertEqual(1,worker.call_count)
        self.assertEqual({'saved':True},self.plans.get_plan(self.plan['id'])['steps'][0]['result'])
    def test_cross_plan_retry_and_tampered_legacy_type_do_not_dispatch(self):
        other=self.plans.create_plan('Other',[{'task_type':'workspace_status'}])
        with patch('agent.task_worker.execute_task',side_effect=RuntimeError('known failure')):
            self.plans.run_next_step(self.plan['id'])
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.plans.retry_failed_step(other['id'],self.plan['steps'][0]['id'], retry_reason="synthetic_fixture_repaired")
        self.assertEqual(before,self.path.read_bytes())
        with self.plans._connect() as db:
            db.execute("UPDATE plan_steps SET task_type='image.generate' WHERE plan_id=?",(other['id'],))
        with patch('agent.task_worker.execute_task') as worker:
            with self.assertRaises(ValueError):self.plans.run_next_step(other['id'])
            worker.assert_not_called()
    def test_failed_second_step_cannot_rewrite_completed_first_queue_result(self):
        first=self.plans.run_next_step(self.plan['id']);before=self.plans.queue.get(first['task_id'])
        with patch('agent.task_worker.execute_task',side_effect=RuntimeError('known failure')):
            second=self.plans.run_next_step(self.plan['id'])
        self.assertEqual('paused',second['status'])
        self.plans.retry_failed_step(self.plan['id'],self.plan['steps'][1]['id'], retry_reason="synthetic_fixture_repaired")
        self.plans.run_next_step(self.plan['id'])
        self.assertEqual(before,self.plans.queue.get(first['task_id']))
    def test_supervisor_reconciles_durable_success_while_plan_still_running(self):
        step=self.plan['steps'][0]
        with self.plans._connect() as db:
            row=db.execute('SELECT * FROM plan_steps WHERE id=?',(step['id'],)).fetchone()
            self.plans.queue.bind_plan_step(db,row)
            db.execute("UPDATE plan_steps SET status='running' WHERE id=?",(step['id'],))
            db.execute("UPDATE plans SET status='running' WHERE id=?",(self.plan['id'],))
        claim=self.plans.queue.claim_plan_task(self.identity())
        self.plans.queue.finish_plan_task(claim,result={'committed':True})
        with patch('agent.task_worker.execute_task') as worker:
            result=PlanStore(self.path).supervise(self.plan['id'],max_steps=1)
            self.assertEqual('resume_later',result['decision']);worker.assert_not_called()
        self.assertEqual('succeeded',self.plans.get_plan(self.plan['id'])['steps'][0]['status'])

    def test_two_concurrent_targeted_claims_have_exactly_one_winner(self):
        with self.plans._connect() as db:
            step=db.execute('SELECT * FROM plan_steps WHERE id=?',(self.plan['steps'][0]['id'],)).fetchone()
            self.plans.queue.bind_plan_step(db,step)
        gate=threading.Barrier(3);results=[];errors=[]
        def claim():
            try:
                queue=TaskQueue(self.path);gate.wait(5);results.append(queue.claim_plan_task(self.identity()))
            except Exception as exc:errors.append(type(exc).__name__)
        threads=[threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:thread.start()
        gate.wait(5)
        for thread in threads:thread.join(5)
        self.assertFalse(errors);self.assertEqual(1,sum(result is not None for result in results))
        self.assertEqual(1,self.plans.queue.get(self.identity())['attempts'])
