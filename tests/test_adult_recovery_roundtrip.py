"""Actual store/attempt approval roundtrip. No provider or user decision is sent."""
import tempfile
from pathlib import Path
from datetime import datetime,timezone
import unittest
from agent.adult.adult_attempt_tracker import AdultAttemptTracker,AttemptApprovalRequired,DuplicateApproach
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.models import AdultTaskRequest,TaskBudget,RouteCandidate,CheckpointSnapshot

INPUT='hmac-sha256:0123456789abcdef0123456789abcdef'
CONFIG='hmac-sha256:abcdef0123456789abcdef0123456789'


def route(index):
    return RouteCandidate(adapter_id=f'offline-adapter-{index}',adapter_version='1',provider_id='mock-provider',model_id='mock-model',model_version='1',tool_id=None,tool_version=None,task_type='text.analyze',cost_class='free_verified',capability_version='cap-v1',verified_at=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'))


class AdultRecoveryRoundtripTests(unittest.TestCase):
    def test_five_real_failures_checkpoint_reopen_continue_and_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'adult.sqlite3'
            store=AdultTaskStore(db)
            task=store.create_task(AdultTaskRequest('text.analyze',INPUT),TaskBudget(max_total_attempts=20,max_provider_calls=20))
            lease=store.claim_task(task.task_id,'offline-worker',lease_seconds=300)
            tracker=AdultAttemptTracker(store)
            for index in range(5):
                handle=tracker.begin(task.task_id,task.current_step_id,route(index),INPUT,CONFIG)
                tracker.fail(handle,'PROVIDER_ERROR','Transient backend failure.',True)
            reopened=AdultTaskStore(db);restarted=AdultAttemptTracker(reopened)
            with self.assertRaises(AttemptApprovalRequired):restarted.begin(task.task_id,task.current_step_id,route(5),INPUT,CONFIG)
            current=reopened.get_task(task.task_id)
            snapshot=CheckpointSnapshot(schema_version=1,task_id=task.task_id,task_type=task.task_type,status='waiting_user',active_step_id=task.current_step_id,input_fingerprint=INPUT,plan=(task.current_step_id,),completed_step_ids=(),pending_step_ids=(task.current_step_id,),total_attempts=current.attempt_count,failed_attempts_since_approval=5,provider_calls=current.provider_calls,spent_cost_microusd=0,remaining_attempts=15,remaining_wall_seconds=current.max_wall_seconds,remaining_provider_calls=15,remaining_cost_microusd=0,pending_user_action='approval_required',allowed_decisions=('continue','cancel'))
            checkpoint=reopened.set_waiting_user(task.task_id,snapshot,lease,current.state_version)
            self.assertEqual('approval_required',checkpoint.reason)
            waiting=AdultTaskStore(db).get_task(task.task_id)
            self.assertEqual(('waiting_user',5,1),(waiting.status,waiting.failed_attempts_since_approval,waiting.user_action_required))
            # Explicit synthetic decision tests API semantics only, not owner authority.
            resumed=AdultTaskStore(db).resume(task.task_id,'continue')
            self.assertEqual(('queued',5,0,1),(resumed.status,resumed.attempt_count,resumed.failed_attempts_since_approval,resumed.approvals_granted))
            self.assertEqual(5,AdultAttemptTracker(AdultTaskStore(db)).total_attempts(task.task_id))
            next_store=AdultTaskStore(db);next_store.claim_task(task.task_id,'offline-worker-2',lease_seconds=300)
            next_tracker=AdultAttemptTracker(next_store)
            with self.assertRaises(DuplicateApproach):next_tracker.begin(task.task_id,task.current_step_id,route(0),INPUT,CONFIG)
            sixth=next_tracker.begin(task.task_id,task.current_step_id,route(5),INPUT,CONFIG)
            self.assertEqual(6,sixth.attempt_no)
            next_tracker.fail(sixth,'PROVIDER_ERROR','Transient backend failure.',True)
            final=AdultTaskStore(db).get_task(task.task_id)
            self.assertEqual((6,1,1),(final.attempt_count,final.failed_attempts_since_approval,final.approvals_granted))
