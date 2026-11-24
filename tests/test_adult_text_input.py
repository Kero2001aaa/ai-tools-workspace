"""Tests for strict Adult text input envelopes."""
from __future__ import annotations

import json
import unittest

from agent.adult.adult_policies import AdultPolicies
from agent.adult.adult_text_input import (
    TextInputError, decode_text_input, encode_text_input,
)


class AdultTextInputTests(unittest.TestCase):
    def test_canonical_envelope_round_trips_with_and_without_instruction(self):
        for instruction in (None, "Keep it concise"):
            encoded = encode_text_input("text.generate", "A short brief", instruction)
            self.assertEqual(
                encoded,
                json.dumps(
                    {
                        "schema_version": 1,
                        "task_type": "text.generate",
                        "text": "A short brief",
                        "instruction": instruction,
                    },
                    ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                    allow_nan=False,
                ).encode("utf-8"),
            )
            decoded = decode_text_input(encoded, "text.generate")
            self.assertEqual(decoded.text, "A short brief")
            self.assertEqual(decoded.instruction, instruction)
            self.assertNotIn("A short brief", repr(decoded))

    def test_rejects_duplicate_json_keys(self):
        raw = b'{"schema_version":1,"task_type":"text.analyze","text":"one","text":"two","instruction":null}'
        with self.assertRaises(TextInputError):
            decode_text_input(raw, "text.analyze")

    def test_rejects_unknown_or_missing_keys(self):
        valid = {
            "schema_version": 1, "task_type": "text.analyze",
            "text": "source", "instruction": None,
        }
        unknown = dict(valid, extra="no")
        missing = dict(valid)
        missing.pop("instruction")
        for value in (unknown, missing):
            with self.subTest(value=value):
                with self.assertRaises(TextInputError):
                    decode_text_input(json.dumps(value).encode(), "text.analyze")

    def test_rejects_invalid_utf8_and_malformed_json(self):
        for raw in (b"\xff", b'{"schema_version":'):
            with self.subTest(raw=raw):
                with self.assertRaises(TextInputError):
                    decode_text_input(raw, "text.analyze")

    def test_rejects_wrong_schema_version_and_non_integer_version(self):
        for version in (2, True, "1"):
            raw = json.dumps({
                "schema_version": version, "task_type": "text.analyze",
                "text": "source", "instruction": None,
            }).encode()
            with self.subTest(version=version):
                with self.assertRaises(TextInputError):
                    decode_text_input(raw, "text.analyze")

    def test_rejects_task_type_mismatch_and_unsupported_operation(self):
        raw = encode_text_input("text.rewrite", "source")
        with self.assertRaises(TextInputError):
            decode_text_input(raw, "text.analyze")
        with self.assertRaises(TextInputError):
            encode_text_input("image.generate", "source")

    def test_rejects_non_text_fields(self):
        for text, instruction in ((7, None), ("source", 7)):
            raw = json.dumps({
                "schema_version": 1, "task_type": "text.analyze",
                "text": text, "instruction": instruction,
            }).encode()
            with self.subTest(text=text, instruction=instruction):
                with self.assertRaises(TextInputError):
                    decode_text_input(raw, "text.analyze")

    def test_policy_rejects_empty_text_and_oversized_fields(self):
        cases = (
            ("", None), ("   ", None), ("x" * 20001, None),
            ("source", "i" * 4001), ("bad\x00text", None),
        )
        for text, instruction in cases:
            with self.subTest(text_length=len(text), instruction_length=None if instruction is None else len(instruction)):
                with self.assertRaises(TextInputError):
                    encode_text_input("text.analyze", text, instruction)

    def test_policy_rejects_invalid_unicode_surrogates(self):
        with self.assertRaises(TextInputError):
            encode_text_input("text.analyze", "\ud800")

    def test_decoder_rejects_policy_invalid_escaped_controls(self):
        raw = b'{"schema_version":1,"task_type":"text.analyze","text":"bad\\u0000text","instruction":null}'
        with self.assertRaises(TextInputError):
            decode_text_input(raw, "text.analyze")

    def test_envelope_byte_limit_is_enforced(self):
        with self.assertRaises(TextInputError) as raised:
            decode_text_input(b" " * (128 * 1024 + 1), "text.analyze")
        self.assertEqual(raised.exception.error_code, "input_too_large")

    def test_policy_dependency_failure_fails_closed(self):
        class BrokenPolicies(AdultPolicies):
            def validate_text_task(self, task_type, instruction, text):
                raise RuntimeError("sensitive input must not leak")

        with self.assertRaises(TextInputError) as raised:
            encode_text_input("text.analyze", "source", policies=BrokenPolicies())
        self.assertEqual(raised.exception.error_code, "input_policy_rejected")
        self.assertNotIn("sensitive", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
