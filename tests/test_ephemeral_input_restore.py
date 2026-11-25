"""Exact transient reupload, real stores and offline fake client only."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from agent.adult.fixed_free_text_service import FixedFreeTextService
from agent.adult.adult_task_restore import resume_restored_ephemeral
from agent.adult.payload_store import AdultArtifactStore,PayloadUnavailable
from agent.adult.models import TaskBudget
from tests import test_fixed_free_text_service as fixtures


class RestoreTests(unittest.TestCase):
    setUp=fixtures.FixedServiceTests.setUp
    service=fixtures.FixedServiceTests.service
    enable_route=fixtures.FixedServiceTests.enable_route

    def lost(self):
        original=self.service();self.record=self.enable_route(original)
        task=original.submit('text.analyze','Original source.','Original instruction.',
                            budget=TaskBudget(max_provider_calls=1),retain_payload=False).task_id
        service=FixedFreeTextService(self.tasks,AdultArtifactStore(self.root/'retained',cipher=self.cipher),
                                    self.cipher,original.config)
        self.assertEqual('needs_input',service.run(task).status)
        return service,task

    def test_exact_restore_one_fake_segment_and_original_limits(self):
        service,task=self.lost();before=self.tasks.get_task(task)
        fake=Mock();fake.invoke.return_value=SimpleNamespace(content='Safe fake result.',usage_metadata={},response_metadata={})
        with patch('agent.adult.integrations.omniroute_chat.build_chat_model',return_value=fake):
            result=service.restore_input_and_run(task,'Original source.','Original instruction.')
            self.assertEqual('completed',result.status);self.assertEqual(1,fake.invoke.call_count)
            with self.assertRaises(ValueError):service.restore_input_and_run(task,'Original source.','Original instruction.')
            self.assertEqual('completed',service.run(task).status);self.assertEqual(1,fake.invoke.call_count)
        after=self.tasks.get_task(task)
        self.assertEqual(before.deadline_at,after.deadline_at)
        self.assertEqual(before.max_total_attempts,after.max_total_attempts)
        self.assertEqual(before.approvals_granted,after.approvals_granted)

    def test_mismatch_cancel_and_invalid_bound_never_cache_or_call(self):
        service,task=self.lost();ref=self.tasks.get_task_request(task).payload_id
        with patch('agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            for text,instruction in [('Changed.','Original instruction.'),('Original source.','')]:
                with self.assertRaises(ValueError):service.restore_input_and_run(task,text,instruction)
            with self.assertRaises(ValueError):service.restore_input_and_run(task,'Original source.','Original instruction.',max_seconds=True)
            self.assertEqual('needs_input',self.tasks.get_task(task).status)
            with self.assertRaises(PayloadUnavailable):service.artifacts.load_input(ref,task)
            self.tasks.request_cancel(task)
            with self.assertRaises(ValueError):service.restore_input_and_run(task,'Original source.','Original instruction.')
            factory.assert_not_called()

    def test_lost_commit_response_queues_but_does_not_dispatch(self):
        service,task=self.lost();real=resume_restored_ephemeral
        def lost(*args):
            real(*args)
            raise RuntimeError('synthetic response loss')
        with patch('agent.adult.adult_task_restore.resume_restored_ephemeral',side_effect=lost),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            with self.assertRaises(RuntimeError):service.restore_input_and_run(task,'Original source.','Original instruction.')
            factory.assert_not_called()
        self.assertEqual('queued',self.tasks.get_task(task).status)
        self.assertEqual(0,self.tasks.get_task(task).attempt_count)
        ref=self.tasks.get_task_request(task).payload_id
        self.assertIn(b'Original source.',service.artifacts.load_input(ref,task))

    def test_store_guard_rejects_stale_reference_cancel_and_deadline(self):
        from agent.adult.adult_task_store import AdultTaskStoreError
        service,task=self.lost();snapshot=self.tasks.get_task(task);request=self.tasks.get_task_request(task)
        for ref,fingerprint,version in [('0'*32,request.input_fingerprint,snapshot.state_version),
                (request.payload_id,'bad',snapshot.state_version),
                (request.payload_id,request.input_fingerprint,snapshot.state_version-1)]:
            with self.assertRaises(AdultTaskStoreError):
                resume_restored_ephemeral(self.tasks,task,ref,fingerprint,version)
        with patch('agent.adult.adult_task_restore._utc_now',return_value='2099-01-01T00:00:00.000000Z'):
            with self.assertRaises(AdultTaskStoreError):
                resume_restored_ephemeral(self.tasks,task,request.payload_id,request.input_fingerprint,snapshot.state_version)
        self.assertEqual('needs_input',self.tasks.get_task(task).status)
        self.tasks.request_cancel(task)
        with self.assertRaises(AdultTaskStoreError):
            resume_restored_ephemeral(self.tasks,task,request.payload_id,request.input_fingerprint,
                                               self.tasks.get_task(task).state_version)

    def test_restore_is_transient_idempotent_and_cannot_overwrite_input(self):
        from agent.adult.payload_store import InvalidPayloadReference
        service,task=self.lost();ref=self.tasks.get_task_request(task).payload_id
        service.artifacts.restore_transient_input(task,ref,b'one')
        service.artifacts.restore_transient_input(task,ref,b'one')
        with self.assertRaises(InvalidPayloadReference):service.artifacts.restore_transient_input(task,ref,b'two')
        retained=service.artifacts.save_input(task,b'encrypted','text/plain',retain_encrypted=True)
        with self.assertRaises(InvalidPayloadReference):service.artifacts.restore_transient_input(task,retained.payload_id,b'one')
        self.assertEqual(b'encrypted',service.artifacts.load_input(retained.payload_id,task))

    def test_queued_response_loss_then_new_process_loss_can_request_reupload_again(self):
        service,task=self.lost();request=self.tasks.get_task_request(task);snapshot=self.tasks.get_task(task)
        service.artifacts.restore_transient_input(task,request.payload_id,b'not-used-after-reopen')
        resume_restored_ephemeral(self.tasks,task,request.payload_id,request.input_fingerprint,snapshot.state_version)
        reopened=FixedFreeTextService(self.tasks,AdultArtifactStore(self.root/'retained',cipher=self.cipher),
                                      self.cipher,service.config)
        with patch('agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            self.assertEqual('needs_input',reopened.run(task).status)
            self.assertEqual(0,self.tasks.get_task(task).attempt_count);factory.assert_not_called()
        fake=Mock();fake.invoke.return_value=SimpleNamespace(content='Safe fake result.',usage_metadata={},response_metadata={})
        with patch('agent.adult.integrations.omniroute_chat.build_chat_model',return_value=fake):
            self.assertEqual('completed',reopened.restore_input_and_run(task,'Original source.','Original instruction.').status)
            self.assertEqual(1,fake.invoke.call_count)

    def test_actual_subprocess_restores_original_reference_without_persistent_input(self):
        import json,os,subprocess,sys
        service,task=self.lost();request=self.tasks.get_task_request(task)
        # Clear route evidence so this subprocess cannot reach any network path.
        record=self.record
        service.registry.disable(record.capability_id,'operator_disabled',record.state_version)
        code="""
