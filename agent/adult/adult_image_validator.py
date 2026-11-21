"""Strict standard-library structural validation for PNG, JPEG, and WebP images.

This module validates container structure and metadata only. It deliberately does not
decode raster pixels or claim that the image can be rendered by a full decoder.
"""
from __future__ import annotations

import hashlib
import struct
import zlib

from agent.adult.models import (
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_INPUT_BYTES,
    MAX_IMAGE_OUTPUT_BYTES,
    MAX_IMAGE_PIXELS,
    ValidatedImage,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8"
_WEBP_SIGNATURE = b"RIFF"
_WEBP_FORM = b"WEBP"
_ANIMATED_PNG_CHUNKS = {b"acTL", b"fcTL", b"fdAT"}
_PNG_CRITICAL_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
_PNG_COLOR_DEPTHS = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}
_JPEG_SOF_MARKERS = {0xC0, 0xC1, 0xC2}
_JPEG_LENGTH_MARKERS = (
    _JPEG_SOF_MARKERS
    | {0xC4, 0xCC, 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF, 0xFE}
    | set(range(0xE0, 0xF0))
)
_WEBP_METADATA = {b"ICCP", b"EXIF", b"XMP "}
_WEBP_STATIC_CHUNKS = _WEBP_METADATA | {b"VP8X", b"VP8 ", b"VP8L", b"ALPH"}


class ImageValidationError(ValueError):
    """Stable, content-free image validation failure."""

    _CODES = {
        "invalid_image", "input_too_large", "output_too_large",
        "unsupported_format", "invalid_dimensions", "checksum_mismatch",
        "unsupported_feature",
    }

    def __init__(self, error_code: str = "invalid_image") -> None:
        self.error_code = error_code if error_code in self._CODES else "invalid_image"
        super().__init__(self.error_code)


def _fail(code: str = "invalid_image") -> None:
    raise ImageValidationError(code)


def _dimensions(width: int, height: int) -> None:
    if (
        isinstance(width, bool) or isinstance(height, bool)
        or not isinstance(width, int) or not isinstance(height, int)
        or width < 1 or height < 1
        or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        _fail("invalid_dimensions")


def _parse_png(data: bytes) -> tuple[str, int, int]:
    if len(data) < 8 or data[:8] != _PNG_SIGNATURE:
        _fail("unsupported_format")
    offset = 8
    width = height = color_type = bit_depth = None
    seen_ihdr = seen_plte = seen_idat = seen_iend = False
    seen_trns = idat_closed = False
    idat_bytes = 0
    palette_entries = 0

    while offset < len(data):
        if offset + 12 > len(data):
            _fail()
        chunk_length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_length
        crc_end = chunk_end + 4
        if crc_end > len(data):
            _fail()
        if len(kind) != 4 or any(
            not (65 <= value <= 90 or 97 <= value <= 122) for value in kind
        ):
            _fail()
        # PNG's reserved third chunk-name bit must be zero (uppercase).
        if kind[2] & 0x20:
            _fail()
        if zlib.crc32(kind + data[chunk_start:chunk_end]) & 0xFFFFFFFF != struct.unpack_from(">I", data, chunk_end)[0]:
            _fail("checksum_mismatch")
        if kind in _ANIMATED_PNG_CHUNKS:
            _fail("unsupported_feature")
        if not seen_ihdr and kind != b"IHDR":
            _fail()

        if seen_idat and kind != b"IDAT":
            idat_closed = True

        if kind == b"IHDR":
            if seen_ihdr or offset != 8 or chunk_length != 13:
                _fail()
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data[chunk_start:chunk_end]
            )
            _dimensions(width, height)
            if (
                color_type not in _PNG_COLOR_DEPTHS
                or bit_depth not in _PNG_COLOR_DEPTHS[color_type]
                or compression != 0 or filtering != 0 or interlace not in (0, 1)
            ):
                _fail()
            seen_ihdr = True

        elif kind == b"PLTE":
            if (
                not seen_ihdr or seen_plte or seen_idat
                or color_type in (0, 4)
                or chunk_length == 0 or chunk_length > 768 or chunk_length % 3
            ):
                _fail()
            palette_entries = chunk_length // 3
            if color_type == 3 and palette_entries > (1 << bit_depth):
                _fail()
            seen_plte = True

        elif kind == b"tRNS":
            if not seen_ihdr or seen_trns or seen_idat:
                _fail()
            if (
                (color_type == 0 and chunk_length != 2)
                or (color_type == 2 and chunk_length != 6)
                or (color_type == 3 and (
                    not seen_plte or chunk_length < 1 or chunk_length > palette_entries
                ))
                or color_type in (4, 6)
            ):
                _fail()
            seen_trns = True

        elif kind == b"IDAT":
            if not seen_ihdr or idat_closed or (color_type == 3 and not seen_plte):
                _fail()
            seen_idat = True
            idat_bytes += chunk_length

        elif kind == b"IEND":
            if not seen_ihdr or not seen_idat or chunk_length != 0 or idat_bytes == 0:
                _fail()
            seen_iend = True

        else:
            if not seen_ihdr:
                _fail()
            # Unknown critical chunks cannot safely be ignored.
            if kind[0] & 0x20 == 0 and kind not in _PNG_CRITICAL_CHUNKS:
                _fail("unsupported_feature")

        offset = crc_end
        if seen_iend:
            if offset != len(data):
                _fail()
            break

    if not (seen_ihdr and seen_idat and seen_iend and width is not None and height is not None):
        _fail()
    return "image/png", width, height


