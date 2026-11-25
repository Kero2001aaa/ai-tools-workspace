"""Strict draft public request contracts only. No registration or execution.

Validation does not establish caller identity, consent, semantic policy approval
or capability availability. Validated payload values must never be logged.
"""
from __future__ import annotations
import base64
import binascii
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from agent.adult.adult_image_validator import validate_image

TaskID = Annotated[str, Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
ArtifactID = Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]
TextType = Literal['text.analyze', 'text.generate', 'text.rewrite']
ImageType = Literal['image.analyze', 'image.generate', 'image.edit']
Mime = Literal['image/png', 'image/jpeg', 'image/webp']
Status = Literal['queued', 'analyzing', 'running', 'evaluating', 'checkpointed',
                 'waiting_user', 'needs_input', 'paused', 'completed', 'failed', 'cancelled']


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)


def _image(encoded, mime):
    try:
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > 15 * 1024 * 1024:
            raise ValueError()
        actual = validate_image(data, output=False)
        if mime is not None and mime != actual.mime_type:
            raise ValueError()
    except (ValueError, TypeError, binascii.Error):
        raise ValueError('invalid_image_input') from None
    return data


class SubmitText(StrictRequest):
    task_type: TextType
    text: str = Field(min_length=1, max_length=20000, repr=False)
    instruction: str = Field(default='', max_length=4000, repr=False)
    retain_payload: bool = False

    @model_validator(mode='after')
    def content(self):
        from agent.adult.adult_text_input import encode_text_input
        encode_text_input(self.task_type, self.text, self.instruction)
        return self


class SubmitImage(StrictRequest):
    task_type: ImageType
    prompt: str = Field(max_length=4000, repr=False)
    image_base64: str | None = Field(default=None, max_length=20971520, repr=False)
    image_mime_type: Mime | None = None
    retain_payload: bool = False

    @model_validator(mode='after')
    def shape(self):
        from agent.adult.adult_image_input import encode_image_prompt
        if self.task_type == 'image.generate':
            if self.image_base64 is not None or self.image_mime_type is not None:
                raise ValueError('unexpected_image_input')
        else:
            if self.image_base64 is None:
                raise ValueError('image_required')
            _image(self.image_base64, self.image_mime_type)
        if self.task_type != 'image.analyze' or self.prompt:
            encode_image_prompt(self.prompt)
        return self


class TaskRef(StrictRequest):
    task_id: TaskID


class RunTask(TaskRef):
    max_seconds: int = Field(default=300, ge=1, le=300)


class ListTasks(StrictRequest):
    status: Status | None = None
    limit: int = Field(default=20, ge=1, le=20)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)


class GetAttempts(TaskRef):
    limit: int = Field(default=20, ge=1, le=20)


class ResumeTask(TaskRef):
    decision: Literal['continue', 'cancel']
    replacement_text: str | None = Field(default=None, min_length=1, max_length=20000, repr=False)
    replacement_image_base64: str | None = Field(default=None, max_length=20971520, repr=False)
    replacement_image_mime_type: Mime | None = None

    @model_validator(mode='after')
    def shape(self):
        text = self.replacement_text is not None
        image = self.replacement_image_base64 is not None
        if (text and image) or (self.decision == 'cancel' and (text or image)):
            raise ValueError('ambiguous_replacement')
        if self.replacement_image_mime_type is not None and not image:
            raise ValueError('replacement_image_required')
        if image:
            _image(self.replacement_image_base64, self.replacement_image_mime_type)
        if text:
            from agent.adult.adult_text_input import encode_text_input
            encode_text_input('text.analyze', self.replacement_text)
        return self


class ArtifactChunk(StrictRequest):
    artifact_id: ArtifactID
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=262144, ge=1, le=262144)


REQUEST_MODELS = {
    'adult_submit_text_task': SubmitText,
    'adult_submit_image_task': SubmitImage,
    'adult_run_task': RunTask,
    'adult_get_task': TaskRef,
    'adult_list_tasks': ListTasks,
    'adult_get_attempts': GetAttempts,
    'adult_resume_task': ResumeTask,
    'adult_cancel_task': TaskRef,
    'adult_get_artifact_chunk': ArtifactChunk,
}


def request_schemas():
    """Return isolated schema objects, without adding tools to any runtime."""
    return {name: model.model_json_schema() for name, model in REQUEST_MODELS.items()}


def validate_request(name, arguments):
    """Stable non-sensitive failure; never echo validation input or exception."""
    if type(name) is not str or name not in REQUEST_MODELS or type(arguments) is not dict:
        raise ValueError('invalid_adult_request')
    try:
        return REQUEST_MODELS[name].model_validate(arguments)
    except Exception:
        raise ValueError('invalid_adult_request') from None
