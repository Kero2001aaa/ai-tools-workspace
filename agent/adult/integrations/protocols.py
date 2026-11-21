"""Typed, payload-reference-only adapter contracts for Adult operations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent.adult.models import _identifier


@dataclass(frozen=True)
class TextRequest:
    task_id: str
    step_id: str
    input_payload_id: str

    def __post_init__(self) -> None:
        _identifier("task_id", self.task_id)
        _identifier("step_id", self.step_id)
        _identifier("input_payload_id", self.input_payload_id)


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


@dataclass(frozen=True)
class ArtifactResult:
    artifact_id: str
    mime_type: str
    byte_count: int
    sha256: str


class TextModelAdapter(Protocol):
    def invoke(self, request: TextRequest) -> AdapterResult: ...


class ImageUnderstandingAdapter(Protocol):
    def analyze(self, request: ImageAnalysisRequest) -> AdapterResult: ...


class ImageGenerationAdapter(Protocol):
    def generate(self, request: ImageGenerationRequest) -> ArtifactResult: ...


class ImageEditingAdapter(Protocol):
    def edit(self, request: ImageEditRequest) -> ArtifactResult: ...
