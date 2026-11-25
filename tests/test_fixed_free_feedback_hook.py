"""Opt-in private proposal hook never publishes or changes task outcome."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from agent.adult.adult_feedback import AdultFeedback
from agent.adult.adult_memory import AdultMemory
from agent.adult.fixed_free_text_application import FixedFreeTextApplication
from agent.adult.models import TaskBudget
from agent.memory_store import MemoryStore
from tests import test_fixed_free_text_service as fixtures


class FeedbackHookTests(unittest.TestCase):
    setUp=fixtures.FixedServiceTests.setUp
    service=fixtures.FixedServiceTests.service
    enable_route=fixtures.FixedServiceTests.enable_route

    def application(self,enabled=True):
        service=self.service();self.enable_route(service)
        memory=MemoryStore(self.root/'memory.sqlite3');self.memory=AdultMemory(memory.adult_scope())
        self.feedback=AdultFeedback(self.tasks,service.tracker,memory.feedback_publisher_scope(),self.memory,self.cipher)
        return FixedFreeTextApplication(service,budget=TaskBudget(max_provider_calls=1),
            feedback=self.feedback if enabled else None)

    def run_fake(self,app,output):
        client=Mock();client.invoke.return_value=SimpleNamespace(content=output,usage_metadata={},response_metadata={})
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model',return_value=client):
            task=app.submit_text_task('text.analyze','Safe private hook context.',retain_payload=True)['task_id']
            result=app.run_task(task)
        return task,result,client

    def test_invalid_output_builds_private_proposal_and_explicit_publish_only(self):
        app=self.application();task,result,client=self.run_fake(app,'')
        self.assertEqual('failed',result['task']['status'])
        self.assertEqual('private_proposal_created',result['feedback']['status'])
        proposal=result['feedback']['private_proposal_id']
        self.assertEqual('proposed',self.feedback.publication_status(proposal)['status'])
        self.assertEqual([],self.memory.recent_shareable_workspace())
        self.assertEqual(1,len(self.memory.recent_private()))
        app.run_task(task)
        self.assertEqual(1,len(self.memory.recent_private()));self.assertEqual(1,client.invoke.call_count)
        published=self.feedback.publish(proposal)
        self.assertEqual(published.id,self.memory.recent_shareable_workspace()[0].id)
        self.assertNotIn('Safe private hook context.',json.dumps(result))

    def test_feedback_write_failure_does_not_mask_completed_task_or_result(self):
        app=self.application()
        with patch.object(self.feedback,'build_proposal',side_effect=OSError('synthetic private failure')):
            task,result,client=self.run_fake(app,'Safe mock output.')
        self.assertEqual('completed',result['task']['status'])
        self.assertEqual('proposal_failed',result['feedback']['status'])
        self.assertTrue(app.get_result_chunk(task)['complete'])
        self.assertEqual('completed',app.run_task(task)['task']['status'])
        self.assertEqual(1,client.invoke.call_count)
        self.assertEqual([],self.memory.recent_shareable_workspace())

    def test_default_disabled_and_zero_attempt_pause_do_not_build_or_publish(self):
        app=self.application(False);task,result,_=self.run_fake(app,'Safe mock output.')
        self.assertNotIn('feedback',result);self.assertEqual([],self.memory.recent_private())
        app=FixedFreeTextApplication(app._service,budget=TaskBudget(max_provider_calls=1),feedback=self.feedback)
        # Existing service with routing withheld must remain a zero-call pause.
        with patch.object(app._service.registry,'list_eligible',return_value=[]),patch.object(self.feedback,'build_proposal') as build:
            with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}):
                task=app.submit_text_task('text.analyze','Safe sample.',retain_payload=True)['task_id']
                self.assertEqual('not_applicable',app.run_task(task)['feedback']['status'])
            build.assert_not_called()
        self.assertEqual([],self.memory.recent_shareable_workspace())

    def test_feedback_must_share_same_store_and_key(self):
        app=self.application()
        from agent.adult.adult_task_store import AdultTaskStore
        other=AdultTaskStore(self.root/'other.sqlite3')
        with patch.object(self.feedback,'tasks',other):
            with self.assertRaises(ValueError):FixedFreeTextApplication(app._service,budget=TaskBudget(max_provider_calls=1),feedback=self.feedback)
