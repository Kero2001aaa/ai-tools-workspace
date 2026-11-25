"""Leased image intake only. No route selection, adapter calls, or retry loop.

The durable task request stores one keyed fingerprint of a deterministic,
content-addressed canonical envelope, so exact content is bound before any
payload identifier exists. The stored operational envelope references the
actual task-scoped payload identifiers chosen by the artifact store.
"""
from __future__ import annotations

import hashlib
import hmac

from agent.adult.adult_image_input import (
    ImageInputError,
    decode_image_input,
    encode_image_input,
    encode_image_prompt,
)
from agent.adult.adult_image_validator import ImageValidationError, validate_image
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.models import ImageInputEnvelope, TaskLease, ValidatedImage
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher

_IMAGE_TASK_TYPES = frozenset({"image.analyze", "image.generate", "image.edit"})


class ImageIntakeError(RuntimeError):
    """Stable error without image bytes, prompt text, identifiers, or storage details."""


def _content_reference(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _validated_parts(
    task_type: str, image: bytes | None, prompt: str | None
) -> tuple[bytes | None, bytes | None, ValidatedImage | None]:
    """Validate caller content for one image task type; never returns partial content."""
    if not isinstance(task_type, str) or task_type not in _IMAGE_TASK_TYPES:
        raise ImageInputError("invalid_envelope")
    if image is not None and not isinstance(image, bytes):
        raise ImageInputError("invalid_envelope")
    if prompt is not None and not isinstance(prompt, str):
        raise ImageInputError("invalid_prompt")
    if task_type == "image.analyze" and (image is None or prompt is not None):
        raise ImageInputError("invalid_envelope")
    if task_type == "image.generate" and (prompt is None or image is not None):
        raise ImageInputError("invalid_envelope")
    if task_type == "image.edit" and (image is None or prompt is None):
        raise ImageInputError("invalid_envelope")
    validated = None
    if image is not None:
        try:
            validated = validate_image(image, output=False)
        except ImageValidationError:
            raise ImageInputError("invalid_envelope") from None
    prompt_bytes = encode_image_prompt(prompt) if prompt is not None else None
    return image, prompt_bytes, validated


def canonical_image_input(task_type: str, image: bytes | None, prompt: str | None) -> bytes:
    """Deterministic content-addressed envelope bytes for fingerprint binding only.

    These bytes are never stored; the operational envelope references the
    actual stored payload identifiers. Equal validated content always yields
    equal canonical bytes, so one keyed fingerprint binds exact content before
    any task or payload identifier exists.
    """
    image_bytes, prompt_bytes, _validated = _validated_parts(task_type, image, prompt)
    return encode_image_input(
        ImageInputEnvelope(
            task_type=task_type,
            image_payload_id=_content_reference(image_bytes) if image_bytes is not None else None,
            prompt_payload_id=_content_reference(prompt_bytes) if prompt_bytes is not None else None,
        )
    )


class AdultImageIntake:
    def __init__(self, tasks: AdultTaskStore, artifacts: AdultArtifactStore,
                 fingerprinter: PayloadCipher):
        if not isinstance(tasks, AdultTaskStore):
            raise TypeError("tasks must be an AdultTaskStore.")
        if not isinstance(artifacts, AdultArtifactStore):
            raise TypeError("artifacts must be an AdultArtifactStore.")
        if not isinstance(fingerprinter, PayloadCipher):
            raise TypeError("fingerprinter must be a PayloadCipher.")
        self.tasks, self.artifacts, self.fingerprinter = tasks, artifacts, fingerprinter

    def attach(self, task_id: str, *, image: bytes | None, prompt: str | None,
               lease: TaskLease, expected_state_version: int):
        """Validate, store, and attach the first image input set to a claimed task.

        All validation precedes storage. Stored bytes are read back through the
        task-owned input path before attachment. A failed task-store commit
        removes only the payloads created by this call; a payload the task now
        owns is never deleted. A cleanup error is surfaced, not hidden. The
        caller still owns lease renewal and any execution-step creation.
        """
        request = self.tasks.get_task_request(task_id)
        if request is None or request.payload_id is not None:
            raise ImageIntakeError("intake_unavailable")
        try:
            image_bytes, prompt_bytes, validated = _validated_parts(
                request.task_type, image, prompt)
            canonical = canonical_image_input(request.task_type, image, prompt)
            if not hmac.compare_digest(
                self.fingerprinter.fingerprint(canonical), request.input_fingerprint
            ):
                raise ValueError()
            retain = request.retention_policy == "encrypted"
            if retain and (self.artifacts.cipher is None
                           or self.artifacts.cipher.key_id != self.fingerprinter.key_id):
                raise ValueError()
        except Exception:
            raise ImageIntakeError("input_invalid") from None
        created: list[str] = []
        try:
            image_ref = None
            if image_bytes is not None:
                image_ref = self.artifacts.save_input(
                    task_id, image_bytes, validated.mime_type, retain_encrypted=retain)
                created.append(image_ref.payload_id)
                if not hmac.compare_digest(
                    self.artifacts.load_input(image_ref.payload_id, task_id), image_bytes
                ):
                    raise ImageIntakeError("input_invalid")
            prompt_ref = None
            if prompt_bytes is not None:
                prompt_ref = self.artifacts.save_input(
                    task_id, prompt_bytes, "text/plain", retain_encrypted=retain)
                created.append(prompt_ref.payload_id)
                if not hmac.compare_digest(
                    self.artifacts.load_input(prompt_ref.payload_id, task_id), prompt_bytes
                ):
                    raise ImageIntakeError("input_invalid")
            envelope_data = encode_image_input(
                ImageInputEnvelope(
                    task_type=request.task_type,
                    image_payload_id=image_ref.payload_id if image_ref is not None else None,
                    prompt_payload_id=prompt_ref.payload_id if prompt_ref is not None else None,
                )
            )
            # The stored envelope must stay decodable under the strict contract.
            decode_image_input(envelope_data, request.task_type)
            envelope_ref = self.artifacts.save_input(
                task_id, envelope_data, "application/json", retain_encrypted=retain)
            created.append(envelope_ref.payload_id)
            # Read through the task-owned input path before attachment.
            if not hmac.compare_digest(
                self.artifacts.load_input(envelope_ref.payload_id, task_id), envelope_data
            ):
                raise ImageIntakeError("input_invalid")
            return self.tasks.attach_input(
                task_id, envelope_ref.payload_id, lease, expected_state_version)
        except Exception:
            # A commit can succeed before its response/readback fails. Never
            # delete a reference that the task now owns, or one whose commit
            # state cannot be established. Such orphans remain retention-bound.
            try:
                committed = self.tasks.get_task_request(task_id)
            except Exception:
                raise ImageIntakeError("intake_state_unknown") from None
            if committed is not None and committed.payload_id is not None \
                    and committed.payload_id in created:
                raise ImageIntakeError("intake_committed_readback_failed") from None
            cleanup_failed = False
            for payload_id in created:
                try:
                    self.artifacts.discard_input(payload_id, task_id)
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                raise ImageIntakeError("input_cleanup_failed") from None
            raise ImageIntakeError("intake_unavailable") from None
