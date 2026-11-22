"""Deterministic, non-semantic policy gates for Adult image tasks."""
from __future__ import annotations

import unicodedata

from agent.adult.models import (
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_INPUT_BYTES,
    MAX_IMAGE_OUTPUT_BYTES,
    MAX_IMAGE_PIXELS,
    SUPPORTED_IMAGE_MIME_TYPES,
    PolicyDecision,
    ValidatedImage,
)

_ALLOWED_CONTROLS = frozenset({"\t", "\n", "\r"})
_MAX_ANALYSIS_CHARS = 20_000
_MAX_ANALYSIS_BYTES = 80 * 1024


def _decision(reasons: list[str]) -> PolicyDecision:
    unique = tuple(dict.fromkeys(reasons))
    return PolicyDecision(not unique, unique)


def _mime_allowlist(value: object, reasons: list[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(sorted(SUPPORTED_IMAGE_MIME_TYPES))
    if not isinstance(value, tuple) or not value or len(value) > len(SUPPORTED_IMAGE_MIME_TYPES):
        reasons.append("route_mime_policy_invalid")
        return ()
    if any(not isinstance(item, str) or item not in SUPPORTED_IMAGE_MIME_TYPES for item in value):
        reasons.append("route_mime_policy_invalid")
        return ()
    if len(value) != len(set(value)):
        reasons.append("route_mime_policy_invalid")
        return ()
    return value


def _effective_limit(value: object, hard_limit: int, reasons: list[str]) -> int:
    if value is None:
        return hard_limit
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        reasons.append("route_limit_invalid")
        return hard_limit
    # A route may be stricter than the application boundary, never looser.
    return min(value, hard_limit)


def _text_problem(value: str) -> tuple[str, ...]:
    reasons: list[str] = []
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return ("invalid_utf8",)
    if not value.strip():
        reasons.append("output_empty")
    if len(value) > _MAX_ANALYSIS_CHARS:
        reasons.append("output_too_long")
    if len(encoded) > _MAX_ANALYSIS_BYTES:
        reasons.append("output_too_many_bytes")
    if any(
        unicodedata.category(character) == "Cc" and character not in _ALLOWED_CONTROLS
        for character in value
    ):
        reasons.append("prohibited_control_character")
    return tuple(reasons)


class AdultImagePolicies:
    """Checks image task boundaries; this class does not perform semantic moderation."""

    def validate_image_input(
        self,
        task_type: str,
        image: ValidatedImage,
        *,
        accepted_mime_types: tuple[str, ...] | None = None,
        max_bytes: int | None = None,
        max_width: int | None = None,
        max_height: int | None = None,
    ) -> PolicyDecision:
        reasons: list[str] = []
        if not isinstance(task_type, str) or task_type not in {"image.analyze", "image.edit"}:
            reasons.append("unsupported_image_input_task")
        if not isinstance(image, ValidatedImage):
            reasons.append("image_metadata_invalid")

        mime_types = _mime_allowlist(accepted_mime_types, reasons)
        byte_limit = _effective_limit(max_bytes, MAX_IMAGE_INPUT_BYTES, reasons)
        width_limit = _effective_limit(max_width, MAX_IMAGE_DIMENSION, reasons)
        height_limit = _effective_limit(max_height, MAX_IMAGE_DIMENSION, reasons)
        if isinstance(image, ValidatedImage):
            if image.mime_type not in mime_types:
                reasons.append("input_mime_not_allowed")
            if image.byte_count > byte_limit:
                reasons.append("input_image_too_large")
            if image.width > width_limit or image.height > height_limit:
                reasons.append("input_dimensions_exceed_route")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                reasons.append("input_pixel_limit_exceeded")
        return _decision(reasons)

    def validate_image_output(
        self,
        task_type: str,
        declared_mime_type: str,
        image: ValidatedImage,
        *,
        output_mime_types: tuple[str, ...] | None = None,
        max_bytes: int | None = None,
        max_width: int | None = None,
        max_height: int | None = None,
    ) -> PolicyDecision:
        reasons: list[str] = []
        if not isinstance(task_type, str) or task_type not in {"image.generate", "image.edit"}:
            reasons.append("unsupported_image_output_task")
        if not isinstance(image, ValidatedImage):
            reasons.append("image_metadata_invalid")
        if output_mime_types is None:
            reasons.append("route_policy_missing")
            mime_types = ()
        else:
            mime_types = _mime_allowlist(output_mime_types, reasons)
        route_limits = (
            ("max_bytes", max_bytes, MAX_IMAGE_OUTPUT_BYTES),
            ("max_width", max_width, MAX_IMAGE_DIMENSION),
            ("max_height", max_height, MAX_IMAGE_DIMENSION),
        )
        effective_limits: dict[str, int] = {}
        for name, value, hard_limit in route_limits:
            if value is None:
                reasons.append("route_policy_missing")
                effective_limits[name] = hard_limit
            elif isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > hard_limit:
                reasons.append("route_limit_invalid")
                effective_limits[name] = hard_limit
            else:
                effective_limits[name] = value
        byte_limit = effective_limits["max_bytes"]
        width_limit = effective_limits["max_width"]
        height_limit = effective_limits["max_height"]
        if not isinstance(declared_mime_type, str) or declared_mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
            reasons.append("declared_mime_invalid")
        if isinstance(image, ValidatedImage):
            if image.mime_type != declared_mime_type:
                reasons.append("declared_mime_mismatch")
            if image.mime_type not in mime_types:
                reasons.append("output_mime_not_allowed")
            if image.byte_count > byte_limit:
                reasons.append("output_image_too_large")
            if image.width > width_limit or image.height > height_limit:
                reasons.append("output_dimensions_exceed_route")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                reasons.append("output_pixel_limit_exceeded")
        return _decision(reasons)

    def validate_analysis_output(self, task_type: str, output_text: str) -> PolicyDecision:
        reasons: list[str] = []
        if not isinstance(task_type, str) or task_type != "image.analyze":
            reasons.append("unsupported_image_analysis_task")
        if not isinstance(output_text, str):
            reasons.append("analysis_text_invalid")
        else:
            reasons.extend(_text_problem(output_text))
        return _decision(reasons)
