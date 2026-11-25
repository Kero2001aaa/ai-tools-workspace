"""Explicit authenticated HTTP cancellation reuses the inactive transaction."""
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from agent.control_plane import Handler, allowed_authorities
from agent.plan_store import PlanStore


class HTTPPlanCancelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'plans?#.sqlite3'
        env=patch.dict(os.environ,{'WORKSPACE_PLAN_DB':str(self.path)});env.start();self.addCleanup(env.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.control_token='test-token';self.server.allowed_authorities=allowed_authorities(self.server.server_port)
        self.server.action_lock=threading.Lock();self.server.action_times=[]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

    def request(self, body, extra=None, auth=True):
        headers={'Content-Type':'application/json'}
        if auth:headers['Authorization']='Bearer test-token'
        headers.update(extra or {})
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        connection.request('POST','/api/plans/cancel',body=body if isinstance(body,str) else json.dumps(body),headers=headers)
        response=connection.getresponse();status=response.status;data=json.loads(response.read());connection.close()
        return status,data

    def make(self):
        self.store=PlanStore(self.path)
        return self.store.create_plan('PRIVATE_PLAN_TITLE',[{'task_type':'workspace_status'}]*2)

    def body(self, plan):return {'plan_id':plan['id'],'expected_updated_at':plan['updated_at']}

    def test_auth_origin_no_create_and_strict_body(self):
        self.assertEqual(401,self.request({},auth=False)[0])
        self.assertEqual(403,self.request({},extra={'Origin':'https://other.invalid'})[0])
        self.assertFalse(self.path.exists())
        self.assertEqual(400,self.request({'extra':1})[0])
        self.assertEqual(400,self.request('{"plan_id":"a","plan_id":"b","expected_updated_at":"x"}')[0])
        self.assertFalse(self.path.exists())

    def test_checkpoint_cancel_retains_completed_result_attempts_and_no_dispatch(self):
        plan=self.make();self.store.run_next_step(plan['id']);current=self.store.get_plan(plan['id'])
        first=current['steps'][0];task=self.store.queue.list_tasks()[0]
        with patch('agent.task_worker.execute_task',side_effect=AssertionError('no dispatch')):
            status,result=self.request(self.body(current))
        self.assertEqual(200,status);self.assertEqual('cancelled',result['status'])
        self.assertEqual({'plan_id','status','updated_at'},set(result))
        self.assertNotIn('PRIVATE_PLAN_TITLE',json.dumps(result))
        after=self.store.get_plan(plan['id']);self.assertEqual(first,after['steps'][0])
        self.assertEqual('cancelled',after['steps'][1]['status'])
        self.assertEqual(task,self.store.queue.get(task['id']))
        before=after
        self.assertEqual(409,self.request(self.body(current))[0])
        self.assertEqual(before,self.store.get_plan(plan['id']))

    def test_stale_or_running_refuses_with_unchanged_records(self):
        plan=self.make()
        self.store.run_next_step(plan['id'])
        before=self.store.get_plan(plan['id'])
        self.assertEqual(409,self.request(self.body(plan))[0]);self.assertEqual(before,self.store.get_plan(plan['id']))
        with sqlite3.connect(self.path) as db:db.execute("UPDATE plans SET status='running'")
        before=self.store.get_plan(plan['id'])
        self.assertEqual(409,self.request(self.body(before))[0]);self.assertEqual(before,self.store.get_plan(plan['id']))

    def test_missing_and_legacy_schema_no_create_or_migration(self):
        from uuid import uuid4
        body={'plan_id':str(uuid4()),'expected_updated_at':'x'}
        self.assertEqual(409,self.request(body)[0]);self.assertFalse(self.path.exists())
        plan=self.make()
        with sqlite3.connect(self.path) as db:
            db.execute('DROP INDEX idx_tasks_plan_step');db.execute('ALTER TABLE tasks DROP COLUMN plan_step_id')
        before=self.path.read_bytes()
        self.assertEqual(409,self.request(self.body(plan))[0]);self.assertEqual(before,self.path.read_bytes())

    def test_trigger_schema_refused_before_cancellation(self):
        plan=self.make()
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER private_trigger AFTER UPDATE ON plans BEGIN DELETE FROM tasks; END")
        before=self.path.read_bytes()
        self.assertEqual(409,self.request(self.body(plan))[0]);self.assertEqual(before,self.path.read_bytes())
