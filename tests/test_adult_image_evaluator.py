"""Tests for deterministic Adult image output evaluation."""
from __future__ import annotations

import struct
import unittest
import zlib

from agent.adult.adult_image_evaluator import AdultImageEvaluator
from agent.adult.adult_image_policies import AdultImagePolicies
from agent.adult.integrations.protocols import ImageAdapterResult, ImageAnalysisResult
from agent.adult.models import PolicyDecision


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(width: int = 1, height: int = 1) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanline = b"\\x00" + (b"\\x00" * width * 4)
    return (
        b"\\x89PNG\\r\\n\\x1a\\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanline))
        + _png_chunk(b"IEND", b"")
    )


def _unsafe_analysis(text: str) -> ImageAnalysisResult:
    result = object.__new__(ImageAnalysisResult)
    object.__setattr__(result, "text", text)
    object.__setattr__(result, "usage", {})
    object.__setattr__(result, "cost_microusd", 0)
    return result


class RaisingPolicies(AdultImagePolicies):
    def validate_analysis_output(self, task_type, output_text):
        raise RuntimeError("private output must not escape")

    def validate_image_output(self, *args, **kwargs):
        raise RuntimeError("private image must not escape")


class MalformedPolicies(AdultImagePolicies):
    def validate_analysis_output(self, task_type, output_text):
        return {"allowed": True}

    def validate_image_output(self, *args, **kwargs):
        return {"allowed": True}


class DenyingPolicies(AdultImagePolicies):
    def validate_analysis_output(self, task_type, output_text):
        return PolicyDecision(False, ("policy_blocked",))

    def validate_image_output(self, *args, **kwargs):
        return PolicyDecision(False, ("policy_blocked",))


class AdultImageEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.policies = AdultImagePolicies()
        self.evaluator = AdultImageEvaluator(self.policies)
        self.analysis = ImageAnalysisResult("A bounded image analysis.", {}, 0)
        self.image = ImageAdapterResult(_png(), "image/png", {}, 0)

    def test_accepts_all_three_image_operations(self):
        analysis = self.evaluator.evaluate("image.analyze", self.analysis)
        self.assertTrue(analysis.accepted)
        self.assertEqual(1.0, analysis.score)
        self.assertFalse(analysis.needs_revision)
        self.assertEqual(8, len(analysis.checks))
        self.assertEqual((), analysis.reason_codes)

        for task_type in ("image.generate", "image.edit"):
            with self.subTest(task_type=task_type):
                result = self.evaluator.evaluate(task_type, self.image)
                self.assertTrue(result.accepted)
                self.assertEqual(1.0, result.score)
                self.assertFalse(result.needs_revision)
                self.assertEqual(6, len(result.checks))
                self.assertEqual((), result.reason_codes)

    def test_unsupported_task_and_wrong_result_types_are_terminal(self):
        unsupported = self.evaluator.evaluate("text.generate", self.analysis)
        self.assertFalse(unsupported.accepted)
        self.assertIn("unsupported_task_type", unsupported.reason_codes)
        self.assertFalse(unsupported.needs_revision)

        wrong_analysis = self.evaluator.evaluate("image.analyze", self.image)
        self.assertFalse(wrong_analysis.accepted)
        self.assertIn("analysis_result_invalid", wrong_analysis.reason_codes)
        self.assertFalse(wrong_analysis.needs_revision)

        wrong_image = self.evaluator.evaluate("image.generate", self.analysis)
        self.assertFalse(wrong_image.accepted)
        self.assertIn("image_result_invalid", wrong_image.reason_codes)
        self.assertFalse(wrong_image.needs_revision)

    def test_analysis_rejects_empty_oversized_and_prohibited_controls(self):
        cases = (
            ("", "output_empty"),
            ("x" * 20_001, "output_too_long"),
            ("bad\\x00text", "prohibited_control_character"),
            ("bad\\x85text", "prohibited_control_character"),
        )
        for value, reason in cases:
            with self.subTest(reason=reason):
                result = self.evaluator.evaluate("image.analyze", _unsafe_analysis(value))
                self.assertFalse(result.accepted)
                self.assertIn(reason, result.reason_codes)
                self.assertTrue(result.needs_revision)
                self.assertNotIn(value, repr(result))

    def test_analysis_accepts_tab_newline_and_carriage_return(self):
        result = self.evaluator.evaluate(
            "image.analyze", ImageAnalysisResult("line 1\\tline 2\\r\\n", {}, 0)
        )
        self.assertTrue(result.accepted)

    def test_analysis_rejects_invalid_utf8_surrogates_without_echoing(self):
        private_value = "private-\\ud800"
        result = self.evaluator.evaluate(
            "image.analyze", _unsafe_analysis(private_value)
        )
        self.assertFalse(result.accepted)
        self.assertIn("invalid_utf8", result.reason_codes)
        self.assertTrue(result.needs_revision)
        self.assertNotIn(private_value, repr(result))

    def test_corrupt_and_unsupported_image_outputs_are_retryable(self):
        for data, reason in (
            (b"not an image", "image_output_unsupported_format"),
            (b"\\x89PNG\\r\\n\\x1a\\ntruncated", "image_output_invalid"),
        ):
            with self.subTest(reason=reason):
                output = ImageAdapterResult(data, "image/png", {}, 0)
                result = self.evaluator.evaluate("image.generate", output)
                self.assertFalse(result.accepted)
                self.assertIn(reason, result.reason_codes)
                self.assertTrue(result.needs_revision)
                self.assertNotIn(data.decode("latin1"), repr(result))

    def test_declared_detected_and_route_mime_constraints_are_enforced(self):
        mismatch = ImageAdapterResult(_png(), "image/jpeg", {}, 0)
        result = self.evaluator.evaluate("image.generate", mismatch)
        self.assertFalse(result.accepted)
        self.assertIn("declared_mime_mismatch", result.reason_codes)
        self.assertTrue(result.needs_revision)

        route_mismatch = self.evaluator.evaluate(
            "image.generate", self.image, output_mime_types=("image/webp",)
        )
        self.assertFalse(route_mismatch.accepted)
        self.assertIn("output_mime_not_allowed", route_mismatch.reason_codes)
        self.assertTrue(route_mismatch.needs_revision)

    def test_route_byte_and_dimension_limits_are_enforced(self):
        for kwargs, reason in (
            ({"max_bytes": 10}, "output_image_too_large"),
            ({"max_width": 0}, "route_limit_invalid"),
            ({"max_height": 1}, None),
        ):
            with self.subTest(kwargs=kwargs):
                result = self.evaluator.evaluate(
                    "image.generate", self.image, **kwargs
                )
                if reason is None:
                    self.assertTrue(result.accepted)
                else:
                    self.assertFalse(result.accepted)
                    self.assertIn(reason, result.reason_codes)
                    self.assertEqual(reason != "route_limit_invalid", result.needs_revision)

    def test_policy_exceptions_and_malformed_decisions_fail_closed(self):
        for policies in (RaisingPolicies(), MalformedPolicies()):
            evaluator = AdultImageEvaluator(policies)
            analysis = evaluator.evaluate("image.analyze", self.analysis)
            image = evaluator.evaluate("image.generate", self.image)
            for result in (analysis, image):
                self.assertFalse(result.accepted)
                self.assertIn("policy_unavailable", result.reason_codes)
                self.assertFalse(result.needs_revision)

    def test_missing_policy_dependency_fails_closed(self):
        evaluator = AdultImageEvaluator()
        for task_type, output in (
            ("image.analyze", self.analysis),
            ("image.generate", self.image),
        ):
            with self.subTest(task_type=task_type):
                result = evaluator.evaluate(task_type, output)
                self.assertFalse(result.accepted)
                self.assertIn("policy_unavailable", result.reason_codes)
                self.assertFalse(result.needs_revision)

    def test_non_structural_policy_rejection_is_terminal(self):
        evaluator = AdultImageEvaluator(DenyingPolicies())
        for task_type, output in (
            ("image.analyze", self.analysis),
            ("image.edit", self.image),
        ):
            with self.subTest(task_type=task_type):
                result = evaluator.evaluate(task_type, output)
                self.assertFalse(result.accepted)
                self.assertIn("policy_rejected", result.reason_codes)
                self.assertFalse(result.needs_revision)

    def test_invalid_route_mime_policy_fails_closed(self):
        result = self.evaluator.evaluate(
            "image.generate", self.image, output_mime_types=("image/*",)
        )
        self.assertFalse(result.accepted)
        self.assertIn("policy_rejected", result.reason_codes)
        self.assertFalse(result.needs_revision)


if __name__ == "__main__":
    unittest.main()
