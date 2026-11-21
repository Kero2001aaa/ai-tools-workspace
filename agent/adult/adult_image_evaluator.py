"""Deterministic, bounded evaluation for Adult image-task outputs."""
from __future__ import annotations

import unicodedata
from typing import Any

from agent.adult.adult_image_policies import AdultImagePolicies
from agent.adult.adult_image_validator import ImageValidationError, validate_image
from agent.adult.integrations.protocols import ImageAdapterResult, ImageAnalysisResult
from agent.adult.models import EvaluationResult, PolicyDecision

_SUPPORTED_TASK_TYPES = frozenset({"image.analyze", "image.generate", "image.edit"})
_ALLOWED_CONTROLS = frozenset({"\\t", "\\n", "\\r"})
_MAX_ANALYSIS_CHARS = 20_000
_MAX_ANALYSIS_BYTES = 80 * 1024
_ANALYSIS_CHECK_COUNT = 8
_IMAGE_CHECK_COUNT = 6

_ANALYSIS_RETRYABLE_REASONS = frozenset({
    "analysis_text_invalid",
    "invalid_utf8",
    "output_empty",
    "output_too_long",
    "output_too_many_bytes",
    "prohibited_control_character",
})
_IMAGE_RETRYABLE_REASONS = frozenset({
    "declared_mime_invalid",
    "declared_mime_mismatch",
    "output_mime_not_allowed",
    "output_image_too_large",
    "output_dimensions_exceed_route",
    "output_pixel_limit_exceeded",
})
_VALIDATOR_REASON_MAP = {
    "output_too_large": "image_output_too_large",
    "unsupported_format": "image_output_unsupported_format",
    "invalid_dimensions": "image_output_invalid_dimensions",
    "checksum_mismatch": "image_output_checksum_mismatch",
    "unsupported_feature": "image_output_unsupported_feature",
    "invalid_image": "image_output_invalid",
}


def _result(
    checks: list[str],
    reasons: list[str],
    *,
    retryable: bool,
    expected_checks: int,
) -> EvaluationResult:
    normalized_reasons = tuple(dict.fromkeys(reasons))[:8]
    normalized_checks = tuple(dict.fromkeys(checks))[:16]
    accepted = not normalized_reasons and len(normalized_checks) == expected_checks
    if not accepted and not normalized_reasons:
        normalized_reasons = ("output_invalid",)
    return EvaluationResult(
        accepted=accepted,
        score=len(normalized_checks) / expected_checks,
        checks=normalized_checks,
        reason_codes=normalized_reasons,
        needs_revision=bool(retryable and normalized_reasons),
    )


