"""Stage 7 deterministic Adult text evaluator tests."""
import unittest

from agent.adult.adult_evaluator import AdultEvaluator
from agent.adult.models import PolicyDecision


class RaisingPolicy:
    def validate_text_output(self, task_type, output_text):
        raise RuntimeError("must not escape")


class MalformedPolicy:
    def validate_text_output(self, task_type, output_text):
        return {"allowed": True}


class DenyingPolicy:
    def validate_text_output(self, task_type, output_text):
        return PolicyDecision(False, ("policy_blocked",))


class AdultEvaluatorTests(unittest.TestCase):
    def setUp(self):
        from agent.adult.adult_policies import AdultPolicies
        self.evaluator = AdultEvaluator(AdultPolicies())

    def test_accepts_each_supported_text_operation(self):
        for task_type in ("text.analyze", "text.generate", "text.rewrite"):
            result = self.evaluator.evaluate(task_type, "A valid output.\n")
            self.assertTrue(result.accepted)
            self.assertEqual(1.0, result.score)
            self.assertFalse(result.needs_revision)
            self.assertEqual(8, len(result.checks))
            self.assertEqual((), result.reason_codes)

    def test_rejects_non_text_output_without_exposing_value(self):
        result = self.evaluator.evaluate("text.generate", object())
        self.assertFalse(result.accepted)
        self.assertIn("output_invalid", result.reason_codes)
        self.assertTrue(result.needs_revision)

    def test_rejects_empty_or_whitespace_only_output_as_retryable(self):
        for output in ("", " \t\n "):
            result = self.evaluator.evaluate("text.rewrite", output)
            self.assertFalse(result.accepted)
            self.assertIn("output_empty", result.reason_codes)
            self.assertTrue(result.needs_revision)

    def test_rejects_invalid_utf8_surrogates(self):
        result = self.evaluator.evaluate("text.analyze", "bad\ud800text")
        self.assertFalse(result.accepted)
        self.assertIn("invalid_utf8", result.reason_codes)
        self.assertTrue(result.needs_revision)

    def test_rejects_prohibited_c0_and_c1_controls(self):
        for output in ("bad\x00text", "bad\x85text"):
            result = self.evaluator.evaluate("text.analyze", output)
            self.assertFalse(result.accepted)
            self.assertIn("prohibited_control_character", result.reason_codes)
            self.assertTrue(result.needs_revision)

    def test_accepts_permitted_tab_newline_and_carriage_return(self):
        result = self.evaluator.evaluate("text.analyze", "a\tb\r\nc")
        self.assertTrue(result.accepted)

    def test_rejects_output_over_character_limit(self):
        result = self.evaluator.evaluate("text.generate", "x" * 20_001)
        self.assertFalse(result.accepted)
        self.assertIn("output_too_long", result.reason_codes)
        self.assertTrue(result.needs_revision)

    def test_unsupported_task_type_is_terminal(self):
        result = self.evaluator.evaluate("image.generate", "valid output")
        self.assertFalse(result.accepted)
        self.assertIn("unsupported_task_type", result.reason_codes)
        self.assertFalse(result.needs_revision)

    def test_missing_policy_dependency_fails_closed(self):
        result = AdultEvaluator().evaluate("text.generate", "valid output")
        self.assertFalse(result.accepted)
        self.assertIn("policy_unavailable", result.reason_codes)
        self.assertFalse(result.needs_revision)

    def test_policy_exception_and_malformed_decision_fail_closed(self):
        for policy in (RaisingPolicy(), MalformedPolicy()):
            result = AdultEvaluator(policy).evaluate("text.generate", "valid output")
            self.assertFalse(result.accepted)
            self.assertIn("policy_unavailable", result.reason_codes)
            self.assertFalse(result.needs_revision)

    def test_non_structural_policy_rejection_is_terminal(self):
        result = AdultEvaluator(DenyingPolicy()).evaluate("text.generate", "valid output")
        self.assertFalse(result.accepted)
        self.assertIn("policy_rejected", result.reason_codes)
        self.assertFalse(result.needs_revision)


if __name__ == "__main__":
    unittest.main()