import json,os
from pathlib import Path
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.payload_store import AdultArtifactStore,PayloadCipher
from agent.adult.fixed_free_text_service import FixedFreeTextService
from agent.adult.integrations.omniroute_chat import AdultOmniRouteConfig
root=Path(os.environ['TEST_ROOT']);cipher=PayloadCipher(os.environ['TEST_KEY'])
service=FixedFreeTextService(AdultTaskStore(root/'tasks.sqlite3'),
 AdultArtifactStore(root/'retained',cipher=cipher),cipher,
 AdultOmniRouteConfig('https://openrouter.ai/api/v1','synthetic-offline-only'))
outcome=service.restore_input_and_run(os.environ['TEST_TASK'],'Original source.','Original instruction.')
request=service.tasks.get_task_request(outcome.task_id)
print(json.dumps({'status':outcome.status,'reference':request.payload_id,
 'attempts':service.tasks.get_task(outcome.task_id).attempt_count,
 'cached':bool(service.artifacts.load_input(request.payload_id,outcome.task_id))}))
"""
        result=subprocess.run([sys.executable,'-c',code],env={**os.environ,
          'TEST_ROOT':str(self.root),'TEST_KEY':self.key,'TEST_TASK':task},capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual({'status':'paused','reference':request.payload_id,'attempts':0,'cached':True},json.loads(result.stdout))
