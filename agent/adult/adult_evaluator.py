"""Deterministic, bounded evaluation for Adult text outputs."""
from __future__ import annotations

from agent.adult.adult_policies import AdultPolicies
from agent.adult.models import EvaluationResult, PolicyDecision

_SUPPORTED_TASK_TYPES = frozenset({"text.analyze", "text.generate", "text.rewrite"})
_MAX_OUTPUT_CHARS = 20_000
_MAX_OUTPUT_BYTES = 80 * 1024
_ALLOWED_CONTROLS = frozenset({"\t", "\n", "\r"})
_QUALITY_REASON_CODES = frozenset({
    "output_invalid", "invalid_utf8", "output_empty",
    "prohibited_control_character", "output_too_long", "output_too_many_bytes",
})


class AdultEvaluator:
    """Scores mandatory structural checks only; never estimates semantic quality."""

    def __init__(self, policies: AdultPolicies | None = None) -> None:
        self._policies = policies

    def evaluate(self, task_type: str, output_text: object) -> EvaluationResult:
        checks: list[str] = []
        reasons: list[str] = []
        quality_reasons: list[str] = []

        task_supported = isinstance(task_type, str) and task_type in _SUPPORTED_TASK_TYPES
        if task_supported:
            checks.append("supported_task_type")
        else:
            reasons.append("unsupported_task_type")

        is_text = isinstance(output_text, str)
        if is_text:
            checks.append("output_is_text")
        else:
            reasons.append("output_invalid")
            quality_reasons.append("output_invalid")

        encoded: bytes | None = None
        valid_utf8 = False
        if is_text:
            try:
                encoded = output_text.encode("utf-8", errors="strict")
                valid_utf8 = True
            except UnicodeEncodeError:
                reasons.append("invalid_utf8")
                quality_reasons.append("invalid_utf8")
        if valid_utf8:
            checks.append("valid_utf8")

        non_empty = is_text and bool(output_text.strip())
        if non_empty:
            checks.append("non_empty")
        else:
            if is_text:
                reasons.append("output_empty")
                quality_reasons.append("output_empty")

        controls_valid = is_text and not any(
            ord(char) < 32 and char not in _ALLOWED_CONTROLS
            or ord(char) == 127
            for char in output_text
        )
        if controls_valid:
            checks.append("allowed_control_characters")
        elif is_text:
            reasons.append("prohibited_control_character")
            quality_reasons.append("prohibited_control_character")

        within_chars = is_text and len(output_text) <= _MAX_OUTPUT_CHARS
        if within_chars:
            checks.append("within_character_limit")
        elif is_text:
            reasons.append("output_too_long")
            quality_reasons.append("output_too_long")

        within_bytes = encoded is not None and len(encoded) <= _MAX_OUTPUT_BYTES
        if within_bytes:
            checks.append("within_utf8_byte_limit")
        elif encoded is not None:
            reasons.append("output_too_many_bytes")
            quality_reasons.append("output_too_many_bytes")

        policy_allowed = False
        if self._policies is None:
            reasons.append("policy_unavailable")
        else:
            try:
                decision = self._policies.validate_text_output(task_type, output_text)
            except Exception:
                decision = None
            if not isinstance(decision, PolicyDecision):
                reasons.append("policy_unavailable")
            elif decision.allowed:
                policy_allowed = True
            else:
                structural = [code for code in decision.reason_codes
                              if code in _QUALITY_REASON_CODES]
                if structural:
                    reasons.extend(structural)
                    quality_reasons.extend(structural)
                else:
                    reasons.append("policy_rejected")

        if policy_allowed:
            checks.append("output_policy_allowed")

        # Keep reason codes unique and bounded. A malformed policy cannot inject
        # arbitrary text because only normalized PolicyDecision codes are accepted.
        normalized_reasons = tuple(dict.fromkeys(reasons))[:8]
        accepted = len(checks) == 8 and not normalized_reasons
        score = len(checks) / 8
        needs_revision = bool(quality_reasons) and not any(
            code in {"policy_unavailable", "policy_rejected", "unsupported_task_type"}
            for code in normalized_reasons
        )
        return EvaluationResult(
            accepted=accepted,
            score=score,
            checks=tuple(checks),
            reason_codes=normalized_reasons,
            needs_revision=needs_revision,
        )
