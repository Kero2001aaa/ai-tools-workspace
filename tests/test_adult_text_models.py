"""Stage 7 typed text-boundary DTO tests."""
import unittest

from agent.adult.integrations.protocols import TextAdapterResult, TextRequest
from agent.adult.models import AdultModelValidationError, EvaluationResult, PolicyDecision


class AdultTextModelDTOTests(unittest.TestCase):
    def test_text_request_accepts_only_supported_text_operations(self):
        for task_type in ("text.analyze", "text.generate", "text.rewrite"):
            request = TextRequest("task-1234567890123456", "step-1234567890123456",
                                  task_type, "a" * 32)
            self.assertEqual(task_type, request.task_type)
            self.assertEqual(20000, request.max_output_chars)

    def test_text_request_rejects_image_operation_and_oversized_output_limit(self):
        with self.assertRaises(AdultModelValidationError):
            TextRequest("task-1234567890123456", "step-1234567890123456",
                        "image.analyze", "a" * 32)
        with self.assertRaises(AdultModelValidationError):
            TextRequest("task-1234567890123456", "step-1234567890123456",
                        "text.analyze", "a" * 32, 20001)

    def test_text_adapter_result_hides_text_from_repr_and_validates_metadata(self):
        result = TextAdapterResult("private generated output", {"input_tokens": 12}, 0)
        self.assertNotIn("private generated output", repr(result))
        self.assertEqual({"input_tokens": 12}, result.usage)
        with self.assertRaises(AdultModelValidationError):
            TextAdapterResult("ok", {"input_tokens": True}, 0)
        with self.assertRaises(AdultModelValidationError):
            TextAdapterResult("ok", {}, -1)

    def test_policy_decision_requires_bounded_reason_codes(self):
        self.assertTrue(PolicyDecision(True).allowed)
        self.assertFalse(PolicyDecision(False, ("unsupported_task_type",)).allowed)
        for invalid in (
            (True, ("not_allowed_reason",)),
            (False, ()),
            (False, ("UPPERCASE",)),
            (False, ("x" * 49,)),
        ):
            with self.assertRaises(AdultModelValidationError):
                PolicyDecision(*invalid)

    def test_evaluation_result_enforces_consistent_bounded_outcome(self):
        accepted = EvaluationResult(True, 1.0, ("valid_text", "within_bounds"), (), False)
        self.assertTrue(accepted.accepted)
        rejected = EvaluationResult(False, 0.5, ("valid_text",), ("empty_output",), True)
        self.assertTrue(rejected.needs_revision)
        for invalid in (
            (True, 1.0, ("valid_text",), (), True),
            (False, 0.0, (), (), False),
            (True, 1.1, ("valid_text",), (), False),
            (True, True, ("valid_text",), (), False),
            (True, 1.0, ("BAD CHECK",), (), False),
        ):
            with self.assertRaises(AdultModelValidationError):
                EvaluationResult(*invalid)


if __name__ == "__main__":
    unittest.main()