def _read_jpeg_marker(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data) or data[offset] != 0xFF:
        _fail()
    while offset < len(data) and data[offset] == 0xFF:
        offset += 1
    if offset >= len(data):
        _fail()
    marker = data[offset]
    if marker == 0:
        _fail()
    return marker, offset + 1


def _read_jpeg_scan_marker(data: bytes, offset: int) -> tuple[int, int, int]:
    while offset < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker_start = offset
        offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            _fail()
        marker = data[offset]
        offset += 1
        if marker == 0x00 or 0xD0 <= marker <= 0xD7:
            continue
        return marker, offset, marker_start
    _fail()
    raise AssertionError("unreachable")


def _parse_jpeg(data: bytes) -> tuple[str, int, int]:
    if len(data) < 4 or not data.startswith(_JPEG_SIGNATURE):
        _fail("unsupported_format")
    offset = 2
    width = height = component_count = None
    seen_sof = seen_sos = False
    in_scan = False
    scan_data_start = 0

    while offset < len(data):
        if in_scan:
            marker, offset, marker_start = _read_jpeg_scan_marker(data, offset)
            if marker_start <= scan_data_start:
                _fail()
            in_scan = False
        else:
            marker, offset = _read_jpeg_marker(data, offset)

        if marker == 0xD9:
            if not (seen_sof and seen_sos) or offset != len(data):
                _fail()
            if width is None or height is None:
                _fail()
            return "image/jpeg", width, height

        if marker == 0xD8 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            _fail("unsupported_feature")
        if marker not in _JPEG_LENGTH_MARKERS:
            _fail("unsupported_feature")
        if offset + 2 > len(data):
            _fail()
        segment_length = struct.unpack_from(">H", data, offset)[0]
        if segment_length < 2:
            _fail()
        segment_start = offset + 2
        segment_end = offset + segment_length
        if segment_end > len(data):
            _fail()
        segment = data[segment_start:segment_end]

        if marker in _JPEG_SOF_MARKERS:
            if seen_sof or len(segment) < 6:
                _fail()
            precision, raw_height, raw_width, component_count = struct.unpack_from(">BHHB", segment, 0)
            if (
                precision != 8 or component_count not in (1, 3, 4)
                or len(segment) != 6 + (3 * component_count)
            ):
                _fail("unsupported_feature")
            component_ids = [segment[6 + (3 * index)] for index in range(component_count)]
            if len(component_ids) != len(set(component_ids)):
                _fail()
            _dimensions(raw_width, raw_height)
            width, height = raw_width, raw_height
            seen_sof = True

        elif marker == 0xDA:
            if not seen_sof or component_count is None or len(segment) < 4:
                _fail()
            scan_components = segment[0]
            if (
                scan_components < 1 or scan_components > component_count
                or len(segment) != 4 + (2 * scan_components)
            ):
                _fail()
            scan_ids = [segment[1 + (2 * index)] for index in range(scan_components)]
            if len(scan_ids) != len(set(scan_ids)):
                _fail()
            seen_sos = True
            in_scan = True
            scan_data_start = segment_end

        offset = segment_end

    _fail()
    raise AssertionError("unreachable")


def _webp_chunk(data: bytes, offset: int) -> tuple[bytes, bytes, int]:
    if offset + 8 > len(data):
        _fail()
    kind = data[offset:offset + 4]
    size = struct.unpack_from("<I", data, offset + 4)[0]
    start = offset + 8
    end = start + size
    padded_end = end + (size & 1)
    if padded_end > len(data):
        _fail()
    if size & 1 and data[end] != 0:
        _fail()
    return kind, data[start:end], padded_end


def _vp8_dimensions(payload: bytes) -> tuple[int, int]:
    if (
        len(payload) < 10 or payload[0] & 0x01
        or payload[3:6] != b"\x9d\x01\x2a"
    ):
        _fail()
    width = struct.unpack_from("<H", payload, 6)[0] & 0x3FFF
    height = struct.unpack_from("<H", payload, 8)[0] & 0x3FFF
    _dimensions(width, height)
    return width, height


def _vp8l_dimensions(payload: bytes) -> tuple[int, int, bool]:
    if len(payload) < 5 or payload[0] != 0x2F:
        _fail()
    b1, b2, b3, b4 = payload[1:5]
    width = 1 + b1 + ((b2 & 0x3F) << 8)
    height = 1 + ((b2 & 0xC0) >> 6) + (b3 << 2) + ((b4 & 0x0F) << 10)
    alpha_used = bool(b4 & 0x10)
    if b4 >> 5:
        _fail("unsupported_feature")
    _dimensions(width, height)
    return width, height, alpha_used


