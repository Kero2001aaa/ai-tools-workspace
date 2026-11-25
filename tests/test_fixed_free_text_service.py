"""Fixed-free application composition with fake client, never live inference."""
import base64
from datetime import datetime,timedelta,timezone
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

from agent.adult.fixed_free_text_service import FixedFreeTextService
from agent.adult.integrations.omniroute_chat import AdultOmniRouteConfig
from agent.adult.models import ModelCapabilitySpec,CapabilityEvidence,TaskBudget
from agent.adult.payload_store import PayloadCipher,AdultArtifactStore
from tests import test_adult_text_executor as fixtures


class FixedServiceTests(unittest.TestCase):
    setUp=fixtures.ExecutorTests.setUp

    def service(self):
        self.key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        self.cipher=PayloadCipher(self.key)
        self.artifacts=AdultArtifactStore(self.root/'retained',cipher=self.cipher)
        return FixedFreeTextService(self.tasks,self.artifacts,self.cipher,
            AdultOmniRouteConfig('https://openrouter.ai/api/v1','synthetic-unit-test-value'))

    def enable_route(self,service,model='openrouter/free',provider='openrouter'):
        spec=ModelCapabilitySpec('fixed-free-text','1','text.analyze',('text',),('text',),
            'free_verified',provider_id=provider,model_id=model)
        rec=service.registry.register(spec);now=datetime.now(timezone.utc);end=now+timedelta(hours=1)
        # Synthetic offline evidence exercises schema, not live certification.
        e=CapabilityEvidence(capability_source='official_provider_docs',
            capability_reference='https://example.com/synthetic-docs',verified_at=now.isoformat().replace('+00:00','Z'),
            expires_at=end.isoformat().replace('+00:00','Z'),adapter_id=spec.adapter_id,
            adapter_version='1',task_type='text.analyze',input_modalities=('text',),output_modalities=('text',),
            provider_id=provider,model_id=model,cost_source='official_provider_pricing',
            cost_reference='https://example.com/synthetic-pricing',cost_verified_at=now.isoformat().replace('+00:00','Z'),
            cost_expires_at=end.isoformat().replace('+00:00','Z'),cost_is_zero=True)
        rec=service.registry.verify(rec.capability_id,e,rec.state_version)
        return service.registry.enable(rec.capability_id,rec.state_version)

    def test_submit_no_call_run_fake_client_and_reopen_result_no_duplicate(self):
        service=self.service();self.enable_route(service)
        fake=Mock();fake.invoke.return_value=SimpleNamespace(content='A structurally accepted fake result.',usage_metadata={},response_metadata={})
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model',return_value=fake) as factory:
            staged=service.submit('text.analyze','Safe private context.',budget=TaskBudget(max_provider_calls=1))
            factory.assert_not_called();self.assertEqual('checkpointed',staged.status)
            result=service.run(staged.task_id)
            self.assertEqual('completed',result.status);self.assertEqual(1,fake.invoke.call_count)
            reopened=FixedFreeTextService(self.tasks,AdultArtifactStore(self.root/'retained',cipher=self.cipher),
                self.cipher,service.config)
            self.assertEqual(result,reopened.run(staged.task_id))
            self.assertEqual(1,fake.invoke.call_count)
        self.assertIn(b'fake result',self.artifacts.read_chunk(result.artifact_id,0).data)
        current=self.tasks.get_task(staged.task_id)
        self.assertEqual((1,1,0),(current.attempt_count,current.provider_calls,current.spent_cost_microusd))

    def test_no_evidence_no_route_no_call_and_no_autocertification(self):
        service=self.service()
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            staged=service.submit('text.analyze','Safe context.',budget=TaskBudget(max_provider_calls=1))
            result=service.run(staged.task_id)
            factory.assert_not_called()
        self.assertEqual('paused',result.status)
        self.assertEqual(0,self.tasks.get_task(staged.task_id).attempt_count)
        with self.tasks._connect() as db:
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM adult_model_capabilities').fetchone()[0])

    def test_other_model_even_with_synthetic_evidence_never_calls(self):
        service=self.service();self.enable_route(service,model='different-model')
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            staged=service.submit('text.analyze','Safe context.',budget=TaskBudget(max_provider_calls=1))
            self.assertEqual('paused',service.run(staged.task_id).status);factory.assert_not_called()

    def test_endpoint_and_provider_budget_are_explicit(self):
        service=self.service()
        with self.assertRaises(ValueError):FixedFreeTextService(self.tasks,self.artifacts,self.cipher,
            AdultOmniRouteConfig('https://example.com/v1','synthetic'))
        with self.assertRaises(ValueError):service.submit('text.analyze','safe',budget=TaskBudget())
        self.assertEqual((),self.tasks.list_tasks().items)

    def test_literal_wire_model_roundtrips_all_persisted_metadata_without_broadening_ids(self):
        from dataclasses import replace
        from agent.adult.models import RouteCandidate,AdultModelValidationError
        service=self.service();self.enable_route(service)
        fake=Mock();fake.invoke.return_value=SimpleNamespace(content='Safe fake output.',usage_metadata={},response_metadata={})
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model',return_value=fake):
            staged=service.submit('text.analyze','Safe context.',budget=TaskBudget(max_provider_calls=1))
            self.assertEqual('completed',service.run(staged.task_id).status)
        task=self.tasks.get_task(staged.task_id)
        self.assertEqual('openrouter/free',task.selected_model)
        self.assertEqual('openrouter/free',self.tasks.get_latest_checkpoint(staged.task_id).snapshot.selected_model)
        self.assertEqual('openrouter/free',service.tracker.list_summaries(staged.task_id)[0].model_id)
        for value in ('owner/model','../openrouter/free','openrouter/free/extra','https://openrouter/free','/tmp/private'):
            with self.subTest(value=value),self.assertRaises(AdultModelValidationError):
                ModelCapabilitySpec('fixed-free-text','1','text.analyze',('text',),('text',),
                    'free_verified',provider_id='openrouter',model_id=value)
        with self.assertRaises(AdultModelValidationError):
            ModelCapabilitySpec('openrouter/free','1','text.analyze',('text',),('text',),'local')

    def test_provider_failure_not_retried_and_disabled_evidence_never_calls(self):
        service=self.service();record=self.enable_route(service)
        fake=Mock();fake.invoke.side_effect=RuntimeError('synthetic provider failure')
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model',return_value=fake):
            staged=service.submit('text.analyze','Safe context.',budget=TaskBudget(max_provider_calls=1))
            self.assertEqual('failed',service.run(staged.task_id).status)
            self.assertEqual(1,fake.invoke.call_count)
        service.registry.disable(record.capability_id,"operator_disabled",record.state_version)
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            staged=service.submit('text.analyze','Safe context.',budget=TaskBudget(max_provider_calls=1))
            self.assertEqual('paused',service.run(staged.task_id).status)
            factory.assert_not_called()

    def test_wrong_key_or_multi_attempt_request_does_not_mutate_staged_task(self):
        service=self.service()
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}):
            staged=service.submit('text.analyze','Safe context.',budget=TaskBudget(max_provider_calls=1))
        for count in (2,5,True):
            with self.assertRaises(ValueError):service.run(staged.task_id,max_attempts=count)
        wrong=PayloadCipher(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        other=FixedFreeTextService(self.tasks,AdultArtifactStore(self.root/'retained',cipher=wrong),wrong,service.config)
        with self.assertRaises(Exception):other.run(staged.task_id)
        task=self.tasks.get_task(staged.task_id)
        self.assertEqual(('checkpointed',0),(task.status,task.attempt_count))

    def test_disable_between_route_selection_and_attempt_commit_makes_zero_calls(self):
        service=self.service();record=self.enable_route(service)
        original=service.tracker.begin
        def revoke_then_begin(*args,**kwargs):
            service.registry.disable(record.capability_id,'operator_disabled',record.state_version)
            return original(*args,**kwargs)
        with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
            'agent.adult.integrations.omniroute_chat.build_chat_model') as factory:
            staged=service.submit('text.analyze','Safe context.',budget=TaskBudget(max_provider_calls=1))
            with patch.object(service.tracker,'begin',side_effect=revoke_then_begin):
                self.assertEqual('paused',service.run(staged.task_id).status)
            factory.assert_not_called()
        task=self.tasks.get_task(staged.task_id)
        self.assertEqual((0,0),(task.attempt_count,task.provider_calls))

    def test_transactional_route_check_refuses_expired_or_changed_candidate(self):
        from dataclasses import replace
        from agent.adult.models import CapabilityQuery,CostPolicy
        service=self.service();self.enable_route(service)
        query=CapabilityQuery('text.analyze',('text',),'text',service.cipher.fingerprint(b'sample'),service._fingerprint('text.analyze'))
        route=service.registry.list_eligible(query,CostPolicy(1,0,False))[0]
        now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        future=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat().replace('+00:00','Z')
        with self.tasks._transaction() as db:
            self.assertTrue(service.registry.current_text_route_in_transaction(db,route,now))
            self.assertFalse(service.registry.current_text_route_in_transaction(db,route,future))
            self.assertFalse(service.registry.current_text_route_in_transaction(db,replace(route,capability_version='a'*64),now))
        with self.tasks._connect() as db:
            self.assertFalse(service.registry.current_text_route_in_transaction(db,route,now))

    def test_invalid_segment_limits_cannot_mutate_lost_ephemeral_state(self):
        service=self.service()
        task=service.submit('text.analyze','Safe transient context.',
             budget=TaskBudget(max_provider_calls=1),retain_payload=False).task_id
        lost=FixedFreeTextService(self.tasks,AdultArtifactStore(self.root/'retained',cipher=self.cipher),
                                  self.cipher,service.config)
        before=(self.root/'tasks.sqlite3').read_bytes()
        for kwargs in ({'max_attempts':1.0},{'max_attempts':True},{'max_seconds':True},
                       {'max_seconds':0},{'max_seconds':241},{'max_seconds':1.0}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):lost.run(task,**kwargs)
            self.assertEqual(before,(self.root/'tasks.sqlite3').read_bytes())
        self.assertEqual('checkpointed',self.tasks.get_task(task).status)
