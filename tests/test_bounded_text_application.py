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