def _parse_webp(data: bytes) -> tuple[str, int, int]:
    if len(data) < 20 or data[:4] != _WEBP_SIGNATURE or data[8:12] != _WEBP_FORM:
        _fail("unsupported_format")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size != len(data) - 8:
        _fail()
    chunks: list[tuple[bytes, bytes]] = []
    offset = 12
    while offset < len(data):
        kind, payload, offset = _webp_chunk(data, offset)
        if kind in {b"ANIM", b"ANMF"}:
            _fail("unsupported_feature")
        if kind not in _WEBP_STATIC_CHUNKS:
            _fail("unsupported_feature")
        chunks.append((kind, payload))
    if offset != len(data) or not chunks:
        _fail()

    has_extended = chunks[0][0] == b"VP8X"
    if not has_extended:
        if len(chunks) != 1 or chunks[0][0] not in {b"VP8 ", b"VP8L"}:
            _fail("unsupported_feature")
        if chunks[0][0] == b"VP8 ":
            width, height = _vp8_dimensions(chunks[0][1])
        else:
            width, height, _alpha_used = _vp8l_dimensions(chunks[0][1])
        return "image/webp", width, height

    vp8x = chunks[0][1]
    if len(vp8x) != 10:
        _fail()
    flags = vp8x[0]
    if flags & 0xC1:
        _fail("unsupported_feature")
    if flags & 0x02:
        _fail("unsupported_feature")
    if vp8x[1:4] != b"\x00\x00\x00":
        _fail()
    canvas_width = 1 + vp8x[4] + (vp8x[5] << 8) + (vp8x[6] << 16)
    canvas_height = 1 + vp8x[7] + (vp8x[8] << 8) + (vp8x[9] << 16)
    _dimensions(canvas_width, canvas_height)

    kinds = [kind for kind, _payload in chunks]
    if kinds.count(b"VP8X") != 1:
        _fail()
    image_kinds = [kind for kind in kinds if kind in {b"VP8 ", b"VP8L"}]
    if len(image_kinds) != 1:
        _fail()
    image_index = next(i for i, kind in enumerate(kinds) if kind in {b"VP8 ", b"VP8L"})
    for metadata_kind in _WEBP_METADATA:
        if kinds.count(metadata_kind) > 1:
            _fail()
    for kind in (b"ICCP", b"ALPH"):
        if kind in kinds and kinds.index(kind) > image_index:
            _fail()
    for kind in (b"EXIF", b"XMP "):
        if kind in kinds and kinds.index(kind) < image_index:
            _fail()
    for kind in _WEBP_METADATA:
        flag = {
            b"ICCP": 0x20,
            b"EXIF": 0x08,
            b"XMP ": 0x04,
        }[kind]
        if bool(flags & flag) != (kind in kinds):
            _fail()
    if any(not payload for kind, payload in chunks if kind in _WEBP_METADATA):
        _fail()

    image_kind = image_kinds[0]
    image_payload = chunks[image_index][1]
    if image_kind == b"VP8 ":
        width, height = _vp8_dimensions(image_payload)
        has_alpha = b"ALPH" in kinds
        if has_alpha != bool(flags & 0x10):
            _fail()
        if has_alpha:
            alpha_payload = chunks[kinds.index(b"ALPH")][1]
            if not alpha_payload or alpha_payload[0] & 0xC0:
                _fail()
    else:
        width, height, alpha_used = _vp8l_dimensions(image_payload)
        if b"ALPH" in kinds or alpha_used != bool(flags & 0x10):
            _fail()
    if width != canvas_width or height != canvas_height:
        _fail()
    return "image/webp", canvas_width, canvas_height


def validate_image(data: bytes, *, output: bool = False) -> ValidatedImage:
    """Validate a supported image container and return safe, validator-derived metadata.

    Input images are limited to 15 MiB; generated/edited outputs are limited to 20 MiB.
    """
    if type(output) is not bool:
        raise TypeError("output must be boolean.")
    if not isinstance(data, bytes):
        _fail()
    limit = MAX_IMAGE_OUTPUT_BYTES if output else MAX_IMAGE_INPUT_BYTES
    if not data:
        _fail()
    if len(data) > limit:
        _fail("output_too_large" if output else "input_too_large")

    if data.startswith(_PNG_SIGNATURE):
        mime_type, width, height = _parse_png(data)
    elif data.startswith(_JPEG_SIGNATURE):
        mime_type, width, height = _parse_jpeg(data)
    elif data.startswith(_WEBP_SIGNATURE):
        mime_type, width, height = _parse_webp(data)
    else:
        _fail("unsupported_format")
    _dimensions(width, height)
    return ValidatedImage(
        mime_type=mime_type,
        byte_count=len(data),
        width=width,
        height=height,
        sha256=hashlib.sha256(data).hexdigest(),
    )
