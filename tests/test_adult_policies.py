"""Stage 7 deterministic Adult text policy tests."""
import unittest

from agent.adult.adult_policies import AdultPolicies
from agent.adult.models import PolicyDecision


class AdultPoliciesTests(unittest.TestCase):
    def setUp(self):
        self.policies = AdultPolicies()

    def test_accepts_supported_text_tasks_and_optional_instruction(self):
        for task_type in ("text.analyze", "text.generate", "text.rewrite"):
            self.assertEqual(PolicyDecision(True), self.policies.validate_text_task(
                task_type, None, "A bounded source text."
            ))
            self.assertTrue(self.policies.validate_text_task(
                task_type, "Keep the result concise.", "A bounded source text."
            ).allowed)

    def test_rejects_unsupported_task_type(self):
        decision = self.policies.validate_text_task("image.analyze", None, "source")
        self.assertFalse(decision.allowed)
        self.assertIn("unsupported_task_type", decision.reason_codes)

    def test_rejects_empty_or_oversized_source_text(self):
        self.assertIn("source_text_empty", self.policies.validate_text_task(
            "text.analyze", None, "  \n "
        ).reason_codes)
        self.assertIn("source_text_too_long", self.policies.validate_text_task(
            "text.analyze", None, "x" * 20_001
        ).reason_codes)

    def test_rejects_invalid_or_oversized_instruction(self):
        self.assertIn("instruction_invalid", self.policies.validate_text_task(
            "text.analyze", 42, "source"
        ).reason_codes)
        self.assertIn("instruction_too_long", self.policies.validate_text_task(
            "text.analyze", "x" * 4_001, "source"
        ).reason_codes)

    def test_rejects_invalid_utf8_and_prohibited_controls(self):
        invalid_utf8 = self.policies.validate_text_task(
            "text.analyze", None, "bad\ud800text"
        )
        self.assertIn("invalid_utf8", invalid_utf8.reason_codes)
        controls = self.policies.validate_text_task(
            "text.analyze", "instruction\x00", "source"
        )
        self.assertIn("prohibited_control_character", controls.reason_codes)

    def test_accepts_tab_newline_and_carriage_return(self):
        self.assertTrue(self.policies.validate_text_task(
            "text.analyze", "line one\nline two", "a\tb\r\nc"
        ).allowed)

    def test_output_is_checked_independently(self):
        self.assertTrue(self.policies.validate_text_output(
            "text.rewrite", "A valid output."
        ).allowed)
        self.assertIn("output_empty", self.policies.validate_text_output(
            "text.rewrite", " \n "
        ).reason_codes)
        self.assertIn("output_too_long", self.policies.validate_text_output(
            "text.rewrite", "x" * 20_001
        ).reason_codes)

    def test_output_enforces_utf8_byte_limit_and_control_characters(self):
        max_unicode = self.policies.validate_text_output(
            "text.generate", "😀" * 20_000
        )
        self.assertTrue(max_unicode.allowed)
        controls = self.policies.validate_text_output(
            "text.generate", "output\x7f"
        )
        self.assertIn("prohibited_control_character", controls.reason_codes)

    def test_output_rejects_unsupported_task_and_non_text_values(self):
        self.assertIn("unsupported_task_type", self.policies.validate_text_output(
            "image.generate", "output"
        ).reason_codes)
        self.assertIn("output_invalid", self.policies.validate_text_output(
            "text.generate", None
        ).reason_codes)


if __name__ == "__main__":
    unittest.main()
