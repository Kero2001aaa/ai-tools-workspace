"""Leased text intake only. No route selection, model calls, or retry loop."""
from __future__ import annotations

import hmac
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.adult_text_input import decode_text_input
from agent.adult.models import TaskLease
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher


class TextIntakeError(RuntimeError):
    """Stable error without source text, identifiers, or storage details."""


class AdultTextIntake:
    def __init__(self, tasks: AdultTaskStore, artifacts: AdultArtifactStore,
                 fingerprinter: PayloadCipher):
        if not isinstance(tasks, AdultTaskStore):
            raise TypeError("tasks must be an AdultTaskStore.")
        if not isinstance(artifacts, AdultArtifactStore):
            raise TypeError("artifacts must be an AdultArtifactStore.")
        if not isinstance(fingerprinter, PayloadCipher):
            raise TypeError("fingerprinter must be a PayloadCipher.")
        self.tasks, self.artifacts, self.fingerprinter = tasks, artifacts, fingerprinter

    def attach(self, task_id: str, data: bytes, lease: TaskLease, expected_state_version: int):
        """Validate, store, and attach the first text input to a claimed task.

        All validation precedes storage. A failed task-store commit removes only
        the payload created by this call. A cleanup error is surfaced, not hidden.
        The caller still owns lease renewal and any execution-step creation.
        """
        request = self.tasks.get_task_request(task_id)
        if request is None or request.payload_id is not None:
            raise TextIntakeError("intake_unavailable")
        try:
            envelope = decode_text_input(data, request.task_type)
            fingerprint = self.fingerprinter.fingerprint(data)
            if not hmac.compare_digest(fingerprint, request.input_fingerprint):
                raise ValueError()
            if request.instruction_fingerprint is not None:
                instruction = (envelope.instruction or "").encode("utf-8")
                if not hmac.compare_digest(
                    self.fingerprinter.fingerprint(instruction), request.instruction_fingerprint,
                ):
                    raise ValueError()
            retain = request.retention_policy == "encrypted"
            if retain and (self.artifacts.cipher is None
                           or self.artifacts.cipher.key_id != self.fingerprinter.key_id):
                raise ValueError()
        except Exception:
            raise TextIntakeError("input_invalid") from None
        reference = self.artifacts.save_input(task_id, data, "application/json", retain_encrypted=retain)
        try:
            # Read through the task-owned input path before attachment.
            if not hmac.compare_digest(self.artifacts.load_input(reference.payload_id, task_id), data):
                raise TextIntakeError("input_invalid")
            return self.tasks.attach_input(task_id, reference.payload_id, lease, expected_state_version)
        except Exception:
            # A commit can succeed before its response/readback fails. Never
            # delete a reference that the task now owns, or one whose commit
            # state cannot be established. Such orphans remain retention-bound.
            try:
                committed = self.tasks.get_task_request(task_id)
            except Exception:
                raise TextIntakeError("intake_state_unknown") from None
            if committed is not None and committed.payload_id == reference.payload_id:
                raise TextIntakeError("intake_committed_readback_failed") from None
            try:
                self.artifacts.discard_input(reference.payload_id, task_id)
            except Exception:
                raise TextIntakeError("input_cleanup_failed") from None
            raise TextIntakeError("intake_unavailable") from None