class AdultImageEvaluator:
    """Evaluates structural output validity only; it does not assess semantic quality."""

    def __init__(self, policies: AdultImagePolicies | None = None) -> None:
        self._policies = policies

    def evaluate(
        self,
        task_type: str,
        output: object,
        *,
        output_mime_types: tuple[str, ...] | None = None,
        max_bytes: int | None = None,
        max_width: int | None = None,
        max_height: int | None = None,
    ) -> EvaluationResult:
        if not isinstance(task_type, str) or task_type not in _SUPPORTED_TASK_TYPES:
            return _result(
                [], ["unsupported_task_type"], retryable=False,
                expected_checks=_ANALYSIS_CHECK_COUNT,
            )
        if task_type == "image.analyze":
            return self._evaluate_analysis(task_type, output)
        return self._evaluate_image(
            task_type,
            output,
            output_mime_types=output_mime_types,
            max_bytes=max_bytes,
            max_width=max_width,
            max_height=max_height,
        )

    def _evaluate_analysis(
        self, task_type: str, output: object,
    ) -> EvaluationResult:
        checks = ["supported_task_type"]
        if not isinstance(output, ImageAnalysisResult):
            return _result(
                checks, ["analysis_result_invalid"], retryable=False,
                expected_checks=_ANALYSIS_CHECK_COUNT,
            )
        checks.append("analysis_result_type")

        text = output.text
        reasons: list[str] = []
        retryable = False
        encoded: bytes | None = None
        if isinstance(text, str):
            try:
                encoded = text.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                reasons.append("invalid_utf8")
                retryable = True
            else:
                checks.append("valid_utf8")
        else:
            reasons.append("analysis_text_invalid")
            retryable = True

        non_empty = isinstance(text, str) and bool(text.strip())
        if non_empty:
            checks.append("non_empty")
        elif isinstance(text, str):
            reasons.append("output_empty")
            retryable = True

        within_chars = isinstance(text, str) and len(text) <= _MAX_ANALYSIS_CHARS
        if within_chars:
            checks.append("within_character_limit")
        elif isinstance(text, str):
            reasons.append("output_too_long")
            retryable = True

        within_bytes = encoded is not None and len(encoded) <= _MAX_ANALYSIS_BYTES
        if within_bytes:
            checks.append("within_utf8_byte_limit")
        elif encoded is not None:
            reasons.append("output_too_many_bytes")
            retryable = True

        controls_valid = isinstance(text, str) and not any(
            unicodedata.category(character) == "Cc" and character not in _ALLOWED_CONTROLS
            for character in text
        )
        if controls_valid:
            checks.append("allowed_control_characters")
        elif isinstance(text, str):
            reasons.append("prohibited_control_character")
            retryable = True

        decision = self._analysis_policy(task_type, text)
        if decision is None:
            reasons.append("policy_unavailable")
            retryable = False
        elif decision.allowed:
            checks.append("output_policy_allowed")
        elif all(code in _ANALYSIS_RETRYABLE_REASONS for code in decision.reason_codes):
            reasons.extend(decision.reason_codes)
            retryable = True
        else:
            reasons.append("policy_rejected")
            retryable = False

        return _result(
            checks, reasons, retryable=retryable,
            expected_checks=_ANALYSIS_CHECK_COUNT,
        )

    def _analysis_policy(self, task_type: str, text: object) -> PolicyDecision | None:
        if self._policies is None:
            return None
        try:
            decision = self._policies.validate_analysis_output(task_type, text)
        except Exception:
            return None
        return decision if isinstance(decision, PolicyDecision) else None

    def _evaluate_image(
        self,
        task_type: str,
        output: object,
        *,
        output_mime_types: tuple[str, ...] | None,
        max_bytes: int | None,
        max_width: int | None,
        max_height: int | None,
    ) -> EvaluationResult:
        checks = ["supported_task_type"]
        if not isinstance(output, ImageAdapterResult):
            return _result(
                checks, ["image_result_invalid"], retryable=False,
                expected_checks=_IMAGE_CHECK_COUNT,
            )
        checks.append("image_result_type")

        try:
            validated = validate_image(output.image_bytes, output=True)
        except ImageValidationError as exc:
            reason = _VALIDATOR_REASON_MAP.get(exc.error_code, "image_output_invalid")
            return _result(
                checks, [reason], retryable=True,
                expected_checks=_IMAGE_CHECK_COUNT,
            )
        except Exception:
            return _result(
                checks, ["image_output_invalid"], retryable=True,
                expected_checks=_IMAGE_CHECK_COUNT,
            )
        checks.append("valid_image_container")

        if isinstance(output.mime_type, str) and output.mime_type in {
            "image/png", "image/jpeg", "image/webp",
        }:
            checks.append("declared_mime_supported")

        decision = self._image_policy(
            task_type,
            output.mime_type,
            validated,
            output_mime_types=output_mime_types,
            max_bytes=max_bytes,
            max_width=max_width,
            max_height=max_height,
        )
        if decision is None:
            return _result(
                checks, ["policy_unavailable"], retryable=False,
                expected_checks=_IMAGE_CHECK_COUNT,
            )

        reasons: list[str] = []
        retryable = False
        if output.mime_type == validated.mime_type:
            checks.append("declared_mime_matches_detected")
        if decision.allowed:
            checks.append("output_policy_allowed")
        elif all(code in _IMAGE_RETRYABLE_REASONS for code in decision.reason_codes):
            reasons.extend(decision.reason_codes)
            retryable = True
        else:
            reasons.append("policy_rejected")

        return _result(
            checks, reasons, retryable=retryable,
            expected_checks=_IMAGE_CHECK_COUNT,
        )

    def _image_policy(
        self,
        task_type: str,
        declared_mime_type: object,
        validated: object,
        *,
        output_mime_types: tuple[str, ...] | None,
        max_bytes: int | None,
        max_width: int | None,
        max_height: int | None,
    ) -> PolicyDecision | None:
        if self._policies is None:
            return None
        try:
            decision: Any = self._policies.validate_image_output(
                task_type,
                declared_mime_type,
                validated,
                output_mime_types=output_mime_types,
                max_bytes=max_bytes,
                max_width=max_width,
                max_height=max_height,
            )
        except Exception:
            return None
        return decision if isinstance(decision, PolicyDecision) else None
