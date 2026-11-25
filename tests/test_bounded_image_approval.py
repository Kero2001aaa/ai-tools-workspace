"""Internal synthetic operator decisions, never real user consent evidence."""
import unittest
from unittest.mock import patch
from tests import test_adult_image_supervisor as fixtures
from agent.adult.bounded_image_application import BoundedImageApplication
from agent.adult.models import TaskBudget


class ImageApprovalTests(unittest.TestCase):
    setUp = fixtures.ImageSupervisorTests.setUp
    routes = fixtures.ImageSupervisorTests.routes

    def app(self, count=6, succeed_last=True):
        self.routes(count, succeed_last=succeed_last)
        return BoundedImageApplication(self.executor, fixtures.CONFIG, budget=TaskBudget())

    def test_five_failures_wait_then_explicit_continue_completes_and_cleans(self):
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        waiting = app.run_task(task, max_attempts=5)
        self.assertEqual('waiting_user', waiting['task']['status'])
        self.assertEqual([1,1,1,1,1,0], [a.calls for a in self.adapters])
        self.assertEqual('waiting_user', app.run_task(task)['task']['status'])
        result = app.resume_task(task, 'continue', max_attempts=1)
        self.assertEqual('completed', result['task']['status'])
        self.assertEqual('input_removed', result['input_cleanup']['status'])
        self.assertTrue(app.get_result_chunk(task)['complete'])
        self.assertEqual([1]*6, [a.calls for a in self.adapters])
        with self.assertRaises(ValueError):app.resume_task(task,'continue')

    def test_lost_decision_commit_response_never_dispatches_sixth(self):
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        app.run_task(task, max_attempts=5)
        original = self.tasks.resume
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('synthetic lost response')
        with patch.object(self.tasks, 'resume', side_effect=lost):
            with self.assertRaises(RuntimeError):app.resume_task(task, 'continue')
        self.assertEqual('queued', self.tasks.get_task(task).status)
        self.assertEqual([1,1,1,1,1,0], [a.calls for a in self.adapters])
        self.assertEqual('completed', app.run_task(task)['task']['status'])
        self.assertEqual([1]*6, [a.calls for a in self.adapters])

    def test_checkpoint_continue_and_cancel_do_not_invent_approval(self):
        app = self.app(3)
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        self.assertEqual('checkpointed', app.resume_task(task,'continue',max_attempts=1)['task']['status'])
        self.assertEqual(0, self.tasks.get_task(task).approvals_granted)
        result = app.resume_task(task,'cancel')
        self.assertEqual('cancelled', result['task']['status'])
        self.assertEqual('input_removed', result['input_cleanup']['status'])
        self.assertEqual([1,0,0], [a.calls for a in self.adapters])

    def test_invalid_bounds_and_decisions_preserve_wait_and_call_count(self):
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        app.run_task(task,max_attempts=5)
        before = (self.root/'tasks.sqlite3').read_bytes()
        for decision, kwargs in [('yes',{}),('continue',{'max_attempts':True}),
                                 ('continue',{'max_seconds':241}),('cancel',{'max_seconds':1.0})]:
            with self.assertRaises(ValueError):app.resume_task(task,decision,**kwargs)
            self.assertEqual(before, (self.root/'tasks.sqlite3').read_bytes())
        self.assertEqual([1,1,1,1,1,0], [a.calls for a in self.adapters])

    def test_paused_task_not_generically_resumed(self):
        app = self.app(0)
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        self.assertEqual('paused',app.run_task(task)['task']['status'])
        with self.assertRaises(ValueError):app.resume_task(task,'continue')
        self.assertEqual(0,self.tasks.get_task(task).attempt_count)
