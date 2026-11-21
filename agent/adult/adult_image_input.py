"""Strict canonical image-task input envelopes and prompt validation."""
from __future__ import annotations

import json
import unicodedata
from typing import Any

from agent.adult.models import AdultModelValidationError, ImageInputEnvelope

MAX_IMAGE_ENVELOPE_BYTES = 4 * 1024
MAX_IMAGE_PROMPT_CHARS = 4_000
MAX_IMAGE_PROMPT_BYTES = 16 * 1024
_IMAGE_TASK_TYPES = frozenset({"image.analyze", "image.generate", "image.edit"})


class ImageInputError(ValueError):
    """Stable image-input error that never includes envelope or prompt content."""

    def __init__(self, error_code: str) -> None:
        allowed = {"invalid_envelope", "invalid_prompt", "input_too_large"}
        self.error_code = error_code if error_code in allowed else "invalid_envelope"
        super().__init__(self.error_code)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are not supported.")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError("Non-finite JSON values are not supported.")


def _expected_keys(task_type: str) -> set[str]:
    if task_type == "image.analyze":
        return {"schema_version", "task_type", "image_payload_id"}
    if task_type == "image.generate":
        return {"schema_version", "task_type", "prompt_payload_id"}
    if task_type == "image.edit":
        return {"schema_version", "task_type", "image_payload_id", "prompt_payload_id"}
    raise ImageInputError("invalid_envelope")


def encode_image_input(envelope: ImageInputEnvelope) -> bytes:
    """Encode a validated envelope into canonical, bounded UTF-8 JSON bytes."""
    if not isinstance(envelope, ImageInputEnvelope):
        raise ImageInputError("invalid_envelope")
    try:
        encoded = json.dumps(
            envelope.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError, AdultModelValidationError):
        raise ImageInputError("invalid_envelope") from None
    if len(encoded) > MAX_IMAGE_ENVELOPE_BYTES:
        raise ImageInputError("input_too_large")
    return encoded


def decode_image_input(data: bytes, expected_task_type: str) -> ImageInputEnvelope:
    """Decode only canonical JSON with the exact operation-specific reference keys."""
    if not isinstance(data, bytes):
        raise ImageInputError("invalid_envelope")
    if len(data) > MAX_IMAGE_ENVELOPE_BYTES:
        raise ImageInputError("input_too_large")
    if expected_task_type not in _IMAGE_TASK_TYPES:
        raise ImageInputError("invalid_envelope")
    try:
        decoded = data.decode("utf-8", "strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise ImageInputError("invalid_envelope") from None
    if not isinstance(value, dict) or set(value) != _expected_keys(expected_task_type):
        raise ImageInputError("invalid_envelope")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ImageInputError("invalid_envelope")
    if not isinstance(value.get("task_type"), str) or value["task_type"] != expected_task_type:
        raise ImageInputError("invalid_envelope")
    try:
        envelope = ImageInputEnvelope(
            task_type=value["task_type"],
            schema_version=value["schema_version"],
            image_payload_id=value.get("image_payload_id"),
            prompt_payload_id=value.get("prompt_payload_id"),
        )
        if encode_image_input(envelope) != data:
            raise ImageInputError("invalid_envelope")
    except (AdultModelValidationError, ImageInputError, TypeError, ValueError):
        raise ImageInputError("invalid_envelope") from None
    return envelope


def _encode_validated_prompt(prompt: str) -> bytes:
    if not isinstance(prompt, str):
        raise ImageInputError("invalid_prompt")
    try:
        encoded = prompt.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise ImageInputError("invalid_prompt") from None
    if len(encoded) > MAX_IMAGE_PROMPT_BYTES:
        raise ImageInputError("input_too_large")
    if len(prompt) > MAX_IMAGE_PROMPT_CHARS or not prompt.strip():
        raise ImageInputError("invalid_prompt")
    if any(
        unicodedata.category(character) == "Cc" and character not in "\t\n\r"
        for character in prompt
    ):
        raise ImageInputError("invalid_prompt")
    return encoded


def encode_image_prompt(prompt: str) -> bytes:
    """Validate and encode an untrusted prompt for task-scoped payload storage."""
    return _encode_validated_prompt(prompt)


def decode_image_prompt(data: bytes) -> str:
    """Decode a task-scoped prompt payload as strict, bounded UTF-8 text."""
    if not isinstance(data, bytes):
        raise ImageInputError("invalid_prompt")
    if len(data) > MAX_IMAGE_PROMPT_BYTES:
        raise ImageInputError("input_too_large")
    try:
        prompt = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise ImageInputError("invalid_prompt") from None
    _encode_validated_prompt(prompt)
    return prompt
