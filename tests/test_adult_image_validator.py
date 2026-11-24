"""Synthetic structural fixtures and adversarial tests for image validation."""
from __future__ import annotations

import struct
import unittest
import zlib

from agent.adult.adult_image_validator import ImageValidationError, validate_image


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(width: int = 1, height: int = 1, *, color_type: int = 6, bit_depth: int = 8,
         chunks: tuple[tuple[bytes, bytes], ...] | None = None) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    body = [_png_chunk(b"IHDR", header)]
    if chunks is None:
        body.extend((_png_chunk(b"IDAT", b"synthetic"), _png_chunk(b"IEND", b"")))
    else:
        body.extend(_png_chunk(kind, payload) for kind, payload in chunks)
    return b"\x89PNG\r\n\x1a\n" + b"".join(body)


def _jpeg(width: int = 1, height: int = 1) -> bytes:
    sof_payload = (
        bytes((8,)) + struct.pack(">HHB", height, width, 1) + bytes((1, 0x11, 0))
    )
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    sos_payload = bytes((1, 1, 0, 0, 63, 0))
    sos = b"\xff\xda" + struct.pack(">H", len(sos_payload) + 2) + sos_payload
    return b"\xff\xd8" + sof + sos + b"\x12\xff\x00\x34\xff\xd9"


def _webp_chunk(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) & 1 else b"")


def _webp(payload: bytes, kind: bytes = b"VP8L", *, extended: bool = False,
          flags: int = 0) -> bytes:
    chunks = []
    if extended:
        # Canvas is 1x1; all reserved bytes are zero.
        chunks.append(_webp_chunk(b"VP8X", bytes((flags, 0, 0, 0, 0, 0, 0, 0, 0, 0))))
    chunks.append(_webp_chunk(kind, payload))
    body = b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


