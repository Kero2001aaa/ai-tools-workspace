"""Optional image-analysis prompt over real stores and plumbing. Offline only.

The analysis prompt is bounded untrusted user data bound into the exact
content fingerprint. Promptless image.analyze behavior is unchanged. No
network, live provider, MCP or semantic moderation is exercised here.
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock

from tests import test_adult_image_executor as fixtures
from tests import test_adult_image_pipeline as pipeline_fixtures

from agent.adult.adult_image_input import (
    decode_image_input,
    encode_image_input,
    encode_image_prompt,
)
from agent.adult.adult_image_intake import canonical_image_input
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.integrations.protocols import ImageAnalysisRequest
from agent.adult.models import AdultModelValidationError, ImageInputEnvelope
from agent.adult.payload_store import AdultArtifactStore

PROMPT = "Read any visible text on the sign."


class AnalysisPromptStagingTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    compose = fixtures.ExecutorTests.compose
    enable = fixtures.ExecutorTests.enable

    def test_prompt_binds_distinct_content_fingerprint_before_staging(self):
        self.enable('image.analyze')
        staged = self.executor.stage('image.analyze', image=self.image, prompt=PROMPT)
        self.assertEqual('checkpointed', staged.status)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        request = self.tasks.get_task_request(staged.task_id)
        prompted = canonical_image_input('image.analyze', self.image, PROMPT)
        promptless = canonical_image_input('image.analyze', self.image, None)
        self.assertEqual(self.cipher.fingerprint(prompted), request.input_fingerprint)
        self.assertNotEqual(self.cipher.fingerprint(promptless), request.input_fingerprint)
        envelope = decode_image_input(
            self.artifacts.load_input(request.payload_id, staged.task_id), 'image.analyze')
        self.assertIsNotNone(envelope.image_payload_id)
        self.assertIsNotNone(envelope.prompt_payload_id)
        self.assertEqual(
            PROMPT,
            self.artifacts.load_input(envelope.prompt_payload_id, staged.task_id).decode('utf-8'))

    def test_promptless_analyze_is_unchanged_and_dispatches_no_prompt_reference(self):
        self.enable('image.analyze')
        staged = self.executor.stage('image.analyze', image=self.image)
        result = self.executor.run(staged.task_id, fixtures.CONFIG)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['analyze'])
        self.assertIsNone(self.adapter.requests[-1].prompt_payload_id)
        envelope = decode_image_input(
            self.artifacts.load_input(
                self.tasks.get_task_request(staged.task_id).payload_id, staged.task_id),
            'image.analyze')
        self.assertIsNone(envelope.prompt_payload_id)

    def test_prompted_analyze_dispatches_exact_prompt_reference(self):
        self.enable('image.analyze')
        staged = self.executor.stage('image.analyze', image=self.image, prompt=PROMPT)
        result = self.executor.run(staged.task_id, fixtures.CONFIG)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['analyze'])
        request = self.adapter.requests[-1]
        self.assertIsNotNone(request.prompt_payload_id)
        self.assertEqual(
            encode_image_prompt(PROMPT),
            self.artifacts.load_input(request.prompt_payload_id, staged.task_id))
        self.assertEqual(1, self.tasks.get_task(staged.task_id).attempt_count)


class AnalysisPromptRestoreTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    compose = fixtures.ExecutorTests.compose
    enable = fixtures.ExecutorTests.enable

    def reopened(self):
        tasks2 = AdultTaskStore(self.root / 'tasks.sqlite3')
        artifacts2 = AdultArtifactStore(self.root / 'artifacts', cipher=self.cipher)
        return tasks2, self.compose(tasks2, artifacts2)

    def test_full_loss_requires_exact_image_and_prompt_restore(self):
        self.enable('image.analyze')
        staged = self.executor.stage('image.analyze', image=self.image, prompt=PROMPT)
        tasks2, executor2 = self.reopened()
        self.assertEqual('needs_input', executor2.run(staged.task_id, fixtures.CONFIG).status)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        # Wrong prompt, dropped prompt and missing image are all rejected
        # before any queueing, without echoing content.
        rejections = ((self.image, None, 'restored_input_mismatch'),
                      (self.image, 'a changed question', 'restored_input_mismatch'),
                      (None, PROMPT, 'invalid_envelope'))
        for image, prompt, code in rejections:
            with self.subTest(image=image is not None, prompt=prompt):
                with self.assertRaises(ValueError) as raised:
                    executor2.restore_input(staged.task_id, image=image, prompt=prompt)
                self.assertEqual(code, str(raised.exception))
        self.assertEqual('needs_input', tasks2.get_task(staged.task_id).status)
        result = executor2.restore_input_and_run(
            staged.task_id, fixtures.CONFIG, image=self.image, prompt=PROMPT)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['analyze'])
        self.assertIsNotNone(self.adapter.requests[-1].prompt_payload_id)

    def test_partial_prompt_loss_restores_only_the_prompt_part(self):
        self.enable('image.analyze')
        staged = self.executor.stage('image.analyze', image=self.image, prompt=PROMPT)
        request = self.tasks.get_task_request(staged.task_id)
        envelope_bytes = self.artifacts.load_input(request.payload_id, staged.task_id)
        envelope = decode_image_input(envelope_bytes, 'image.analyze')
        # Evict only the prompt part; the envelope and image survive.
        self.artifacts.cache.discard_ref(staged.task_id, envelope.prompt_payload_id)
        self.assertEqual('needs_input', self.executor.run(staged.task_id, fixtures.CONFIG).status)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        self.executor.restore_input(staged.task_id, image=self.image, prompt=PROMPT)
        # The surviving envelope keeps its exact bytes and original references.
        self.assertEqual(envelope_bytes,
                         self.artifacts.load_input(request.payload_id, staged.task_id))
        self.assertEqual('queued', self.tasks.get_task(staged.task_id).status)
        result = self.executor.run(staged.task_id, fixtures.CONFIG)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['analyze'])


class AnalysisPromptPipelineTests(unittest.TestCase):
    """Pipeline-level analyze coverage, reusing the shared mock fixture build."""

    def build_prompted_analyze(self, prompt_blob):
        helper = object.__new__(pipeline_fixtures.AdultImagePipelineTests)
        helper.build(
            'image.analyze', output_mime_types=None, max_output_bytes=None,
            max_output_width=None, max_output_height=None,
        )
        envelope = ImageInputEnvelope(
            task_type='image.analyze',
            image_payload_id=pipeline_fixtures.IMAGE_ID,
            prompt_payload_id=pipeline_fixtures.PROMPT_ID,
        )
        blobs = {
            pipeline_fixtures.PAYLOAD_ID: encode_image_input(envelope),
            pipeline_fixtures.IMAGE_ID: helper.input_image,
            pipeline_fixtures.PROMPT_ID: prompt_blob,
        }
        helper.store.load_input = Mock(side_effect=lambda pid, tid: blobs[pid])
        return helper

    def test_prompted_analyze_passes_prompt_reference_to_adapter(self):
        helper = self.build_prompted_analyze(encode_image_prompt(PROMPT))
        outcome = helper.run_once()
        self.assertEqual('succeeded', outcome.status)
        helper.adapter.analyze.assert_called_once()
        request = helper.adapter.analyze.call_args.args[0]
        self.assertEqual(pipeline_fixtures.PROMPT_ID, request.prompt_payload_id)
        self.assertEqual(pipeline_fixtures.IMAGE_ID, request.input_payload_id)
        query = helper.router.choose_routes.call_args.args[0]
        self.assertEqual(('image',), query.input_modalities)

    def test_undecodable_stored_prompt_is_rejected_before_routing_or_attempt(self):
        helper = self.build_prompted_analyze(b'\xff')
        outcome = helper.run_once()
        self.assertEqual('rejected', outcome.status)
        self.assertEqual('INVALID_INPUT', outcome.error_code)
        helper.router.choose_routes.assert_not_called()
        helper.adapter.analyze.assert_not_called()
        helper.tracker.begin.assert_not_called()


class AnalysisPromptProtocolTests(unittest.TestCase):
    def test_optional_prompt_reference_validation(self):
        request = ImageAnalysisRequest('task-1', 'step-1', 'input-ref-1', 'image/png')
        self.assertIsNone(request.prompt_payload_id)
        prompted = ImageAnalysisRequest(
            'task-1', 'step-1', 'input-ref-1', 'image/png',
            prompt_payload_id='prompt-ref-1')
        self.assertEqual('prompt-ref-1', prompted.prompt_payload_id)
        with self.assertRaises(AdultModelValidationError):
            ImageAnalysisRequest(
                'task-1', 'step-1', 'input-ref-1', 'image/png',
                prompt_payload_id='../unsafe')


class AnalysisPromptCombinedBoundaryTests(unittest.TestCase):
    def test_facade_prompted_analysis_completes_and_output_contracts_validate(self):
        import base64
        from tests import test_adult_agent as combined_fixtures
        from agent.adult.adult_response_contracts import validate_response
        from agent.adult.artifact_result_access import ArtifactResultAccess
        helper=combined_fixtures.AdultAgentTests();helper.setUp()
        self.addCleanup(helper.doCleanups)
        app=helper.agent();helper.image_helper.enable('image.analyze')
        task=app.submit_image_task('image.analyze',PROMPT,helper.image_helper.image,'image/png')['task_id']
        result=app.run_task(task,max_attempts=1)
        self.assertEqual('completed',result['task']['status'])
        validate_response('execution',result)
        request=helper.image_helper.adapter.requests[-1]
        self.assertIsNotNone(request.prompt_payload_id)
        self.assertEqual(1,helper.image_helper.adapter.calls['analyze'])
        self.assertEqual('input_removed',result['input_cleanup']['status'])
        artifact=result['task']['latest_result_artifact_id']
        chunk=ArtifactResultAccess(helper.tasks,helper.artifacts).read(artifact)
        validate_response('artifact',chunk)
        self.assertTrue(base64.b64decode(chunk['data_base64']))
        app.run_task(task)
        self.assertEqual(1,helper.image_helper.adapter.calls['analyze'])
