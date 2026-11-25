"""Real stores, deterministic offline adapters, no transport approval claim."""
import base64
import json
import unittest
from unittest.mock import patch
from agent.adult.bounded_text_application import BoundedTextApplication
from agent.adult.models import TaskBudget
from tests import test_adult_supervisor as fixtures


class ApplicationTests(unittest.TestCase):
    setUp=fixtures.SupervisorTests.setUp
    routes=fixtures.SupervisorTests.routes

    def app(self,count=6,succeed_last=True):
        self.routes(count,succeed_last=succeed_last)
        return BoundedTextApplication(self.executor,fixtures.CONFIG,budget=TaskBudget())

    def test_five_failures_explicit_resume_runs_one_bounded_segment_then_result(self):
        app=self.app();staged=app.submit_text_task('text.analyze','Safe original.')
        self.assertEqual('checkpointed',staged['status']);self.assertEqual([0]*6,[a.calls for a in self.adapters])
        task=staged['task_id'];waiting=app.run_task(task)
        self.assertEqual('waiting_user',waiting['task']['status'])
        self.assertEqual([1,1,1,1,1,0],[a.calls for a in self.adapters])
        self.assertEqual('waiting_user',app.run_task(task)['task']['status'])
        # Synthetic explicit decision, not evidence of a user's real consent.
        result=app.resume_task(task,'continue',max_attempts=1)
        self.assertEqual('completed',result['task']['status'])
        self.assertEqual(1,self.tasks.get_task(task).approvals_granted)
        self.assertEqual([1]*6,[a.calls for a in self.adapters])
        self.assertIn(b'Not AI inference',base64.b64decode(app.get_result_chunk(task)['data_base64']))
        self.assertEqual('input_removed',result['input_cleanup']['status'])
        with self.assertRaises(ValueError):app.resume_task(task,'continue')
        self.assertEqual('completed',app.run_task(task)['task']['status'])
        self.assertEqual([1]*6,[a.calls for a in self.adapters])
        self.assertNotIn('Safe original.',json.dumps(result))

    def test_checkpoint_resume_does_not_invent_approval_and_cancel_never_dispatches(self):
        app=self.app(2);task=app.submit_text_task('text.analyze','Safe original.')['task_id']
        result=app.resume_task(task,'continue',max_attempts=1)
        self.assertEqual('checkpointed',result['task']['status'])
        self.assertEqual(0,self.tasks.get_task(task).approvals_granted)
        self.assertEqual([1,0],[a.calls for a in self.adapters])
        result=app.resume_task(task,'cancel')
        self.assertEqual('cancelled',result['task']['status'])
        self.assertEqual([1,0],[a.calls for a in self.adapters])

    def test_invalid_decision_and_bounds_preserve_waiting_state(self):
        app=self.app();task=app.submit_text_task('text.analyze','Safe original.')['task_id'];app.run_task(task)
        before=(self.root/'tasks.sqlite3').read_bytes()
        for decision,kwargs in [('yes',{}),('continue',{'max_attempts':True}),('continue',{'max_seconds':241})]:
            with self.assertRaises(ValueError):app.resume_task(task,decision,**kwargs)
            self.assertEqual(before,(self.root/'tasks.sqlite3').read_bytes())
        self.assertEqual([1,1,1,1,1,0],[a.calls for a in self.adapters])

    def test_lost_approval_commit_response_never_dispatches_sixth(self):
        app=self.app();task=app.submit_text_task('text.analyze','Safe original.')['task_id'];app.run_task(task)
        real=self.tasks.resume
        def lost(*args):real(*args);raise RuntimeError('synthetic response loss')
        with patch.object(self.tasks,'resume',side_effect=lost):
            with self.assertRaises(RuntimeError):app.resume_task(task,'continue')
        self.assertEqual('queued',self.tasks.get_task(task).status)
        self.assertEqual([1,1,1,1,1,0],[a.calls for a in self.adapters])
        self.assertEqual('completed',app.run_task(task,max_attempts=1)['task']['status'])
        self.assertEqual([1]*6,[a.calls for a in self.adapters])

    def test_paused_no_route_not_generically_resumed(self):
        app=self.app(0);task=app.submit_text_task('text.analyze','Safe original.')['task_id']
        self.assertEqual('paused',app.run_task(task)['task']['status'])
        with self.assertRaises(ValueError):app.resume_task(task,'continue')
        self.assertEqual(0,self.tasks.get_task(task).attempt_count)

    def test_process_cache_loss_moves_needs_input_and_exact_restore_runs_once(self):
        from agent.adult.payload_store import AdultArtifactStore
        app=self.app(1);task=app.submit_text_task('text.analyze','Safe original.')['task_id']
        # A fresh artifact store models a new process with no transient bytes.
        reopened=AdultArtifactStore(self.root/'artifacts',cipher=self.cipher)
        self.executor.intake.artifacts=reopened;self.pipeline._artifact_store=reopened
        app=BoundedTextApplication(self.executor,fixtures.CONFIG,budget=TaskBudget())
        outcome=app.run_task(task)
        self.assertEqual('needs_input',outcome['task']['status'])
        self.assertEqual(0,self.adapters[0].calls)
        self.assertEqual(0,self.tasks.get_task(task).attempt_count)
        with self.assertRaises(ValueError):app.restore_input_and_resume(task,'Changed input.')
        self.assertEqual('needs_input',self.tasks.get_task(task).status)
        result=app.restore_input_and_resume(task,'Safe original.',max_attempts=1)
        self.assertEqual('completed',result['task']['status'])
        self.assertEqual(1,self.adapters[0].calls)
        self.assertEqual('input_removed',result['input_cleanup']['status'])

    def test_actual_new_process_reports_missing_ephemeral_input_without_attempt(self):
        import os,subprocess,sys
        self.app(1)
        code="""
import json,os
from pathlib import Path
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.payload_store import AdultArtifactStore,PayloadCipher
from agent.adult.fixed_free_text_service import FixedFreeTextService
from agent.adult.integrations.omniroute_chat import AdultOmniRouteConfig
from agent.adult.models import TaskBudget
root=Path(os.environ['TEST_ROOT']);cipher=PayloadCipher(os.environ['TEST_KEY'])
service=FixedFreeTextService(AdultTaskStore(root/'tasks.sqlite3'),
 AdultArtifactStore(root/'artifacts',cipher=cipher),cipher,
 AdultOmniRouteConfig('https://openrouter.ai/api/v1','synthetic-offline-only'))
request=service.tasks.get_task_request(os.environ['TEST_TASK'])
executor=service._executor('text.analyze',request.input_fingerprint,TaskBudget())
result=executor.run(os.environ['TEST_TASK'],None,'hmac-sha256:abcdef0123456789abcdef0123456789')
print(json.dumps({'status':result.status,'attempts':service.tasks.get_task(result.task_id).attempt_count}))
"""
        import base64
        # Fixture cipher cannot expose its key; build a second task with known test key.
        from agent.adult.payload_store import PayloadCipher,AdultArtifactStore
        import secrets
        key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode();cipher=PayloadCipher(key)
        self.executor.fingerprinter=cipher;self.executor.intake.fingerprinter=cipher
        store=AdultArtifactStore(self.root/'artifacts',cipher=cipher)
        self.executor.intake.artifacts=store;self.pipeline._artifact_store=store
        app=BoundedTextApplication(self.executor,fixtures.CONFIG,budget=TaskBudget())
        task=app.submit_text_task('text.analyze','Private process source.')['task_id']
        result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'TEST_ROOT':str(self.root),
            'TEST_KEY':key,'TEST_TASK':task},capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual({'status':'needs_input','attempts':0},json.loads(result.stdout))

    def test_input_lost_after_lease_precheck_leaves_pending_step_and_can_restore(self):
        app=self.app(1);task=app.submit_text_task('text.analyze','Safe original.')['task_id']
        original=self.pipeline.run_one_attempt
        def evict_then_run(*args,**kwargs):
            self.artifacts.cache.discard(task)
            return original(*args,**kwargs)
        with patch.object(self.pipeline,'run_one_attempt',side_effect=evict_then_run):
            outcome=app.run_task(task)
        self.assertEqual('needs_input',outcome['task']['status'])
        self.assertEqual((0,0),(self.tasks.get_task(task).attempt_count,self.adapters[0].calls))
        step=self.tasks.get_step(task,self.tasks.get_task(task).current_step_id)
        self.assertEqual('pending',step.status)
        result=app.restore_input_and_resume(task,'Safe original.',max_attempts=1)
        self.assertEqual('completed',result['task']['status'])
        self.assertEqual(1,self.adapters[0].calls)

    def test_no_attempt_route_loss_keeps_pending_step(self):
        app=self.app(0);task=app.submit_text_task('text.analyze','Safe original.')['task_id']
        result=app.run_task(task)
        self.assertEqual('paused',result['task']['status'])
        self.assertEqual(0,self.tasks.get_task(task).attempt_count)
        self.assertEqual('pending',self.tasks.get_step(task,self.tasks.get_task(task).current_step_id).status)

    def test_task_paging_and_attempt_history_are_safe_readonly(self):
        app=self.app(1);ids=[app.submit_text_task('text.analyze','Safe original.')['task_id']
                            for _ in range(2)]
        app.run_task(ids[0])
        page=app.list_tasks(limit=1)
        self.assertEqual(1,len(page['tasks']));self.assertTrue(page['has_more'])
        self.assertEqual(1,len(app.list_tasks(limit=1,cursor=page['next_cursor'])['tasks']))
        attempts=app.get_attempts(ids[0])
        self.assertEqual(1,len(attempts));self.assertEqual('succeeded',attempts[0]['status'])
        for data in (page,attempts):
            for forbidden in ('payload_id','input_fingerprint','approach_key','Safe original.'):
                self.assertNotIn(forbidden,str(data))
        before=(self.root/'tasks.sqlite3').read_bytes()
        app.list_tasks();app.get_attempts(ids[0])
        self.assertEqual(before,(self.root/'tasks.sqlite3').read_bytes())
        for limit in (0,21,True,1.0):
            with self.assertRaises(ValueError):app.list_tasks(limit=limit)
            with self.assertRaises(ValueError):app.get_attempts(ids[0],limit=limit)

    def test_mixed_lane_list_and_attempt_reads_fail_closed(self):
        from agent.adult.models import AdultTaskRequest
        app=self.app(0);app.submit_text_task('text.analyze','Safe original.')
        image=self.tasks.create_task(AdultTaskRequest('image.generate',self.cipher.fingerprint(b'x')),
                                     TaskBudget())
        with self.assertRaises(ValueError):app.list_tasks()
        with self.assertRaises(ValueError):app.get_attempts(image.task_id)

    def test_cancel_during_finalization_returns_cancelled_and_cleans_input(self):
        app=self.app(1);task=app.submit_text_task('text.analyze','Safe original.')['task_id']
        adapter=self.pipeline._adapters[('offline-supervised-0','1')]
        original=adapter.invoke
        def cancel(request):
            self.tasks.request_cancel(task)
            return original(request)
        with patch.object(adapter,'invoke',side_effect=cancel):result=app.run_task(task)
        self.assertEqual('cancelled',result['task']['status'])
        self.assertEqual('input_removed',result['input_cleanup']['status'])
        self.assertIsNone(self.tasks.get_task(task).latest_result_artifact_id)
        with self.assertRaises(ValueError):app.get_result_chunk(task)

    def test_nonterminal_supervisor_exception_is_not_hidden(self):
        app=self.app(0);task=app.submit_text_task('text.analyze','Safe original.')['task_id']
        with patch.object(app.supervisor,'run_until_done',side_effect=RuntimeError()):
            with self.assertRaises(RuntimeError):app.run_task(task)
        self.assertEqual('checkpointed',self.tasks.get_task(task).status)
