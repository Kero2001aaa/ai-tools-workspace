"""Stage 8 image input DTO, envelope, and prompt contract tests."""
import json
import unittest

from agent.adult.adult_image_input import (
    ImageInputError,
    decode_image_input,
    decode_image_prompt,
    encode_image_input,
    encode_image_prompt,
)
from agent.adult.integrations.protocols import ImageAdapterResult, ImageAnalysisResult
from agent.adult.models import AdultModelValidationError, ImageInputEnvelope, ValidatedImage


class ImageInputEnvelopeTests(unittest.TestCase):
    def test_canonical_round_trip_for_all_image_operations(self):
        envelopes = (
            ImageInputEnvelope("image.analyze", image_payload_id="image-ref-1"),
            ImageInputEnvelope("image.generate", prompt_payload_id="prompt-ref-1"),
            ImageInputEnvelope(
                "image.edit",
                image_payload_id="image-ref-2",
                prompt_payload_id="prompt-ref-2",
            ),
        )
        for envelope in envelopes:
            with self.subTest(task_type=envelope.task_type):
                encoded = encode_image_input(envelope)
                self.assertLessEqual(len(encoded), 4096)
                self.assertEqual(
                    encoded,
                    json.dumps(
                        envelope.to_mapping(),
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8"),
                )
                self.assertEqual(
                    envelope,
                    decode_image_input(encoded, envelope.task_type),
                )

    def test_decoder_rejects_duplicate_keys(self):
        raw = (
            b'{"schema_version":1,"schema_version":1,'
            b'"task_type":"image.analyze","image_payload_id":"image-ref"}'
        )
        with self.assertRaises(ImageInputError):
            decode_image_input(raw, "image.analyze")

    def test_decoder_rejects_unknown_and_missing_operation_keys(self):
        cases = (
            b'{"image_payload_id":"image-ref","schema_version":1,"task_type":"image.analyze","extra":"x"}',
            b'{"schema_version":1,"task_type":"image.analyze"}',
            b'{"image_payload_id":"image-ref","prompt_payload_id":"prompt-ref","schema_version":1,"task_type":"image.analyze"}',
        )
        for raw in cases:
            with self.subTest(raw_length=len(raw)), self.assertRaises(ImageInputError):
                decode_image_input(raw, "image.analyze")

    def test_decoder_rejects_bool_version_wrong_operation_and_wrong_value_types(self):
        cases = (
            b'{"image_payload_id":"image-ref","schema_version":true,"task_type":"image.analyze"}',
            b'{"image_payload_id":"image-ref","schema_version":1.0,"task_type":"image.analyze"}',
            b'{"image_payload_id":["image-ref"],"schema_version":1,"task_type":"image.analyze"}',
            b'{"image_payload_id":"image-ref","schema_version":1,"task_type":"image.generate"}',
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ImageInputError):
                decode_image_input(raw, "image.analyze")

    def test_decoder_rejects_invalid_utf8_json_trailing_data_and_noncanonical_json(self):
        cases = (
            b"\xff",
            b'{"schema_version":1,"task_type":"image.analyze","image_payload_id":"image-ref"} trailing',
            b'{ "image_payload_id":"image-ref", "schema_version":1, "task_type":"image.analyze" }',
        )
        for raw in cases:
            with self.subTest(raw_length=len(raw)), self.assertRaises(ImageInputError):
                decode_image_input(raw, "image.analyze")

    def test_decoder_rejects_unknown_task_and_invalid_opaque_reference(self):
        with self.assertRaises(ImageInputError):
            decode_image_input(b"{}", "text.analyze")
        raw = b'{"image_payload_id":"../unsafe","schema_version":1,"task_type":"image.analyze"}'
        with self.assertRaises(ImageInputError):
            decode_image_input(raw, "image.analyze")

    def test_encoder_requires_a_valid_envelope_and_enforces_size(self):
        with self.assertRaises(ImageInputError):
            encode_image_input(object())  # type: ignore[arg-type]
        oversized_id = "x" * 128
        envelope = ImageInputEnvelope("image.analyze", image_payload_id=oversized_id)
        # The identifier DTO is bounded independently; the codec remains bounded too.
        self.assertLessEqual(len(encode_image_input(envelope)), 4096)
        with self.assertRaises(ImageInputError):
            encode_image_input(ImageInputEnvelope("image.generate", prompt_payload_id="x" * 128))


class ImagePromptTests(unittest.TestCase):
    def test_prompt_round_trip_and_character_boundary(self):
        prompt = "a" * 4000
        self.assertEqual(prompt, decode_image_prompt(encode_image_prompt(prompt)))

    def test_prompt_rejects_empty_overlong_and_control_content_without_echoing(self):
        for prompt in ("", "   ", "x" * 4001, "safe\x00secret prompt"):
            with self.subTest(prompt_length=len(prompt)), self.assertRaises(ImageInputError) as raised:
                encode_image_prompt(prompt)
            self.assertNotIn("secret prompt", str(raised.exception))

    def test_prompt_rejects_invalid_utf8_and_byte_overflow(self):
        for raw in (b"\xff", b"x" * (16 * 1024 + 1)):
            with self.subTest(raw_length=len(raw)), self.assertRaises(ImageInputError):
                decode_image_prompt(raw)

    def test_prompt_allows_common_line_whitespace_but_rejects_other_controls(self):
        prompt = "first line\nsecond\tline"
        self.assertEqual(prompt, decode_image_prompt(encode_image_prompt(prompt)))
        with self.assertRaises(ImageInputError):
            encode_image_prompt("first\x1bsecond")


class ImageMetadataAndAdapterDtoTests(unittest.TestCase):
    def test_validated_image_accepts_only_bounded_supported_metadata(self):
        value = ValidatedImage(
            mime_type="image/png",
            byte_count=1024,
            width=1024,
            height=1024,
            sha256="a" * 64,
        )
        self.assertEqual("image/png", value.mime_type)
        self.assertEqual(1024 * 1024, value.width * value.height)

    def test_validated_image_rejects_invalid_mime_size_dimensions_pixels_and_digest(self):
        base = {
            "mime_type": "image/png",
            "byte_count": 1,
            "width": 1024,
            "height": 1024,
            "sha256": "b" * 64,
        }
        invalid = (
            {"mime_type": "image/svg+xml"},
            {"byte_count": True},
            {"byte_count": 20 * 1024 * 1024 + 1},
            {"width": 0},
            {"width": True},
            {"width": 8193},
            {"width": 8192, "height": 8192},
            {"sha256": "B" * 64},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(AdultModelValidationError):
                ValidatedImage(**{**base, **changes})

    def test_image_analysis_result_hides_text_and_bounds_utf8(self):
        private_text = "distinctive image analysis content"
        result = ImageAnalysisResult(text=private_text, usage={"input_tokens": 10})
        self.assertNotIn(private_text, repr(result))
        with self.assertRaises(AdultModelValidationError):
            ImageAnalysisResult(text="x" * 20_001, usage={})
        with self.assertRaises(AdultModelValidationError):
            ImageAnalysisResult(text="\ud800", usage={})

    def test_image_adapter_result_hides_bytes_and_bounds_type_size_and_mime(self):
        private_bytes = b"distinctive private image bytes"
        result = ImageAdapterResult(
            image_bytes=private_bytes,
            mime_type="image/png",
            usage={"output_tokens": 1},
        )
        self.assertNotIn(repr(private_bytes), repr(result))
        with self.assertRaises(AdultModelValidationError):
            ImageAdapterResult(image_bytes=b"", mime_type="image/png", usage={})
        with self.assertRaises(AdultModelValidationError):
            ImageAdapterResult(
                image_bytes=b"x" * (20 * 1024 * 1024 + 1),
                mime_type="image/png",
                usage={},
            )
        with self.assertRaises(AdultModelValidationError):
            ImageAdapterResult(image_bytes=b"x", mime_type="image/gif", usage={})

    def test_image_result_usage_and_cost_are_bounded(self):
        with self.assertRaises(AdultModelValidationError):
            ImageAnalysisResult(text="result", usage={"Invalid": 1})
        with self.assertRaises(AdultModelValidationError):
            ImageAdapterResult(
                image_bytes=b"x", mime_type="image/png", usage={}, cost_microusd=True
            )


if __name__ == "__main__":
    unittest.main()
