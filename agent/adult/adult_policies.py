"""Deterministic, non-semantic input/output policy gates for Adult text tasks."""
from __future__ import annotations

import unicodedata

from agent.adult.models import PolicyDecision

_ALLOWED_TASK_TYPES = frozenset({"text.analyze", "text.generate", "text.rewrite"})
_MAX_SOURCE_CHARS = 20_000
_MAX_INSTRUCTION_CHARS = 4_000
_MAX_OUTPUT_CHARS = 20_000
_MAX_OUTPUT_BYTES = 80 * 1024
_ALLOWED_CONTROLS = frozenset({"\t", "\n", "\r"})


def _text_problem(value: str) -> str | None:
    """Return a stable reason code; never include the supplied text in errors."""
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "invalid_utf8"
    if any(
        unicodedata.category(char) == "Cc" and char not in _ALLOWED_CONTROLS
        for char in value
    ):
        return "prohibited_control_character"
    return None


def _decision(reasons: list[str]) -> PolicyDecision:
    return PolicyDecision(not reasons, tuple(dict.fromkeys(reasons)))


class AdultPolicies:
    """Baseline boundary validation only; this class is not semantic moderation."""

    def validate_text_task(
        self, task_type: str, instruction: str | None, text: str,
    ) -> PolicyDecision:
        reasons: list[str] = []
        if not isinstance(task_type, str) or task_type not in _ALLOWED_TASK_TYPES:
            reasons.append("unsupported_task_type")

        if not isinstance(text, str):
            reasons.append("source_text_invalid")
        else:
            if not text.strip():
                reasons.append("source_text_empty")
            if len(text) > _MAX_SOURCE_CHARS:
                reasons.append("source_text_too_long")
            problem = _text_problem(text)
            if problem:
                reasons.append(problem)

        if instruction is not None:
            if not isinstance(instruction, str):
                reasons.append("instruction_invalid")
            else:
                if len(instruction) > _MAX_INSTRUCTION_CHARS:
                    reasons.append("instruction_too_long")
                problem = _text_problem(instruction)
                if problem:
                    reasons.append(problem)

        return _decision(reasons)

    def validate_text_output(self, task_type: str, output_text: str) -> PolicyDecision:
        reasons: list[str] = []
        if not isinstance(task_type, str) or task_type not in _ALLOWED_TASK_TYPES:
            reasons.append("unsupported_task_type")

        if not isinstance(output_text, str):
            reasons.append("output_invalid")
        else:
            if not output_text.strip():
                reasons.append("output_empty")
            if len(output_text) > _MAX_OUTPUT_CHARS:
                reasons.append("output_too_long")
            try:
                encoded = output_text.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                reasons.append("invalid_utf8")
            else:
                if len(encoded) > _MAX_OUTPUT_BYTES:
                    reasons.append("output_too_many_bytes")
            problem = _text_problem(output_text)
            if problem:
                reasons.append(problem)

        return _decision(reasons)
