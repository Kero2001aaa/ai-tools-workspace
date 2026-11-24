"""Bounded executor integration over real persistence, no network."""
import base64
from datetime import datetime,timedelta,timezone
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from agent.adult.adult_text_executor import AdultTextExecutor
from agent.adult.adult_text_intake import AdultTextIntake
from agent.adult.adult_text_input import encode_text_input
from agent.adult.adult_text_pipeline import AdultTextPipeline
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.payload_store import AdultArtifactStore,PayloadCipher
from agent.adult.capability_registry import ModelCapabilityRegistry
from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_policies import AdultPolicies
from agent.adult.adult_evaluator import AdultEvaluator
from agent.adult.models import ModelCapabilitySpec,CapabilityEvidence
from agent.adult.integrations.protocols import TextAdapterResult

CONFIG='hmac-sha256:abcdef0123456789abcdef0123456789'

class Adapter:
    def __init__(self): self.calls=0
    def invoke(self,request):
        self.calls+=1
        return TextAdapterResult('Deterministic local plumbing result. Not AI inference.',{},0)

class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.tasks=AdultTaskStore(self.root/'tasks.sqlite3')
        self.cipher=PayloadCipher(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        self.artifacts=AdultArtifactStore(self.root/'artifacts',cipher=self.cipher)
        self.registry=ModelCapabilityRegistry(self.tasks)
        self.tracker=AdultAttemptTracker(self.tasks)
        self.router=AdultModelRouter(self.registry,approach_key_builder=self.tracker.approach_keys,local_adapter_available=lambda x:x=='offline-text')
        self.adapter=Adapter();policy=AdultPolicies()
        self.pipeline=AdultTextPipeline(self.artifacts,self.tasks,self.router,self.tracker,policy,AdultEvaluator(policy),{('offline-text','1'):self.adapter})
        self.executor=AdultTextExecutor(self.tasks,AdultTextIntake(self.tasks,self.artifacts,self.cipher),self.pipeline,self.cipher)
        self.data=encode_text_input('text.analyze','Safe private test text.')
    def enable(self):
        spec=ModelCapabilitySpec('offline-text','1','text.analyze',('text',),('text',),'local')
        rec=self.registry.register(spec);now=datetime.now(timezone.utc)
        evidence=CapabilityEvidence(capability_source='local_adapter_smoke_test',capability_reference='test:offline-text-v1',verified_at=now.isoformat().replace('+00:00','Z'),expires_at=(now+timedelta(hours=1)).isoformat().replace('+00:00','Z'),adapter_id='offline-text',adapter_version='1',task_type='text.analyze',input_modalities=('text',),output_modalities=('text',))
        rec=self.registry.verify(rec.capability_id,evidence,rec.state_version);self.registry.enable(rec.capability_id,rec.state_version)
    def test_complete_checkpoint_reopen_and_no_duplicate(self):
        self.enable();task=self.executor.create('text.analyze',self.data)
        result=self.executor.run(task.task_id,self.data,CONFIG)
        self.assertEqual('completed',result.status);self.assertIsNotNone(result.artifact_id)
        self.assertIn(b'Not AI inference',self.artifacts.read_chunk(result.artifact_id,0).data)
        reopened=AdultTaskStore(self.root/'tasks.sqlite3')
        self.assertEqual(result.artifact_id,reopened.get_task(task.task_id).latest_result_artifact_id)
        self.assertEqual(1,reopened.get_task(task.task_id).attempt_count)
        self.assertEqual('step_completed',reopened.get_latest_checkpoint(task.task_id).reason)
        self.assertEqual(result,self.executor.run(task.task_id,None,CONFIG));self.assertEqual(1,self.adapter.calls)
        self.assertNotIn(self.data,(self.root/'tasks.sqlite3').read_bytes())
    def test_unavailable_route_pauses_without_attempt(self):
        task=self.executor.create('text.analyze',self.data);result=self.executor.run(task.task_id,self.data,CONFIG)
        self.assertEqual('paused',result.status);self.assertEqual('CAPABILITY_UNAVAILABLE',result.error_code)
        self.assertEqual(0,self.tasks.get_task(task.task_id).attempt_count);self.assertEqual(0,self.adapter.calls)
        self.assertIsNotNone(self.tasks.get_latest_checkpoint(task.task_id))
    def test_missing_input_needs_input(self):
        task=self.executor.create('text.analyze',self.data);result=self.executor.run(task.task_id,None,CONFIG)
        self.assertEqual('needs_input',result.status);self.assertEqual(0,self.adapter.calls)
    def test_cancellation_before_run_prevents_call(self):
        self.enable();task=self.executor.create('text.analyze',self.data);self.tasks.request_cancel(task.task_id)
        self.assertEqual('cancelled',self.executor.run(task.task_id,self.data,CONFIG).status);self.assertEqual(0,self.adapter.calls)
    def test_provider_failure_is_one_attempt_not_retry(self):
        self.enable();task=self.executor.create('text.analyze',self.data)
        with patch.object(self.adapter,'invoke',side_effect=TimeoutError()) as invoke:
            result=self.executor.run(task.task_id,self.data,CONFIG)
        self.assertEqual('paused',result.status);self.assertEqual(1,invoke.call_count)
        self.assertEqual(1,self.tasks.get_task(task.task_id).attempt_count)
    def test_invalid_input_creates_no_task(self):
        with self.assertRaises(Exception):self.executor.create('text.analyze',b'bad')
        self.assertEqual((),self.tasks.list_tasks().items)
    def test_recovered_prior_attempt_never_replays(self):
        self.enable();task=self.executor.create('text.analyze',self.data)
        with patch.object(self.adapter,'invoke',side_effect=TimeoutError()):
            self.executor.run(task.task_id,self.data,CONFIG)
        from dataclasses import replace
        prior=self.tasks.get_task(task.task_id)
        with patch.object(self.tasks,'get_task',return_value=replace(prior,status='queued')):
            result=self.executor.run(task.task_id,None,CONFIG)
        self.assertEqual('prior_attempt_requires_recovery',result.error_code)
        self.assertEqual(0,self.adapter.calls)
    def test_cancellation_during_adapter_prevents_completed_task(self):
        self.enable();task=self.executor.create('text.analyze',self.data)
        def cancel(request):
            self.tasks.request_cancel(task.task_id)
            return TextAdapterResult('A safe valid result.',{},0)
        with patch.object(self.adapter,'invoke',side_effect=cancel):
            with self.assertRaises(Exception):self.executor.run(task.task_id,self.data,CONFIG)
        self.assertEqual('cancelled',self.tasks.get_task(task.task_id).status)
        self.assertIsNone(self.tasks.get_task(task.task_id).latest_result_artifact_id)
    def test_finalization_error_cannot_replay_committed_attempt(self):
        self.enable();task=self.executor.create('text.analyze',self.data)
        with patch.object(self.tasks,'save_checkpoint',side_effect=OSError()):
            with self.assertRaises(OSError):self.executor.run(task.task_id,self.data,CONFIG)
        self.assertEqual(1,self.adapter.calls)
        from dataclasses import replace
        prior=self.tasks.get_task(task.task_id)
        with patch.object(self.tasks,'get_task',return_value=replace(prior,status='queued')):
            result=self.executor.run(task.task_id,None,CONFIG)
        self.assertEqual('prior_attempt_requires_recovery',result.error_code)
        self.assertEqual(1,self.adapter.calls)
    def test_durable_encrypted_stage_then_reopen_without_input(self):
        from agent.adult.adult_text_executor import AdultTextExecutor
        key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        cipher=PayloadCipher(key);artifacts=AdultArtifactStore(self.root/'retained',cipher=cipher)
        policy=AdultPolicies()
        pipeline=AdultTextPipeline(artifacts,self.tasks,self.router,self.tracker,policy,AdultEvaluator(policy),{('offline-text','1'):self.adapter})
        executor=AdultTextExecutor(self.tasks,AdultTextIntake(self.tasks,artifacts,cipher),pipeline,cipher)
        self.enable()
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':key}):
            staged=executor.stage_encrypted('text.analyze',self.data)
        self.assertEqual('checkpointed',staged.status);self.assertEqual(0,self.adapter.calls)
        deadline=self.tasks.get_task(staged.task_id).deadline_at
        request=self.tasks.get_task_request(staged.task_id)
        self.assertEqual('encrypted',request.retention_policy)
        self.assertNotIn(self.data,(self.root/'tasks.sqlite3').read_bytes())
        for file in (self.root/'retained').glob('*.aap'):self.assertNotIn(self.data,file.read_bytes())
        # Reconstruct every stateful component, not just a second store handle.
        tasks=AdultTaskStore(self.root/'tasks.sqlite3');cipher=PayloadCipher(key)
        artifacts=AdultArtifactStore(self.root/'retained',cipher=cipher)
        tracker=AdultAttemptTracker(tasks);registry=ModelCapabilityRegistry(tasks)
        router=AdultModelRouter(registry,approach_key_builder=tracker.approach_keys,local_adapter_available=lambda x:x=='offline-text')
        pipeline=AdultTextPipeline(artifacts,tasks,router,tracker,policy,AdultEvaluator(policy),{('offline-text','1'):self.adapter})
        reopened=AdultTextExecutor(tasks,AdultTextIntake(tasks,artifacts,cipher),pipeline,cipher)
        result=reopened.run(staged.task_id,None,CONFIG)
        self.assertEqual('completed',result.status);self.assertEqual(1,self.adapter.calls)
        self.assertEqual(deadline,tasks.get_task(staged.task_id).deadline_at)
        reread=AdultArtifactStore(self.root/'retained',cipher=PayloadCipher(key))
        self.assertIn(b'Not AI inference',reread.read_chunk(result.artifact_id,0).data)

    def test_stage_missing_or_mismatched_key_creates_no_task(self):
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaises(Exception):self.executor.stage_encrypted('text.analyze',self.data)
        wrong=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':wrong}):
            with self.assertRaisesRegex(ValueError,'configured_key_mismatch'):
                self.executor.stage_encrypted('text.analyze',self.data)
        self.assertEqual((),self.tasks.list_tasks().items)
    def test_stage_readback_error_preserves_committed_encrypted_input(self):
        key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode();cipher=PayloadCipher(key)
        artifacts=AdultArtifactStore(self.root/'retained',cipher=cipher);policy=AdultPolicies()
        pipeline=AdultTextPipeline(artifacts,self.tasks,self.router,self.tracker,policy,AdultEvaluator(policy),{('offline-text','1'):self.adapter})
        executor=AdultTextExecutor(self.tasks,AdultTextIntake(self.tasks,artifacts,cipher),pipeline,cipher)
        original=self.tasks.attach_input
        def committed_then_error(*args,**kwargs):
            original(*args,**kwargs);raise OSError()
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':key}),patch.object(self.tasks,'attach_input',side_effect=committed_then_error):
            with self.assertRaises(Exception):executor.stage_encrypted('text.analyze',self.data)
        task=self.tasks.list_tasks().items[0];request=self.tasks.get_task_request(task.task_id)
        self.assertEqual('paused',task.status);self.assertEqual(0,self.adapter.calls)
        self.assertEqual(self.data,AdultArtifactStore(self.root/'retained',cipher=PayloadCipher(key)).load_input(request.payload_id,task.task_id))

    def test_stage_cancellation_stays_cancelled_without_model(self):
        key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode();cipher=PayloadCipher(key)
        artifacts=AdultArtifactStore(self.root/'retained',cipher=cipher);policy=AdultPolicies()
        pipeline=AdultTextPipeline(artifacts,self.tasks,self.router,self.tracker,policy,AdultEvaluator(policy),{('offline-text','1'):self.adapter})
        executor=AdultTextExecutor(self.tasks,AdultTextIntake(self.tasks,artifacts,cipher),pipeline,cipher)
        original=executor._checkpoint
        def checkpoint_then_cancel(task,*args,**kwargs):
            original(task,*args,**kwargs);self.tasks.request_cancel(task.task_id)
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':key}),patch.object(executor,'_checkpoint',side_effect=checkpoint_then_cancel):
            with self.assertRaises(Exception):executor.stage_encrypted('text.analyze',self.data)
        task=self.tasks.list_tasks().items[0];self.assertEqual('cancelled',task.status)
        self.assertEqual(0,self.adapter.calls)
