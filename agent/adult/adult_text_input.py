"""Strict, bounded text input envelopes for Adult Agent Stage 7."""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from agent.adult.adult_policies import AdultPolicies
from agent.adult.models import PolicyDecision

_SUPPORTED_TASK_TYPES = frozenset({"text.analyze", "text.generate", "text.rewrite"})
_MAX_ENVELOPE_BYTES = 128 * 1024


class TextInputError(ValueError):
    """Stable input-envelope error that never includes supplied text."""

    def __init__(self, error_code: str, reason_codes: tuple[str, ...] = ()) -> None:
        if error_code not in {
            "input_invalid", "input_too_large", "input_malformed",
            "input_schema_invalid", "input_task_mismatch", "input_policy_rejected",
        }:
            error_code = "input_invalid"
        self.error_code = error_code
        self.reason_codes = tuple(
            code for code in reason_codes
            if isinstance(code, str) and code.isascii()
            and code.replace("_", "").isalnum() and len(code) <= 48
        )[:8]
        super().__init__(error_code)


@dataclass(frozen=True)
class TextInputEnvelope:
    task_type: str
    text: str = field(repr=False)
    instruction: str | None = field(default=None, repr=False)


def _policy_check(task_type: str, instruction: str | None, text: str,
                  policies: AdultPolicies) -> None:
    try:
        decision = policies.validate_text_task(task_type, instruction, text)
    except Exception as exc:
        raise TextInputError("input_policy_rejected", ("policy_unavailable",)) from None
    if not isinstance(decision, PolicyDecision):
        raise TextInputError("input_policy_rejected", ("policy_unavailable",))
    if not decision.allowed:
        raise TextInputError("input_policy_rejected", decision.reason_codes)


def encode_text_input(
    task_type: str,
    text: str,
    instruction: str | None = None,
    *,
    policies: AdultPolicies | None = None,
) -> bytes:
    """Validate and encode the canonical four-key UTF-8 JSON envelope."""
    if not isinstance(task_type, str) or task_type not in _SUPPORTED_TASK_TYPES:
        raise TextInputError("input_task_mismatch", ("unsupported_task_type",))
    policy = policies if policies is not None else AdultPolicies()
    _policy_check(task_type, instruction, text, policy)
    try:
        encoded = json.dumps(
            {
                "schema_version": 1,
                "task_type": task_type,
                "text": text,
                "instruction": instruction,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise TextInputError("input_invalid") from None
    if len(encoded) > _MAX_ENVELOPE_BYTES:
        raise TextInputError("input_too_large")
    return encoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TextInputError("input_malformed")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise TextInputError("input_malformed")


def decode_text_input(
    data: bytes,
    expected_task_type: str,
    *,
    policies: AdultPolicies | None = None,
) -> TextInputEnvelope:
    """Decode an untrusted envelope with exact schema and duplicate-key rejection."""
    if not isinstance(data, bytes):
        raise TextInputError("input_invalid")
    if len(data) > _MAX_ENVELOPE_BYTES:
        raise TextInputError("input_too_large")
    if not isinstance(expected_task_type, str) or expected_task_type not in _SUPPORTED_TASK_TYPES:
        raise TextInputError("input_task_mismatch", ("unsupported_task_type",))
    try:
        decoded = data.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except TextInputError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise TextInputError("input_malformed") from None

    if not isinstance(value, dict) or set(value) != {
        "schema_version", "task_type", "text", "instruction",
    }:
        raise TextInputError("input_schema_invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise TextInputError("input_schema_invalid")
    task_type = value["task_type"]
    text = value["text"]
    instruction = value["instruction"]
    if task_type != expected_task_type:
        raise TextInputError("input_task_mismatch")
    if not isinstance(text, str) or (instruction is not None and not isinstance(instruction, str)):
        raise TextInputError("input_schema_invalid")

    policy = policies if policies is not None else AdultPolicies()
    _policy_check(task_type, instruction, text, policy)
    return TextInputEnvelope(task_type=task_type, text=text, instruction=instruction)
