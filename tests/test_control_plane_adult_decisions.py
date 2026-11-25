"""Actual loopback HTTP capability boundary, not remote human authentication."""
import http.client
import json
import uuid
import unittest
from tests import test_control_plane_adult as fixtures
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.models import AdultTaskRequest,TaskBudget
from agent.adult.payload_store import AdultArtifactStore,PayloadCipher


class AdultDecisionHTTPTests(fixtures.AdultStatusTests):
    def post(self,path,body,token=fixtures.TOKEN,extra=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        headers={'Content-Type':'application/json'}
        if token is not None:headers['Authorization']='Bearer '+token
        headers.update(extra or {})
        conn.request('POST',path,json.dumps(body),headers)
        response=conn.getresponse();data=response.read();conn.close()
        return response.status,json.loads(data)

    def task(self):
        self.store=AdultTaskStore(self.db)
        task=self.store.create_task(AdultTaskRequest('text.analyze',fixtures.FINGERPRINT),TaskBudget())
        return task

    def body(self,task,**changes):
        return {'task_id':task.task_id,'expected_state_version':task.state_version,
                'decision_id':uuid.uuid4().hex,**changes}

    def test_no_wrong_token_foreign_origin_and_untrusted_host_do_not_mutate(self):
        task=self.task();body=self.body(task);before=self.db.read_bytes()
        for token,extra,code in ((None,{},401),('wrong-owner-token',{},401),
             (fixtures.TOKEN,{'Origin':'https://foreign.invalid'},403),
             (fixtures.TOKEN,{'Host':'foreign.invalid'},403),
             (fixtures.TOKEN,{'Sec-Fetch-Site':'cross-site'},403)):
            self.assertEqual(code,self.post('/api/adult/cancel',body,token,extra)[0])
            self.assertEqual(before,self.db.read_bytes())

    def test_exact_replay_restart_idempotence_and_conflicting_reuse(self):
        task=self.task();body=self.body(task)
        code,result=self.post('/api/adult/cancel',body)
        self.assertEqual(200,code);self.assertEqual('cancelled',result['task']['status'])
        after=self.db.read_bytes()
        self.assertEqual(200,self.post('/api/adult/cancel',body)[0])
        self.assertEqual(after,self.db.read_bytes())
        reopened=AdultTaskStore(self.db)
        replay=reopened.request_cancel(task.task_id,expected_state_version=task.state_version,decision_id=body['decision_id'])
        self.assertEqual(0,replay.approvals_granted);self.assertEqual(0,replay.attempt_count)
        self.assertEqual(1,len([event for event in reopened.list_events(task.task_id) if event.event_type=='task_cancelled']))
        self.assertEqual(409,self.post('/api/adult/cancel',{**body,'expected_state_version':task.state_version+1})[0])
        second=self.store.create_task(AdultTaskRequest('text.generate',fixtures.FINGERPRINT),TaskBudget())
        self.assertEqual(409,self.post('/api/adult/cancel',self.body(second,decision_id=body['decision_id']))[0])
        self.assertEqual('queued',self.store.get_task(second.task_id).status)

    def test_stale_invalid_body_terminal_and_disabled_execution(self):
        task=self.task();body=self.body(task)
        lease=self.store.claim_task(task.task_id,'worker',30)
        self.assertEqual(409,self.post('/api/adult/cancel',body)[0])
        current=self.store.get_task(task.task_id);before=self.db.read_bytes()
        for values in ({'principal':'owner'},{'expected_state_version':True},{'decision_id':'../bad'}):
            self.assertEqual(400,self.post('/api/adult/cancel',{**self.body(current),**values})[0])
            self.assertEqual(before,self.db.read_bytes())
        for path in ('/api/adult/continue','/api/adult/run'):
            self.assertEqual(503,self.post(path,self.body(current))[0])
            self.assertEqual(401,self.post(path,self.body(current),token=None)[0])
            self.assertEqual(before,self.db.read_bytes())
        self.store.release_lease(lease,current.state_version,'failed')
        terminal=self.store.get_task(task.task_id)
        self.assertEqual(409,self.post('/api/adult/cancel',self.body(terminal))[0])

    def test_active_worker_cancel_fences_without_dropping_lease_and_safe_read(self):
        task=self.task();lease=self.store.claim_task(task.task_id,'active-worker',30)
        current=self.store.get_task(task.task_id)
        code,result=self.post('/api/adult/cancel',self.body(current))
        self.assertEqual(200,code);self.assertTrue(result['task']['cancel_requested'])
        self.assertEqual('analyzing',result['task']['status'])
        self.assertEqual('deferred_to_worker',result['input_cleanup'])
        self.assertEqual(0,result['task']['attempt_count'])
        status,body=self.get('/api/adult/tasks/'+task.task_id)
        self.assertEqual(200,status)
        for secret in ('fingerprint','lease_owner','payload_id','decision_id','expected_state_version'):
            self.assertNotIn(secret,body.decode())
        latest=self.store.get_task(task.task_id)
        self.store.release_lease(lease,latest.state_version,'cancelled',reason='cancel_observed')
        self.assertEqual('cancelled',self.store.get_task(task.task_id).status)

    def test_terminal_cancel_cleanup_only_task_inputs_and_no_missing_db_create(self):
        self.assertEqual(404,self.post('/api/adult/cancel',{'task_id':'a'*32,'expected_state_version':0,'decision_id':uuid.uuid4().hex})[0])
        self.assertFalse(self.db.exists())
        task=self.task()
        artifacts=AdultArtifactStore(self.db.parent/'artifacts',cipher=PayloadCipher(b'k'*32))
        source=artifacts.save_input(task.task_id,b'private source','text/plain',retain_encrypted=False)
        output=artifacts.save_output(task.task_id,'a'*32,b'output','text/plain')
        self.server.adult_artifacts=artifacts
        code,result=self.post('/api/adult/cancel',self.body(task))
        self.assertEqual(200,code);self.assertEqual('input_removed',result['input_cleanup'])
        from agent.adult.payload_store import PayloadUnavailable
        with self.assertRaises(PayloadUnavailable):artifacts.load_input(source.payload_id,task.task_id)
        self.assertEqual(b'output',artifacts.read_chunk(output.artifact_id,0,task_id=task.task_id).data)

    def test_duplicate_keys_and_action_rate_limit_preserve_task(self):
        task=self.task();before=self.db.read_bytes()
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        raw=json.dumps(self.body(task))[:-1]+',"expected_state_version":999}'
        conn.request('POST','/api/adult/cancel',raw,{'Authorization':'Bearer '+fixtures.TOKEN,'Content-Type':'application/json'})
        response=conn.getresponse();response.read();conn.close()
        self.assertEqual(400,response.status);self.assertEqual(before,self.db.read_bytes())
        import time
        self.server.action_times=[time.monotonic()]*10
        self.assertEqual(429,self.post('/api/adult/cancel',self.body(task))[0])
        self.assertEqual(before,self.db.read_bytes())

    def test_cleanup_failure_exact_retry_recovers_without_new_event(self):
        from unittest.mock import patch
        task=self.task();body=self.body(task)
        artifacts=AdultArtifactStore(self.db.parent/'retry-artifacts',cipher=PayloadCipher(b'r'*32))
        source=artifacts.save_input(task.task_id,b'private source','text/plain',retain_encrypted=False)
        self.server.adult_artifacts=artifacts
        with patch.object(artifacts,'delete_task_inputs',side_effect=OSError('failure')):
            code,result=self.post('/api/adult/cancel',body)
        self.assertEqual(200,code);self.assertEqual('cleanup_failed',result['input_cleanup'])
        count=len(self.store.list_events(task.task_id));before=self.db.read_bytes()
        code,result=self.post('/api/adult/cancel',body)
        self.assertEqual(200,code);self.assertEqual('input_removed',result['input_cleanup'])
        self.assertEqual(count,len(self.store.list_events(task.task_id)))
        self.assertEqual(before,self.db.read_bytes())
        self.assertEqual(409,self.post('/api/adult/cancel',self.body(self.store.get_task(task.task_id)))[0])
