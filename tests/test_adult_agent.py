"""Mixed modality internal application over shared real stores. No live AI."""
import base64
import unittest
from unittest.mock import patch
from tests import test_adult_text_executor as text_fixtures
from tests import test_adult_image_executor as image_fixtures
from agent.adult.adult_agent import AdultAgent
from agent.adult.bounded_text_application import BoundedTextApplication
from agent.adult.bounded_image_application import BoundedImageApplication
from agent.adult.models import TaskBudget


class AdultAgentTests(unittest.TestCase):
    setUp = text_fixtures.ExecutorTests.setUp
    enable_text = text_fixtures.ExecutorTests.enable

    def agent(self):
        helper = object.__new__(image_fixtures.ExecutorTests)
        helper.tasks, helper.artifacts, helper.cipher = self.tasks, self.artifacts, self.cipher
        helper.root = self.root
        helper.adapter = image_fixtures.Adapter()
        helper.image = image_fixtures.png_bytes()
        self.image_helper = helper
        self.image_executor = helper.compose(self.tasks, self.artifacts)
        text = BoundedTextApplication(self.executor, text_fixtures.CONFIG, budget=TaskBudget())
        image = BoundedImageApplication(self.image_executor, image_fixtures.CONFIG, budget=TaskBudget())
        return AdultAgent(text, image)

    def test_mixed_task_list_routes_both_modalities_without_payload(self):
        app = self.agent()
        a = app.submit_text_task('text.analyze', 'private text')['task_id']
        b = app.submit_image_task('image.generate', 'a red square')['task_id']
        page = app.list_tasks(limit=1)
        self.assertTrue(page['has_more'])
        other = app.list_tasks(limit=1, cursor=page['next_cursor'])
        self.assertEqual({a,b}, {page['tasks'][0]['task_id'],other['tasks'][0]['task_id']})
        self.assertNotIn('payload_id', str(page))
        self.assertNotIn('input_fingerprint', str(page))
        self.assertEqual('checkpointed', app.get_task(a)['status'])
        self.assertEqual(0,self.adapter.calls)
        self.assertEqual(0,sum(self.image_helper.adapter.calls.values()))

    def test_complete_both_modalities_read_results_and_never_replay(self):
        app = self.agent();self.enable_text();self.image_helper.enable('image.generate')
        text = app.submit_text_task('text.analyze','private text')['task_id']
        image = app.submit_image_task('image.generate','a red square')['task_id']
        for task in (text,image):
            result = app.run_task(task,max_attempts=1)
            self.assertEqual('completed',result['task']['status'])
            self.assertEqual('input_removed',result['input_cleanup']['status'])
            self.assertEqual(1,len(app.get_attempts(task)))
            app.run_task(task)
        self.assertIn(b'Not AI inference',base64.b64decode(app.get_result_chunk(text)['data_base64']))
        self.assertEqual(image_fixtures.png_bytes(4,4),base64.b64decode(app.get_result_chunk(image)['data_base64']))
        self.assertEqual(1,self.adapter.calls)
        self.assertEqual(1,self.image_helper.adapter.calls['generate'])

    def test_mime_refused_and_optional_analysis_prompt_supported(self):
        app = self.agent();image = image_fixtures.png_bytes()
        with self.assertRaises(ValueError):
            app.submit_image_task('image.edit','make it blue',image,'image/jpeg')
        with self.assertRaises(ValueError):
            app.submit_image_task('image.generate','a red square',mime_type='image/png')
        self.assertEqual(0,len(app.list_tasks()['tasks']))
        task = app.submit_image_task('image.analyze','',image,'image/png')['task_id']
        self.assertEqual('checkpointed',app.get_task(task)['status'])
        prompted = app.submit_image_task('image.analyze','read the sign',image,'image/png')['task_id']
        self.assertEqual('checkpointed',app.get_task(prompted)['status'])

    def test_cancel_dispatches_neither_modality_and_metadata_reads_are_readonly(self):
        app = self.agent()
        tasks=[app.submit_text_task('text.analyze','private text')['task_id'],
               app.submit_image_task('image.generate','a red square')['task_id']]
        for task in tasks:self.assertEqual('cancelled',app.cancel_task(task)['task']['status'])
        before=(self.root/'tasks.sqlite3').read_bytes()
        app.list_tasks()
        for task in tasks:app.get_task(task);app.get_attempts(task)
        self.assertEqual(before,(self.root/'tasks.sqlite3').read_bytes())
        self.assertEqual(0,self.adapter.calls)
        self.assertEqual(0,sum(self.image_helper.adapter.calls.values()))

    def test_cross_lane_restore_generic_replacement_and_invalid_bounds_fail_closed(self):
        app=self.agent()
        text=app.submit_text_task('text.analyze','private text')['task_id']
        image=app.submit_image_task('image.generate','a red square')['task_id']
        before=(self.root/'tasks.sqlite3').read_bytes()
        for action in (lambda:app.restore_text_input(image,'private text'),
                       lambda:app.restore_image_input(text,prompt='a red square'),
                       lambda:app.resume_task(text,'continue',replacement_payload='new text'),
                       lambda:app.run_task(text,max_attempts=True),
                       lambda:app.list_tasks(limit=21)):
            with self.assertRaises(ValueError):action()
        self.assertEqual(before,(self.root/'tasks.sqlite3').read_bytes())

    def test_shared_store_and_key_budget_bindings_are_required(self):
        app=self.agent()
        with self.assertRaises(TypeError):AdultAgent(None,app.image)
        with patch.object(app.image,'tasks',object()):
            with self.assertRaises(ValueError):AdultAgent(app.text,app.image)
        with patch.object(app.image,'artifacts',object()):
            with self.assertRaises(ValueError):AdultAgent(app.text,app.image)
        with patch.object(app.image,'budget',TaskBudget(max_total_attempts=1)):
            with self.assertRaises(ValueError):AdultAgent(app.text,app.image)

    def test_exact_lost_input_restore_routes_both_modalities(self):
        app=self.agent();self.enable_text();self.image_helper.enable('image.generate')
        text=app.submit_text_task('text.analyze','private text')['task_id']
        image=app.submit_image_task('image.generate','a red square')['task_id']
        self.artifacts.cache.discard(text);self.artifacts.cache.discard(image)
        for task in (text,image):self.assertEqual('needs_input',app.run_task(task)['task']['status'])
        with self.assertRaises(ValueError):app.restore_text_input(text,'changed text')
        with self.assertRaises(ValueError):app.restore_image_input(image,prompt='a blue square')
        self.assertEqual('completed',app.restore_text_input(text,'private text')['task']['status'])
        self.assertEqual('completed',app.restore_image_input(image,prompt='a red square')['task']['status'])

    def test_internal_continue_routes_without_inventing_approval(self):
        app=self.agent();self.enable_text();self.image_helper.enable('image.generate')
        tasks=[app.submit_text_task('text.analyze','private text')['task_id'],
               app.submit_image_task('image.generate','a red square')['task_id']]
        for task in tasks:
            self.assertEqual('completed',app.resume_task(task,'continue',max_attempts=1)['task']['status'])
            self.assertEqual(0,self.tasks.get_task(task).approvals_granted)
            with self.assertRaises(ValueError):app.resume_task(task,'continue')
