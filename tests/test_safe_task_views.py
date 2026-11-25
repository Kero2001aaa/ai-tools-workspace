"""Actual bounded applications expose counters/usage without raw metadata."""
import unittest
from dataclasses import replace
from tests import test_bounded_text_application as text
from tests import test_bounded_image_approval as image
from agent.adult.adult_response_contracts import validate_response
from agent.adult.safe_task_views import attempt_summary


class SafeTaskViewTests(unittest.TestCase):
    def test_text_five_wait_and_continuation_counters_are_visible(self):
        helper=text.ApplicationTests();helper.setUp();self.addCleanup(helper.doCleanups)
        app=helper.app();task=app.submit_text_task('text.analyze','private source')['task_id']
        waiting=app.run_task(task)['task'];validate_response('task',waiting)
        self.assertEqual(5,waiting['failed_attempts_since_approval'])
        self.assertEqual(0,waiting['approvals_granted'])
        self.assertEqual(20,waiting['max_total_attempts'])
        completed=app.resume_task(task,'continue',max_attempts=1)['task'];validate_response('task',completed)
        self.assertEqual(1,completed['approvals_granted'])
        self.assertEqual(0,completed['failed_attempts_since_approval'])
        self.assertEqual(0,completed['spent_cost_microusd'])
        self.assertNotIn('private source',str(completed))
        self.assertNotIn('fingerprint',str(completed))

    def test_image_attempt_tool_usage_versions_and_failure_flags_visible(self):
        helper=image.ImageApprovalTests();helper.setUp();self.addCleanup(helper.doCleanups)
        app=helper.app();task=app.submit_image_task('image.generate',prompt='a square')['task_id']
        app.run_task(task,max_attempts=5)
        attempts=app.get_attempts(task);validate_response('attempts',{'attempts':attempts})
        self.assertEqual(5,len(attempts))
        for item in attempts:
            self.assertIn('usage',item);self.assertIn('tool_id',item);self.assertIn('model_version',item)
            self.assertTrue(item['retryable']);self.assertEqual('transient',item['failure_class'])
            self.assertNotIn('approach_key',item);self.assertNotIn('sanitized_error',item)
        snapshot=app.cancel_task(task)['task'];validate_response('task',snapshot)
        self.assertTrue(snapshot['cancel_requested'])

    def test_corrupt_usage_refused_and_output_copy_independent(self):
        helper=text.ApplicationTests();helper.setUp();self.addCleanup(helper.doCleanups)
        app=helper.app(1);task=app.submit_text_task('text.analyze','source')['task_id'];app.run_task(task,max_attempts=1)
        raw=helper.tracker.list_summaries(task)[0]
        for usage in ({'prompt':'private'},{'input_tokens':True},{'input_tokens':-1},{'input_tokens':10**12+1}):
            with self.assertRaises(ValueError):attempt_summary(replace(raw,usage=usage))
        safe=attempt_summary(replace(raw,usage={'input_tokens':3}));safe['usage'].clear()
        self.assertEqual({'input_tokens':3},attempt_summary(replace(raw,usage={'input_tokens':3}))['usage'])