class ImageValidatorTests(unittest.TestCase):
    def test_valid_png_jpeg_and_webp_return_detected_metadata_and_digest(self):
        fixtures = (
            (_png(), "image/png"),
            (_jpeg(), "image/jpeg"),
            (_webp(bytes((0x2F, 0, 0, 0, 0))), "image/webp"),
            (_webp(bytes((0, 0, 0, 0x9D, 1, 0x2A, 1, 0, 1, 0)), b"VP8 "), "image/webp"),
            (_webp(bytes((0x2F, 0, 0, 0, 0)), extended=True), "image/webp"),
        )
        for raw, expected_mime in fixtures:
            with self.subTest(mime=expected_mime, size=len(raw)):
                result = validate_image(raw)
                self.assertEqual(expected_mime, result.mime_type)
                self.assertEqual(len(raw), result.byte_count)
                self.assertEqual((1, 1), (result.width, result.height))
                self.assertEqual(64, len(result.sha256))
                self.assertNotIn(repr(raw), repr(result))

    def test_png_rejects_crc_error_truncation_trailing_data_and_missing_terminal_chunk(self):
        valid = _png()
        crc_bad = bytearray(valid)
        crc_bad[-5] ^= 1
        for raw in (bytes(crc_bad), valid[:-2], valid + b"trailing",
                    b"\x89PNG\r\n\x1a\n" + _png_chunk(
                        b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
                    )):
            with self.subTest(length=len(raw)), self.assertRaises(ImageValidationError):
                validate_image(raw)

    def test_png_rejects_unknown_critical_animation_and_noncontiguous_idat(self):
        animation = _png(chunks=((b"acTL", struct.pack(">II", 1, 0)),
                                 (b"IDAT", b"x"), (b"IEND", b"")))
        unknown = _png(chunks=((b"ABCD", b"x"), (b"IDAT", b"x"), (b"IEND", b"")))
        separated = _png(chunks=((b"IDAT", b"x"), (b"tEXt", b"k\x00v"),
                                 (b"IDAT", b"y"), (b"IEND", b"")))
        for raw in (animation, unknown, separated):
            with self.subTest(length=len(raw)), self.assertRaises(ImageValidationError):
                validate_image(raw)

    def test_png_enforces_color_depth_palette_and_dimensions(self):
        bad_depth = _png(bit_depth=4)
        invalid_palette = _png(color_type=3, chunks=((b"IDAT", b"x"), (b"IEND", b"")))
        huge_dimension = _png(width=8193)
        too_many_pixels = _png(width=7000, height=6000)
        for raw in (bad_depth, invalid_palette, huge_dimension, too_many_pixels):
            with self.subTest(length=len(raw)), self.assertRaises(ImageValidationError):
                validate_image(raw)

    def test_jpeg_rejects_truncation_missing_eoi_bad_segment_length_and_bad_scan(self):
        valid = _jpeg()
        bad_length = valid[:4] + b"\xff\xc0\x00\x01" + valid[8:]
        cases = (valid[:-2], valid[:2], bad_length,
                 b"\xff\xd8\xff\xda\x00\x02\xff\xd9")
        for raw in cases:
            with self.subTest(length=len(raw)), self.assertRaises(ImageValidationError):
                validate_image(raw)

    def test_jpeg_rejects_unsupported_sof_and_trailing_bytes(self):
        unsupported = _jpeg().replace(b"\xff\xc0", b"\xff\xc3", 1)
        for raw in (unsupported, _jpeg() + b"x"):
            with self.subTest(length=len(raw)), self.assertRaises(ImageValidationError):
                validate_image(raw)

    def test_webp_rejects_riff_size_chunk_padding_animation_and_dimension_mismatch(self):
        valid = _webp(bytes((0x2F, 0, 0, 0, 0)))
        bad_riff = valid[:4] + struct.pack("<I", len(valid)) + valid[8:]
        bad_padding = _webp_chunk(b"VP8L", bytes((0x2F, 0, 0, 0, 0)))[:-1] + b"X"
        padded = b"RIFF" + struct.pack("<I", len(bad_padding) + 4) + b"WEBP" + bad_padding
        animated = _webp(bytes((0x2F, 0, 0, 0, 0)), extended=True, flags=0x02)
        mismatch = _webp(bytes((0x2F, 1, 0, 0, 0)), extended=True)
        for raw in (bad_riff, padded, animated, mismatch):
            with self.subTest(length=len(raw)), self.assertRaises(ImageValidationError):
                validate_image(raw)

    def test_webp_rejects_unknown_chunks_duplicate_frames_and_malformed_headers(self):
        unknown_chunk = _webp_chunk(b"JUNK", b"x")
        unknown = b"RIFF" + struct.pack("<I", len(unknown_chunk) + 4) + b"WEBP" + unknown_chunk
        duplicate_body = _webp_chunk(b"VP8L", bytes((0x2F, 0, 0, 0, 0))) * 2
        duplicate = b"RIFF" + struct.pack("<I", len(duplicate_body) + 4) + b"WEBP" + duplicate_body
        malformed = b"RIFF\x04\x00\x00\x00WEBP"
        for raw in (unknown, duplicate, malformed):
            with self.subTest(length=len(raw)), self.assertRaises(ImageValidationError):
                validate_image(raw)

    def test_format_spoof_empty_input_and_input_output_size_bounds(self):
        for raw in (b"", b"<svg></svg>", b"GIF89a" + b"x" * 20):
            with self.subTest(raw=raw[:8]), self.assertRaises(ImageValidationError):
                validate_image(raw)
        with self.assertRaises(ImageValidationError) as raised:
            validate_image(b"x" * (15 * 1024 * 1024 + 1))
        self.assertEqual("input_too_large", raised.exception.error_code)
        with self.assertRaises(ImageValidationError) as raised_output:
            validate_image(b"x" * (20 * 1024 * 1024 + 1), output=True)
        self.assertEqual("output_too_large", raised_output.exception.error_code)


if __name__ == "__main__":
    unittest.main()
