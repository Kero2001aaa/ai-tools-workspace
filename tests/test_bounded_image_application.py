"""Internal image facade and terminal cleanup over real stores, offline only."""
import base64
import unittest
from unittest.mock import patch
import test_adult_image_executor as fixtures
from agent.adult.bounded_image_application import BoundedImageApplication
from agent.adult.models import TaskBudget
from agent.adult.payload_store import PayloadUnavailable


class BoundedImageApplicationTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    compose = fixtures.ExecutorTests.compose
    enable = fixtures.ExecutorTests.enable

    def app(self):
        return BoundedImageApplication(self.executor, fixtures.CONFIG, budget=TaskBudget())

    def test_submit_stages_without_dispatch_and_hides_input_metadata(self):
        self.enable('image.generate')
        result = self.app().submit_image_task('image.generate', prompt='a red square')
        self.assertEqual('checkpointed', result['status'])
        self.assertEqual(0, sum(self.adapter.calls.values()))
        self.assertNotIn('payload_id', result)
        self.assertNotIn('input_fingerprint', result)
        self.assertNotIn('a red square', str(result))

    def test_completed_cleanup_keeps_output_and_never_replays(self):
        self.enable('image.generate')
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        payload = self.tasks.get_task_request(task).payload_id
        result = app.run_task(task)
        self.assertEqual('completed', result['task']['status'])
        self.assertEqual('input_removed', result['input_cleanup']['status'])
        with self.assertRaises(PayloadUnavailable):
            self.artifacts.load_input(payload, task)
        chunk = app.get_result_chunk(task)
        self.assertEqual(fixtures.png_bytes(4, 4), base64.b64decode(chunk['data_base64']))
        self.assertEqual('image/png', chunk['mime_type'])
        app.run_task(task)
        self.assertEqual(1, sum(self.adapter.calls.values()))

    def test_cleanup_failure_does_not_hide_completion_and_explicit_rerun_retries(self):
        self.enable('image.generate')
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        with patch.object(self.artifacts, 'delete_task_inputs', side_effect=OSError()):
            result = app.run_task(task)
        self.assertEqual('completed', result['task']['status'])
        self.assertEqual('cleanup_failed', result['input_cleanup']['status'])
        self.assertTrue(app.get_result_chunk(task)['complete'])
        self.assertEqual('input_removed', app.run_task(task)['input_cleanup']['status'])
        self.assertEqual(1, sum(self.adapter.calls.values()))

    def test_cancel_staged_task_removes_all_task_inputs_without_dispatch(self):
        app = self.app()
        task = app.submit_image_task('image.edit', image=self.image, prompt='make it blue')['task_id']
        result = app.cancel_task(task)
        self.assertEqual('cancelled', result['task']['status'])
        self.assertEqual('input_removed', result['input_cleanup']['status'])
        self.assertEqual(0, self.artifacts.delete_task_inputs(task))
        self.assertEqual(0, sum(self.adapter.calls.values()))

    def test_get_is_read_only_even_when_cleanup_failed(self):
        self.enable('image.generate')
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        with patch.object(self.artifacts, 'delete_task_inputs', side_effect=OSError()):
            app.run_task(task)
        with patch.object(self.artifacts, 'delete_task_inputs') as cleanup:
            self.assertEqual('completed', app.get_task(task)['status'])
            cleanup.assert_not_called()

    def test_text_task_and_result_path_selection_are_not_exposed(self):
        from agent.adult.models import AdultTaskRequest
        task = self.tasks.create_task(AdultTaskRequest('text.analyze', self.cipher.fingerprint(b'x')),
                                      TaskBudget())
        app = self.app()
        for action in (app.get_task, app.run_task, app.cancel_task, app.get_result_chunk):
            with self.assertRaises(ValueError):
                action(task.task_id)
        self.assertEqual('queued', self.tasks.get_task(task.task_id).status)

    def test_retention_requires_real_boolean_before_creation(self):
        app = self.app()
        for value in (1, 0, 'true', None):
            with self.assertRaises(ValueError):
                app.submit_image_task('image.generate', prompt='a red square', retain_payload=value)
        self.assertEqual(0, len(self.tasks.list_tasks().items))

    def test_encrypted_cleanup_removes_retained_inputs_but_keeps_output(self):
        self.enable('image.edit')
        app = self.app()
        with patch.dict('os.environ', {'ADULT_PAYLOAD_ENCRYPTION_KEY': self.key}):
            task = app.submit_image_task('image.edit', image=self.image, prompt='make it blue',
                                         retain_payload=True)['task_id']
        result = app.run_task(task)
        self.assertEqual('completed', result['task']['status'])
        self.assertEqual('input_removed', result['input_cleanup']['status'])
        self.assertEqual(0, self.artifacts.delete_task_inputs(task))
        self.assertEqual(fixtures.png_bytes(4, 4), base64.b64decode(app.get_result_chunk(task)['data_base64']))

    def test_analysis_exposes_bounded_text_artifact_after_input_cleanup(self):
        self.enable('image.analyze')
        app = self.app()
        task = app.submit_image_task('image.analyze', image=self.image)['task_id']
        result = app.run_task(task)
        self.assertEqual('completed', result['task']['status'])
        self.assertEqual('input_removed', result['input_cleanup']['status'])
        chunk = app.get_result_chunk(task, 0, 8)
        self.assertEqual('text/plain', chunk['mime_type'])
        self.assertEqual(8, len(base64.b64decode(chunk['data_base64'])))
        self.assertFalse(chunk['complete'])
        with self.assertRaises(Exception):
            app.get_result_chunk(task, -1)

    def test_exact_restore_and_run_uses_application_terminal_cleanup(self):
        self.enable('image.generate')
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        from agent.adult.payload_store import AdultArtifactStore
        artifacts2 = AdultArtifactStore(self.root / 'artifacts', cipher=self.cipher)
        executor2 = self.compose(self.tasks, artifacts2)
        app2 = BoundedImageApplication(executor2, fixtures.CONFIG, budget=TaskBudget())
        self.assertEqual('needs_input', app2.run_task(task)['task']['status'])
        result = app2.restore_input_and_run(task, prompt='a red square')
        self.assertEqual('completed', result['task']['status'])
        self.assertEqual('input_removed', result['input_cleanup']['status'])
        self.assertTrue(app2.get_result_chunk(task)['complete'])
        self.assertEqual(1, sum(self.adapter.calls.values()))

    def test_cancel_during_adapter_cleans_input_without_publishing_output(self):
        self.enable('image.generate')
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        def cancel(request):
            self.tasks.request_cancel(task)
            return fixtures.ImageAdapterResult(fixtures.png_bytes(4, 4), 'image/png', {}, 0)
        with patch.object(self.adapter, 'generate', side_effect=cancel):
            result = app.run_task(task)
        self.assertEqual('cancelled', result['task']['status'])
        self.assertEqual('input_removed', result['input_cleanup']['status'])
        self.assertIsNone(self.tasks.get_task(task).latest_result_artifact_id)
        with self.assertRaises(ValueError):
            app.get_result_chunk(task)

    def test_nonterminal_executor_error_is_not_hidden(self):
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        with patch.object(app.supervisor, 'run_until_done', side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):
                app.run_task(task)
        self.assertEqual('checkpointed', self.tasks.get_task(task).status)

    def test_lost_restore_commit_response_stops_before_dispatch(self):
        self.enable('image.generate')
        app = self.app()
        task = app.submit_image_task('image.generate', prompt='a red square')['task_id']
        from agent.adult.payload_store import AdultArtifactStore
        artifacts2 = AdultArtifactStore(self.root / 'artifacts', cipher=self.cipher)
        executor2 = self.compose(self.tasks, artifacts2)
        app2 = BoundedImageApplication(executor2, fixtures.CONFIG, budget=TaskBudget())
        self.assertEqual('needs_input', app2.run_task(task)['task']['status'])
        from agent.adult.adult_task_restore import resume_restored_ephemeral
        def commit_then_lose(*args, **kwargs):
            resume_restored_ephemeral(*args, **kwargs)
            raise RuntimeError('lost response')
        with patch('agent.adult.adult_task_restore.resume_restored_ephemeral', side_effect=commit_then_lose):
            with self.assertRaises(RuntimeError):
                app2.restore_input_and_run(task, prompt='a red square')
        self.assertEqual('queued', self.tasks.get_task(task).status)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        self.assertEqual('completed', app2.run_task(task)['task']['status'])
        self.assertEqual(1, sum(self.adapter.calls.values()))

    def test_safe_bounded_list_and_attempt_history(self):
        self.enable('image.generate')
        app = self.app()
        ids = [app.submit_image_task('image.generate', prompt='a red square')['task_id']
               for _ in range(2)]
        app.run_task(ids[0])
        page = app.list_tasks(limit=1)
        self.assertEqual(1, len(page['tasks']))
        self.assertTrue(page['has_more'])
        self.assertEqual(1, len(app.list_tasks(limit=1, cursor=page['next_cursor'])['tasks']))
        attempts = app.get_attempts(ids[0])
        self.assertEqual(1, len(attempts))
        self.assertEqual('succeeded', attempts[0]['status'])
        self.assertNotIn('input_fingerprint', str(attempts))
        self.assertNotIn('approach_key', str(attempts))
        self.assertNotIn('payload_id', str(page))
        for limit in (True, 0, 21, 1.0):
            with self.assertRaises(ValueError):
                app.list_tasks(limit=limit)
            with self.assertRaises(ValueError):
                app.get_attempts(ids[0], limit=limit)

    def test_mixed_task_list_fails_closed_without_leaking_other_lane(self):
        app = self.app()
        app.submit_image_task('image.generate', prompt='a red square')
        from agent.adult.models import AdultTaskRequest
        other = self.tasks.create_task(AdultTaskRequest('text.analyze', self.cipher.fingerprint(b'x')),
                                      TaskBudget())
        with self.assertRaises(ValueError):
            app.list_tasks()
        with self.assertRaises(ValueError):
            app.get_attempts(other.task_id)
