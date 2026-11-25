"""Strict internal response validation, not registration, auth or dispatch."""
from __future__ import annotations
import base64
import binascii
from datetime import datetime
from typing import Annotated, Literal
from pydantic import Field, field_validator, model_validator
from agent.adult.adult_request_contracts import StrictRequest, TaskID, ArtifactID, Status

Opaque = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')]
ModelID = Annotated[str, Field(pattern=r'^(?:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}|openrouter/free)$')]
Code = Annotated[str, Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,95}$')]
Count = Annotated[int, Field(ge=0)]


class TaskSnapshot(StrictRequest):
    task_id: TaskID
    task_type: Literal['text.analyze','text.generate','text.rewrite','image.analyze','image.generate','image.edit']
    status: Status
    created_at: str
    updated_at: str
    deadline_at: str
    attempt_count: int = Field(ge=0, le=20)
    provider_calls: int = Field(ge=0, le=20)
    current_step_id: Opaque | None
    selected_adapter: Opaque | None
    selected_provider: Opaque | None
    selected_model: ModelID | None
    selected_tool: Opaque | None
    latest_result_artifact_id: ArtifactID | None
    last_error_code: Code | None
    user_action_required: bool

    @field_validator('created_at','updated_at','deadline_at')
    @classmethod
    def timestamp(cls,value):
        if len(value)>40:
            raise ValueError('invalid_timestamp')
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('invalid_timestamp')
        return value


class TaskPage(StrictRequest):
    tasks: list[TaskSnapshot] = Field(max_length=20)
    next_cursor: str | None = Field(max_length=1024)
    has_more: bool

    @model_validator(mode='after')
    def paging(self):
        if self.has_more != (self.next_cursor is not None):
            raise ValueError('inconsistent_paging')
        if self.next_cursor is not None and (not self.next_cursor or not self.tasks):
            raise ValueError('inconsistent_paging')
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError('duplicate_task')
        return self


class AttemptSummary(StrictRequest):
    attempt_id: ArtifactID
    attempt_no: int = Field(ge=1, le=20)
    adapter_id: Opaque
    adapter_version: Opaque
    provider_id: Opaque | None
    model_id: ModelID | None
    status: Literal['running','succeeded','failed','rejected','timed_out','unknown_outcome']
    elapsed_ms: Count | None
    error_code: Code | None
    cost_microusd: Count | None


class AttemptPage(StrictRequest):
    attempts: list[AttemptSummary] = Field(max_length=20)

    @model_validator(mode='after')
    def ordered(self):
        numbers=[attempt.attempt_no for attempt in self.attempts]
        ids=[attempt.attempt_id for attempt in self.attempts]
        if numbers != sorted(set(numbers)) or len(set(ids)) != len(ids):
            raise ValueError('invalid_attempt_order')
        return self


class ArtifactChunk(StrictRequest):
    artifact_id: ArtifactID
    data_base64: str = Field(max_length=349528, repr=False)
    decoded_bytes: int = Field(ge=0, le=262144)
    total_bytes: int = Field(ge=0, le=100*1024*1024)
    offset: Count
    next_offset: Count
    mime_type: Literal['text/plain','image/png','image/jpeg','image/webp']
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    complete: bool

    @model_validator(mode='after')
    def chunk(self):
        try:
            data=base64.b64decode(self.data_base64,validate=True)
        except (ValueError,binascii.Error):
            raise ValueError('invalid_chunk') from None
        if (len(data)!=self.decoded_bytes or self.next_offset!=self.offset+len(data)
                or self.next_offset>self.total_bytes or self.offset>self.total_bytes
                or self.complete != (self.next_offset==self.total_bytes)
                or (not self.complete and not data)):
            raise ValueError('invalid_chunk')
        # Only a full-content chunk can independently verify the full hash.
        if self.offset==0 and self.complete:
            import hashlib
            if hashlib.sha256(data).hexdigest()!=self.sha256:
                raise ValueError('invalid_digest')
        return self


class ExecutionOutcome(StrictRequest):
    task: TaskSnapshot
    error_code: Code | None
    input_cleanup: 'Cleanup'


class Cleanup(StrictRequest):
    status: Literal['not_applicable','input_removed','cleanup_failed']
    error_code: Literal['input_cleanup_unavailable'] | None = None

    @model_validator(mode='after')
    def failure(self):
        if (self.status=='cleanup_failed') != (self.error_code is not None):
            raise ValueError('inconsistent_cleanup')
        return self


ExecutionOutcome.model_rebuild()
RESPONSE_MODELS={'task':TaskSnapshot,'tasks':TaskPage,'attempts':AttemptPage,
                 'artifact':ArtifactChunk,'execution':ExecutionOutcome}


class ErrorResponse(StrictRequest):
    status: Literal['error']
    error_code: Literal['invalid_adult_request','invalid_adult_response',
                        'task_unavailable','artifact_unavailable','operation_unavailable']


def response_schemas():
    return {name:model.model_json_schema() for name,model in {**RESPONSE_MODELS,'error':ErrorResponse}.items()}


def validate_response(kind,value):
    """Reject unknown fields. Never echo raw exceptions or drop unsafe fields."""
    if type(kind) is not str or kind not in RESPONSE_MODELS or type(value) is not dict:
        raise ValueError('invalid_adult_response')
    try:
        return RESPONSE_MODELS[kind].model_validate(value).model_dump(mode='json')
    except Exception:
        raise ValueError('invalid_adult_response') from None
