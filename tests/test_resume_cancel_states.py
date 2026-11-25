"""Explicit cancellation from supported blocked states, no continuation grant."""
import unittest
from tests import test_adult_agent as fixtures


class ResumeCancelStateTests(unittest.TestCase):
    setUp=fixtures.AdultAgentTests.setUp
    agent=fixtures.AdultAgentTests.agent

    def test_paused_text_image_resume_cancel_no_calls(self):
        app=self.agent()
        tasks=[app.submit_text_task('text.analyze','private source')['task_id'],
               app.submit_image_task('image.generate','a square')['task_id']]
        for task in tasks:
            self.assertEqual('paused',app.run_task(task)['task']['status'])
            with self.assertRaises(ValueError):app.resume_task(task,'continue')
            result=app.resume_task(task,'cancel')
            self.assertEqual('cancelled',result['task']['status'])
            self.assertEqual('input_removed',result['input_cleanup']['status'])
            self.assertEqual(0,result['task']['approvals_granted'])
            self.assertEqual(0,result['task']['attempt_count'])
            with self.assertRaises(ValueError):app.resume_task(task,'continue')
        self.assertEqual(0,self.adapter.calls)
        self.assertEqual(0,sum(self.image_helper.adapter.calls.values()))

    def test_missing_ephemeral_input_cancel_needs_no_reupload(self):
        app=self.agent()
        tasks=[app.submit_text_task('text.analyze','private source')['task_id'],
               app.submit_image_task('image.analyze','question',self.image_helper.image,'image/png')['task_id']]
        for task in tasks:
            self.artifacts.cache.discard_task_kind(task,'input')
            self.assertEqual('needs_input',app.run_task(task)['task']['status'])
            result=app.resume_task(task,'cancel')
            self.assertEqual('cancelled',result['task']['status'])
            self.assertEqual(0,result['task']['attempt_count'])
            self.assertEqual(0,result['task']['approvals_granted'])
        self.assertEqual(0,self.adapter.calls)
        self.assertEqual(0,sum(self.image_helper.adapter.calls.values()))

    def test_invalid_cancel_replacement_or_bounds_do_not_mutate(self):
        app=self.agent();task=app.submit_text_task('text.analyze','source')['task_id']
        app.run_task(task);before=(self.root/'tasks.sqlite3').read_bytes()
        for kwargs in ({'replacement_payload':'unapproved replacement'},{'max_seconds':241},{'max_attempts':True}):
            with self.assertRaises(ValueError):app.resume_task(task,'cancel',**kwargs)
            self.assertEqual(before,(self.root/'tasks.sqlite3').read_bytes())
