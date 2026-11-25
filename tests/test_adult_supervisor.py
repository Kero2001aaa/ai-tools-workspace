"""Actual bounded pipeline failures and durable wait/resume, no live service."""
from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import patch

from agent.adult.adult_supervisor import AdultSupervisor
from agent.adult.models import CapabilityEvidence, ModelCapabilitySpec, TaskBudget
from agent.adult.integrations.protocols import TextAdapterResult
from tests import test_adult_text_executor as fixtures
CONFIG = fixtures.CONFIG


class TimeoutAdapter:
    def __init__(self):self.calls=0
    def invoke(self, request):
        self.calls+=1
        raise TimeoutError()


class SupervisorTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp

    def routes(self, count, succeed_last=False):
        self.adapters=[]
        for index in range(count):
            name=f'offline-supervised-{index}'
            adapter=TimeoutAdapter()
            if succeed_last and index==count-1:
                adapter=fixtures.Adapter()
            self.adapters.append(adapter)
            self.pipeline._adapters[(name,'1')]=adapter
            spec=ModelCapabilitySpec(name,'1','text.analyze',('text',),('text',),'local')
            record=self.registry.register(spec);now=datetime.now(timezone.utc)
            evidence=CapabilityEvidence(capability_source='local_adapter_smoke_test',
                capability_reference='test:supervisor-local',verified_at=now.isoformat().replace('+00:00','Z'),
                expires_at=(now+timedelta(hours=1)).isoformat().replace('+00:00','Z'),
                adapter_id=name,adapter_version='1',task_type='text.analyze',input_modalities=('text',),output_modalities=('text',))
            record=self.registry.verify(record.capability_id,evidence,record.state_version)
            self.registry.enable(record.capability_id,record.state_version)
        self.router._local_adapter_available=lambda name:name.startswith('offline-supervised-')
        self.supervisor=AdultSupervisor(self.executor)

    def test_exact_five_wait_reopen_explicit_resume_then_sixth_success_no_replay(self):
        self.routes(6,succeed_last=True);task=self.executor.create('text.analyze',self.data)
        result=self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data)
        self.assertEqual('waiting_user',result.status)
        current=self.tasks.get_task(task.task_id)
        self.assertEqual((5,5,True),(current.attempt_count,current.failed_attempts_since_approval,current.user_action_required))
        self.assertEqual([1,1,1,1,1,0],[a.calls for a in self.adapters])
        checkpoint=self.tasks.get_latest_checkpoint(task.task_id)
        self.assertEqual('approval_required',checkpoint.reason)
        self.assertEqual(5,len(checkpoint.snapshot.excluded_approach_keys))
        self.assertEqual('waiting_user',self.supervisor.run_until_done(task.task_id,CONFIG).status)
        with self.assertRaises(ValueError):self.supervisor.continue_after_approval(task.task_id,CONFIG)
        # Synthetic explicit decision validates software behavior, not owner consent.
        self.tasks.resume(task.task_id,'continue')
        result=AdultSupervisor(self.executor).continue_after_approval(task.task_id,CONFIG)
        self.assertEqual('completed',result.status)
        self.assertEqual([1]*6,[a.calls for a in self.adapters])
        self.assertEqual((6,1),(self.tasks.get_task(task.task_id).attempt_count,self.tasks.get_task(task.task_id).approvals_granted))
        self.assertEqual(result,self.supervisor.run_until_done(task.task_id,CONFIG))

    def test_segment_limit_checkpoint_then_distinct_next_route(self):
        self.routes(3,succeed_last=True);task=self.executor.create('text.analyze',self.data)
        first=self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data,max_attempts=1)
        self.assertEqual('checkpointed',first.status)
        self.assertEqual('completed',self.supervisor.run_until_done(task.task_id,CONFIG).status)
        self.assertEqual([1,1,1],[a.calls for a in self.adapters])

    def test_no_route_pauses_zero_attempt_and_no_generic_resume(self):
        self.routes(0);task=self.executor.create('text.analyze',self.data)
        self.assertEqual('paused',self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data).status)
        self.assertEqual(0,self.tasks.get_task(task.task_id).attempt_count)
        self.assertEqual('paused',self.supervisor.run_until_done(task.task_id,CONFIG).status)

    def test_total_attempt_cap_on_fifth_fails_instead_of_wait(self):
        self.routes(6);task=self.executor.create('text.analyze',self.data,TaskBudget(max_total_attempts=5))
        result=self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data)
        self.assertEqual('failed',result.status)
        current=self.tasks.get_task(task.task_id)
        self.assertEqual(('failed',5,False),(current.status,current.attempt_count,current.user_action_required))
        self.assertEqual([1,1,1,1,1,0],[a.calls for a in self.adapters])

    def test_unknown_committed_attempt_is_not_replayed(self):
        self.routes(2);task=self.executor.create('text.analyze',self.data)
        class ProcessLoss(BaseException):pass
        with patch.object(self.adapters[0],'invoke',side_effect=ProcessLoss):
            with self.assertRaises(ProcessLoss):self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data)
        later=(datetime.now(timezone.utc)+timedelta(seconds=301)).isoformat().replace('+00:00','Z')
        with patch('agent.adult.adult_task_store._utc_now',return_value=later):self.tasks.recover_expired_leases()
        result=self.supervisor.run_until_done(task.task_id,CONFIG)
        self.assertEqual('prior_attempt_requires_recovery',result.error_code)
        self.assertEqual(0,self.adapters[1].calls)

    def test_invalid_segment_limits_rejected_before_task_change(self):
        self.routes(1);task=self.executor.create('text.analyze',self.data)
        for kwargs in ({'max_attempts':6},{'max_attempts':True},{'max_seconds':241},{'max_seconds':0}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                self.supervisor.run_until_done(task.task_id,CONFIG,**kwargs)
        self.assertEqual('queued',self.tasks.get_task(task.task_id).status)

    def test_nonretryable_storage_failure_stops_after_one(self):
        self.routes(2,succeed_last=True);task=self.executor.create('text.analyze',self.data)
        # First adapter produces accepted text but artifact write fails permanently.
        self.pipeline._adapters[('offline-supervised-0','1')]=fixtures.Adapter()
        with patch.object(self.artifacts,'save_output',side_effect=OSError):
            result=self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data)
        self.assertEqual('paused',result.status)
        self.assertEqual(1,self.tasks.get_task(task.task_id).attempt_count)
        self.assertEqual(0,self.adapters[1].calls)

    def test_cancel_between_distinct_attempts_prevents_next_call(self):
        self.routes(3);task=self.executor.create('text.analyze',self.data)
        original=self.executor._checkpoint
        def checkpoint_then_cancel(current,*args,**kwargs):
            original(current,*args,**kwargs)
            if current.attempt_count==1:self.tasks.request_cancel(task.task_id)
        with patch.object(self.executor,'_checkpoint',side_effect=checkpoint_then_cancel):
            result=self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data)
        self.assertEqual('cancelled',result.status)
        self.assertEqual([1,0,0],[a.calls for a in self.adapters])

    def test_invalid_evaluation_is_nonretryable_and_never_routes_around(self):
        from agent.adult.models import PolicyDecision
        self.routes(2);task=self.executor.create('text.analyze',self.data)
        self.pipeline._adapters[('offline-supervised-0','1')]=fixtures.Adapter()
        with patch.object(self.pipeline._evaluator,'evaluate',return_value=None):
            result=self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data)
        self.assertEqual('paused',result.status)
        self.assertEqual(0,self.adapters[1].calls)
        self.assertEqual(1,self.tasks.get_task(task.task_id).attempt_count)

    def test_wall_segment_yields_without_second_attempt(self):
        self.routes(3);task=self.executor.create('text.analyze',self.data)
        with patch('agent.adult.adult_text_executor.time.monotonic',side_effect=[0,2]):
            result=self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data,max_seconds=1)
        self.assertEqual('checkpointed',result.status)
        self.assertEqual([1,0,0],[a.calls for a in self.adapters])

    def test_five_failure_wait_recovery_before_approval_still_no_sixth(self):
        self.routes(6);task=self.executor.create('text.analyze',self.data)
        original=self.executor._checkpoint
        class ProcessLoss(BaseException):pass
        def crash_before_wait(*args,**kwargs):
            if kwargs.get('waiting'):raise ProcessLoss()
            return original(*args,**kwargs)
        with patch.object(self.executor,'_checkpoint',side_effect=crash_before_wait):
            with self.assertRaises(ProcessLoss):self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data)
        later=(datetime.now(timezone.utc)+timedelta(seconds=301)).isoformat().replace('+00:00','Z')
        with patch('agent.adult.adult_task_store._utc_now',return_value=later):self.tasks.recover_expired_leases()
        result=self.supervisor.run_until_done(task.task_id,CONFIG)
        self.assertEqual('waiting_user',result.status)
        self.assertEqual([1,1,1,1,1,0],[a.calls for a in self.adapters])

    def test_output_policy_rejection_is_terminal_and_no_other_route_runs(self):
        from agent.adult.models import EvaluationResult
        self.routes(2);task=self.executor.create('text.analyze',self.data)
        self.pipeline._adapters[('offline-supervised-0','1')]=fixtures.Adapter()
        denied=EvaluationResult(False,0.0,(),('policy_rejected',),False)
        with patch.object(self.pipeline._evaluator,'evaluate',return_value=denied):
            result=self.supervisor.run_until_done(task.task_id,CONFIG,canonical_input=self.data)
        self.assertEqual('failed',result.status)
        self.assertEqual(1,self.tasks.get_task(task.task_id).attempt_count)
        self.assertEqual(0,self.adapters[1].calls)
