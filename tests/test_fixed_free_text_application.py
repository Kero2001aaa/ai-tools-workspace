"""Text facade with real stores and a fake wire client, no live calls."""
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent.adult.fixed_free_text_application import FixedFreeTextApplication
from agent.adult.models import TaskBudget
from tests import test_fixed_free_text_service as fixtures


class ApplicationTests(unittest.TestCase):
    setUp = fixtures.FixedServiceTests.setUp
    service = fixtures.FixedServiceTests.service
    enable_route = fixtures.FixedServiceTests.enable_route

    def application(self):
        service = self.service()
        return FixedFreeTextApplication(service,budget=TaskBudget(max_provider_calls=1))

    def test_retention_must_be_boolean_before_any_task_is_created(self):
        app=self.application()
        for consent in (None,1,'true'):
            with self.assertRaises(ValueError):app.submit_text_task('text.analyze','private context',retain_payload=consent)
        self.assertEqual((),self.tasks.list_tasks().items)

    def test_full_fake_journey_safe_metadata_result_chunk_and_no_duplicate(self):
        app=self.application();self.enable_route(app._service)
        fake=Mock();fake.invoke.return_value=SimpleNamespace(content='Safe fake result.',usage_metadata={},response_metadata={})
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model',return_value=fake):
            staged=app.submit_text_task('text.analyze','private source context',retain_payload=True)
            self.assertEqual('checkpointed',staged['status']);fake.invoke.assert_not_called()
            outcome=app.run_task(staged['task_id']);self.assertEqual('completed',outcome['task']['status'])
            self.assertEqual(1,len(app.get_attempts(staged['task_id'])))
            self.assertEqual(1,len(app.list_tasks()['tasks']))
            result=app.get_result_chunk(staged['task_id'],0,4)
            self.assertEqual(b'Safe',base64.b64decode(result['data_base64']))
            self.assertFalse(result['complete']);self.assertEqual(4,result['next_offset'])
            self.assertEqual('completed',app.run_task(staged['task_id'])['task']['status'])
            self.assertEqual(1,fake.invoke.call_count)
        encoded=json.dumps([staged,outcome,app.list_tasks(),app.get_attempts(staged['task_id'])])
        for forbidden in ('private source context','input_fingerprint','config_fingerprint','approach_key','synthetic-unit-test-value',str(self.root)):
            self.assertNotIn(forbidden,encoded)

    def test_no_route_pause_and_cancel_do_not_invent_attempt(self):
        app=self.application()
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            task=app.submit_text_task('text.rewrite','private context',retain_payload=True)['task_id']
            self.assertEqual('paused',app.run_task(task)['task']['status'])
            self.assertEqual([],app.get_attempts(task));factory.assert_not_called()
            self.assertEqual('cancelled',app.cancel_task(task)['status'])
            self.assertEqual('cancelled',app.run_task(task)['task']['status']);factory.assert_not_called()
        with self.assertRaises(ValueError):app.get_result_chunk(task)

    def test_bounds_invalid_tasks_and_no_arbitrary_artifact_read(self):
        app=self.application()
        for limit in (0,21,True):
            with self.assertRaises(ValueError):app.list_tasks(limit=limit)
        for operation in (app.get_task,app.get_attempts,app.cancel_task,app.get_result_chunk,app.run_task):
            with self.assertRaises(ValueError):operation('unknown')
        with self.assertRaises(TypeError):app.get_result_chunk(artifact_id='arbitrary')
        with self.assertRaises(ValueError):FixedFreeTextApplication(app._service,budget=TaskBudget())
        with self.assertRaises(TypeError):FixedFreeTextApplication(object(),budget=TaskBudget(max_provider_calls=1))

    def test_explicit_pagination_and_readonly_metadata(self):
        app=self.application()
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}):
            ids={app.submit_text_task('text.analyze',f'Safe sample {i}',retain_payload=True)['task_id'] for i in range(3)}
        before=(self.root/'tasks.sqlite3').read_bytes()
        page=app.list_tasks(limit=2);self.assertTrue(page['has_more'])
        last=app.list_tasks(limit=2,cursor=page['next_cursor'])
        self.assertEqual(ids,{x['task_id'] for x in page['tasks']+last['tasks']})
        self.assertFalse(last['has_more'])
        for task in ids:app.get_task(task);app.get_attempts(task)
        self.assertEqual(before,(self.root/'tasks.sqlite3').read_bytes())

    def test_cross_task_artifact_reference_is_refused_before_content_read(self):
        from dataclasses import replace
        from agent.adult.fixed_free_text_application import task_result_chunk
        from agent.adult.payload_store import PayloadNotFound
        app=self.application()
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}):
            task=app.submit_text_task('text.analyze','Safe sample.',retain_payload=True)['task_id']
        other=app._service.artifacts.save_output('other-task','other-attempt',b'Other private result.','text/plain')
        forged=replace(self.tasks.get_task(task),status='completed',latest_result_artifact_id=other.artifact_id)
        with self.assertRaises(PayloadNotFound):task_result_chunk(forged,app._service.artifacts)

    def test_default_ephemeral_stages_without_environment_key_or_persistent_input(self):
        app=self.application()
        with patch.dict('os.environ',{},clear=True),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            task=app.submit_text_task('text.analyze','Transient private source.')['task_id']
            request=self.tasks.get_task_request(task)
            self.assertEqual('ephemeral',request.retention_policy)
            self.assertEqual('checkpointed',app.get_task(task)['status'])
            factory.assert_not_called()
        with self.artifacts._connection() as db:
            self.assertEqual(0,db.execute("SELECT COUNT(*) FROM adult_payload_records").fetchone()[0])
        self.assertIn(b'Transient private source.',self.artifacts.load_input(request.payload_id,task))
        for file in self.root.rglob('*'):
            if file.is_file():self.assertNotIn(b'Transient private source.',file.read_bytes())

    def test_ephemeral_same_process_fake_completion_and_no_replay(self):
        app=self.application();self.enable_route(app._service)
        fake=Mock();fake.invoke.return_value=SimpleNamespace(content='Safe fake result.',usage_metadata={},response_metadata={})
        with patch('agent.adult.integrations.omniroute_chat.build_chat_model',return_value=fake):
            task=app.submit_text_task('text.analyze','Transient source.')['task_id']
            self.assertEqual('completed',app.run_task(task)['task']['status'])
            self.assertEqual(b'Safe fake result.',base64.b64decode(app.get_result_chunk(task)['data_base64']))
            self.assertEqual('completed',app.run_task(task)['task']['status'])
            self.assertEqual(1,fake.invoke.call_count)

    def test_ephemeral_process_cache_loss_requires_input_and_zero_attempts(self):
        from agent.adult.fixed_free_text_service import FixedFreeTextService
        from agent.adult.payload_store import AdultArtifactStore
        app=self.application();self.enable_route(app._service)
        task=app.submit_text_task('text.analyze','Transient source.')['task_id']
        reopened=FixedFreeTextService(self.tasks,AdultArtifactStore(self.root/'retained',cipher=self.cipher),
                                      self.cipher,app._service.config)
        other=FixedFreeTextApplication(reopened,budget=TaskBudget(max_provider_calls=1))
        with patch('agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            outcome=other.run_task(task)
            self.assertEqual('needs_input',outcome['task']['status'])
            self.assertTrue(outcome['task']['user_action_required'])
            self.assertEqual('input_required',outcome['error_code'])
            self.assertEqual([],other.get_attempts(task));factory.assert_not_called()
        self.assertEqual('cancelled',other.cancel_task(task)['status'])

    def test_actual_new_process_reports_ephemeral_loss_without_provider_configuration(self):
        import os,subprocess,sys
        app=self.application()
        task=app.submit_text_task('text.analyze','Ephemeral process private source.')['task_id']
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
first=service.run(os.environ['TEST_TASK']);second=service.run(os.environ['TEST_TASK'])
print(json.dumps({'first':first.status,'second':second.status,
    'attempts':service.tasks.get_task(first.task_id).attempt_count}))
"""
        result=subprocess.run([sys.executable,'-c',code],env={**os.environ,
            'TEST_ROOT':str(self.root),'TEST_KEY':self.key,'TEST_TASK':task},capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual({'first':'needs_input','second':'needs_input','attempts':0},json.loads(result.stdout))
