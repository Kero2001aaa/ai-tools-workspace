"""Typed, payload-reference-only adapter contracts for Adult operations."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Protocol

from agent.adult.models import AdultModelValidationError, _identifier, _integer

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_USAGE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class TextRequest:
    task_id: str
    step_id: str
    task_type: str
    input_payload_id: str
    max_output_chars: int = 20000

    def __post_init__(self) -> None:
        _identifier("task_id", self.task_id)
        _identifier("step_id", self.step_id)
        _identifier("input_payload_id", self.input_payload_id)
        if self.task_type not in {"text.analyze", "text.generate", "text.rewrite"}:
            raise AdultModelValidationError("TextRequest task_type is unsupported.")
        _integer("max_output_chars", self.max_output_chars, 1, 20000)


@dataclass(frozen=True)
class TextAdapterResult:
    text: str = field(repr=False)
    usage: dict[str, int]
    cost_microusd: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise AdultModelValidationError("TextAdapterResult text must be a string.")
        if not isinstance(self.usage, dict) or len(self.usage) > 16:
            raise AdultModelValidationError("usage must be a bounded counter object.")
        for key, value in self.usage.items():
            if not isinstance(key, str) or not _USAGE_KEY_RE.fullmatch(key):
                raise AdultModelValidationError("usage contains an invalid counter name.")
            _integer(f"usage.{key}", value, 0, 10**12)
        if self.cost_microusd is not None:
            _integer("cost_microusd", self.cost_microusd, 0, 10**12)



def _validate_image_result_usage(usage: dict[str, int], cost_microusd: int | None) -> None:
    if not isinstance(usage, dict) or len(usage) > 16:
        raise AdultModelValidationError("usage must be a bounded counter object.")
    for key, value in usage.items():
        if not isinstance(key, str) or not _USAGE_KEY_RE.fullmatch(key):
            raise AdultModelValidationError("usage contains an invalid counter name.")
        _integer(f"usage.{key}", value, 0, 10**12)
    if cost_microusd is not None:
        _integer("cost_microusd", cost_microusd, 0, 10**12)


@dataclass(frozen=True)
class ImageAnalysisResult:
    """Ephemeral analysis text returned by an image adapter."""

    text: str = field(repr=False)
    usage: dict[str, int]
    cost_microusd: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise AdultModelValidationError("Image analysis text must be a string.")
        try:
            encoded = self.text.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise AdultModelValidationError("Image analysis text must be valid UTF-8.") from exc
        if len(self.text) > 20_000 or len(encoded) > 80 * 1024:
            raise AdultModelValidationError("Image analysis text exceeds its size limit.")
        _validate_image_result_usage(self.usage, self.cost_microusd)


@dataclass(frozen=True)
class ImageAdapterResult:
    """Ephemeral generated image bytes; the pipeline owns validation and persistence."""

    image_bytes: bytes = field(repr=False)
    mime_type: str
    usage: dict[str, int]
    cost_microusd: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.image_bytes, bytes):
            raise AdultModelValidationError("Image adapter output must be bytes.")
        _integer("image_bytes", len(self.image_bytes), 1, 20 * 1024 * 1024)
        if not isinstance(self.mime_type, str) or self.mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise AdultModelValidationError("Image adapter output MIME type is unsupported.")
        _validate_image_result_usage(self.usage, self.cost_microusd)


@dataclass(frozen=True)
class ImageAnalysisRequest:
    task_id: str
    step_id: str
    input_payload_id: str
    mime_type: str

    def __post_init__(self) -> None:
        _identifier("task_id", self.task_id)
        _identifier("step_id", self.step_id)
        _identifier("input_payload_id", self.input_payload_id)
        if self.mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Unsupported image MIME type.")


@dataclass(frozen=True)
class ImageGenerationRequest:
    task_id: str
    step_id: str
    prompt_payload_id: str

    def __post_init__(self) -> None:
        _identifier("task_id", self.task_id)
        _identifier("step_id", self.step_id)
        _identifier("prompt_payload_id", self.prompt_payload_id)


@dataclass(frozen=True)
class ImageEditRequest:
    task_id: str
    step_id: str
    image_payload_id: str
    prompt_payload_id: str
    mime_type: str

    def __post_init__(self) -> None:
        _identifier("task_id", self.task_id)
        _identifier("step_id", self.step_id)
        _identifier("image_payload_id", self.image_payload_id)
        _identifier("prompt_payload_id", self.prompt_payload_id)
        if self.mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Unsupported image MIME type.")


@dataclass(frozen=True)
class AdapterResult:
    output_ref: str
    usage: dict[str, int]
    cost_microusd: int | None = None

    def __post_init__(self) -> None:
        _identifier("output_ref", self.output_ref)
        if not isinstance(self.usage, dict) or len(self.usage) > 16:
            raise AdultModelValidationError("usage must be a bounded counter object.")
        for key, value in self.usage.items():
            if not isinstance(key, str) or not _USAGE_KEY_RE.fullmatch(key):
                raise AdultModelValidationError("usage contains an invalid counter name.")
            _integer(f"usage.{key}", value, 0, 10**12)
        if self.cost_microusd is not None:
            _integer("cost_microusd", self.cost_microusd, 0, 10**12)


@dataclass(frozen=True)
class ArtifactResult:
    artifact_id: str
    mime_type: str
    byte_count: int
    sha256: str

    def __post_init__(self) -> None:
        _identifier("artifact_id", self.artifact_id)
        if self.mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise AdultModelValidationError("Artifact MIME type is not supported.")
        _integer("byte_count", self.byte_count, 1, 20 * 1024 * 1024)
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise AdultModelValidationError("sha256 must be a lowercase SHA-256 digest.")


class TextModelAdapter(Protocol):
    def invoke(self, request: TextRequest) -> TextAdapterResult: ...


class ImageUnderstandingAdapter(Protocol):
    def analyze(self, request: ImageAnalysisRequest) -> ImageAnalysisResult: ...


class ImageGenerationAdapter(Protocol):
    def generate(self, request: ImageGenerationRequest) -> ImageAdapterResult: ...


class ImageEditingAdapter(Protocol):
    def edit(self, request: ImageEditRequest) -> ImageAdapterResult: ...
