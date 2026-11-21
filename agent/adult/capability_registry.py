"""Verified capability registry for Adult Agent; no provider/network calls."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable

from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.models import (
    AdultModelValidationError, CapabilityEvidence, CapabilityQuery, CostPolicy,
    ModelCapability, ModelCapabilitySpec, RouteCandidate,
)


class CapabilityRegistryError(RuntimeError):
    error_code = "capability_registry_error"


class CapabilityNotFound(CapabilityRegistryError):
    error_code = "capability_not_found"


class CapabilityConflict(CapabilityRegistryError):
    error_code = "capability_conflict"


class CapabilityStateConflict(CapabilityRegistryError):
    error_code = "capability_state_conflict"


class CapabilityEvidenceError(CapabilityRegistryError):
    error_code = "capability_evidence_invalid"


class CapabilityNotEligible(CapabilityRegistryError):
    error_code = "capability_not_eligible"


_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise CapabilityRegistryError("Registry clock must return a timezone-aware datetime.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _canonical_hash(value: dict) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _capability_version(spec: ModelCapabilitySpec, evidence: CapabilityEvidence | None) -> str:
    return _canonical_hash({
        "contract": spec.to_dict(),
        "evidence": evidence.to_dict() if evidence is not None else None,
    })


def _validate_reason(reason_code: str) -> str:
    if not isinstance(reason_code, str) or not _REASON_RE.fullmatch(reason_code):
        raise AdultModelValidationError("reason_code must be a normalized bounded code.")
    return reason_code


def _state(row: sqlite3.Row) -> str:
    return f"{row['verification_status']}:{'enabled' if row['enabled'] else 'disabled'}"


def _spec_from_row(row: sqlite3.Row) -> ModelCapabilitySpec:
    try:
        data = json.loads(row["capability_json"])
        if not isinstance(data, dict):
            raise ValueError("capability_json must be an object")
        for name in ("input_modalities", "output_modalities", "accepted_mime_types", "output_mime_types"):
            data[name] = tuple(data[name])
        spec = ModelCapabilitySpec(**data)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise CapabilityRegistryError("Stored capability contract failed validation.") from exc
    if (
        row["adapter_id"] != spec.adapter_id
        or row["adapter_version"] != spec.adapter_version
        or row["provider_id"] != spec.provider_id
        or row["model_id"] != spec.model_id
        or row["model_version"] != spec.model_version
        or row["task_type"] != spec.task_type
        or row["cost_class"] != spec.cost_class
    ):
        raise CapabilityRegistryError("Stored capability identity does not match its contract.")
    return spec


def _evidence_from_row(
    row: sqlite3.Row, spec: ModelCapabilitySpec,
) -> CapabilityEvidence | None:
    if row["verification_status"] != "verified":
        return None
    try:
        has_cost_evidence = row["cost_verified_at"] is not None
        return CapabilityEvidence(
            adapter_id=spec.adapter_id, adapter_version=spec.adapter_version,
            provider_id=spec.provider_id, model_id=spec.model_id,
            model_version=spec.model_version, task_type=spec.task_type,
            input_modalities=spec.input_modalities, output_modalities=spec.output_modalities,
            capability_source=row["capability_evidence_source"],
            capability_reference=row["capability_evidence_ref"],
            verified_at=row["capability_verified_at"],
            expires_at=row["capability_expires_at"],
            cost_source=row["cost_evidence_source"],
            cost_reference=row["cost_evidence_ref"],
            cost_verified_at=row["cost_verified_at"],
            cost_expires_at=row["cost_expires_at"],
            cost_is_zero=True if has_cost_evidence else None,
        )
    except (TypeError, ValueError) as exc:
        raise CapabilityRegistryError("Stored capability evidence failed validation.") from exc


def _to_capability(row: sqlite3.Row) -> ModelCapability:
    spec = _spec_from_row(row)
    evidence = _evidence_from_row(row, spec)
    if row["verification_status"] in {"unverified", "verified"}:
        expected_version = _capability_version(spec, evidence)
        if row["capability_version"] != expected_version:
            raise CapabilityRegistryError("Stored capability version does not match its contract/evidence.")
    return ModelCapability(
        capability_id=row["capability_id"],
        spec=spec,
        enabled=bool(row["enabled"]),
        verification_status=row["verification_status"],
        capability_version=row["capability_version"],
        evidence=evidence,
        state_version=row["state_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ModelCapabilityRegistry:
    """Owns capability lifecycle; the TaskStore owns DB connections and transactions."""

    def __init__(
        self,
        store: AdultTaskStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(store, AdultTaskStore):
            raise TypeError("store must be an AdultTaskStore.")
        if not callable(clock):
            raise TypeError("clock must be callable.")
        self._store = store
        self._clock = clock

    def _now(self) -> str:
        return _utc_text(self._clock())

    @staticmethod
    def _load_row(db: sqlite3.Connection, capability_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM adult_model_capabilities WHERE capability_id=?",
            (capability_id,),
        ).fetchone()
        if row is None:
            raise CapabilityNotFound("Capability does not exist.")
        return row

    @staticmethod
    def _expect_version(row: sqlite3.Row, expected_state_version: int) -> None:
        if (
            not isinstance(expected_state_version, int)
            or isinstance(expected_state_version, bool)
            or expected_state_version < 1
            or row["state_version"] != expected_state_version
        ):
            raise CapabilityStateConflict("Capability state_version is stale.")

    @staticmethod
    def _event(
        db: sqlite3.Connection, *, capability_id: str, event_type: str,
        from_state: str | None, to_state: str, reason_code: str | None,
        previous_version: str | None, capability_version: str, created_at: str,
    ) -> None:
        db.execute(
            """INSERT INTO adult_capability_events(
                event_id,capability_id,event_type,from_state,to_state,reason_code,
                previous_version,capability_version,metadata_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex, capability_id, event_type, from_state, to_state,
                reason_code, previous_version, capability_version, "{}", created_at,
            ),
        )

    @staticmethod
    def _require_current_evidence(
        spec: ModelCapabilitySpec, evidence: CapabilityEvidence, now: str
    ) -> None:
        exact_identity = (
            evidence.adapter_id == spec.adapter_id
            and evidence.adapter_version == spec.adapter_version
            and evidence.provider_id == spec.provider_id
            and evidence.model_id == spec.model_id
            and evidence.model_version == spec.model_version
            and evidence.task_type == spec.task_type
            and evidence.input_modalities == spec.input_modalities
            and evidence.output_modalities == spec.output_modalities
        )
        if not exact_identity:
            raise CapabilityEvidenceError("Evidence subject does not match the exact capability contract.")

        instant = _parse_utc(now)
        if _parse_utc(evidence.verified_at) > instant or _parse_utc(evidence.expires_at) <= instant:
            raise CapabilityEvidenceError("Capability evidence is not currently valid.")
        has_cost_evidence = any(value is not None for value in (
            evidence.cost_source, evidence.cost_reference,
            evidence.cost_verified_at, evidence.cost_expires_at,
        ))
        if spec.cost_class == "local":
            if evidence.capability_source != "local_adapter_smoke_test":
                raise CapabilityEvidenceError("Local routes require local adapter verification evidence.")
            if has_cost_evidence or evidence.cost_is_zero is not None:
                raise CapabilityEvidenceError("Local routes do not accept provider cost evidence.")
        else:
            if evidence.capability_source == "local_adapter_smoke_test":
                raise CapabilityEvidenceError("Provider routes require provider-specific capability evidence.")
            if spec.cost_class != "free_verified" and has_cost_evidence:
                raise CapabilityEvidenceError("The MVP accepts cost evidence only for free_verified routes.")

        if spec.cost_class == "free_verified":
            if evidence.cost_source not in {"official_provider_pricing", "official_free_tier_terms"}:
                raise CapabilityEvidenceError("free_verified routes require official zero-cost evidence.")
            if evidence.cost_is_zero is not True:
                raise CapabilityEvidenceError("free_verified routes require explicit proof of zero cost.")
            if (
                evidence.cost_verified_at is None or evidence.cost_expires_at is None
                or _parse_utc(evidence.cost_verified_at) > instant
                or _parse_utc(evidence.cost_expires_at) <= instant
            ):
                raise CapabilityEvidenceError("Zero-cost evidence is missing, future-dated or expired.")

    def register(self, capability: ModelCapabilitySpec) -> ModelCapability:
        if not isinstance(capability, ModelCapabilitySpec):
            raise AdultModelValidationError("capability must be a ModelCapabilitySpec.")
        now = self._now()
        capability_id = uuid.uuid4().hex
        version = _capability_version(capability, None)
        encoded = json.dumps(
            capability.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        try:
            with self._store._transaction() as db:
                db.execute(
                    """INSERT INTO adult_model_capabilities(
                        capability_id,adapter_id,adapter_version,provider_id,model_id,model_version,
                        task_type,capability_json,cost_class,enabled,verification_status,
                        capability_version,state_version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,0,'unverified',?,1,?,?)""",
                    (
                        capability_id, capability.adapter_id, capability.adapter_version,
                        capability.provider_id, capability.model_id, capability.model_version,
                        capability.task_type, encoded, capability.cost_class, version, now, now,
                    ),
                )
                self._event(
                    db, capability_id=capability_id, event_type="registered",
                    from_state=None, to_state="unverified:disabled", reason_code=None,
                    previous_version=None, capability_version=version, created_at=now,
                )
        except sqlite3.IntegrityError as exc:
            raise CapabilityConflict("A capability with this adapter/provider/model/task identity already exists.") from exc
        return self.get(capability_id)

    def get(self, capability_id: str) -> ModelCapability:
        if not isinstance(capability_id, str) or not capability_id or len(capability_id) > 128:
            raise AdultModelValidationError("capability_id must be a bounded identifier.")
        db = self._store._connect()
        try:
            return _to_capability(self._load_row(db, capability_id))
        finally:
            db.close()

    def verify(
        self, capability_id: str, evidence: CapabilityEvidence,
        expected_state_version: int,
    ) -> ModelCapability:
        if not isinstance(evidence, CapabilityEvidence):
            raise AdultModelValidationError("evidence must be a CapabilityEvidence.")
        now = self._now()
        with self._store._transaction() as db:
            row = self._load_row(db, capability_id)
            self._expect_version(row, expected_state_version)
            if row["verification_status"] == "revoked":
                raise CapabilityNotEligible("Revoked capabilities cannot be verified.")
            spec = _spec_from_row(row)
            self._require_current_evidence(spec, evidence, now)
            version = _capability_version(spec, evidence)
            before = _state(row)
            cursor = db.execute(
                """UPDATE adult_model_capabilities SET
                    enabled=0,verification_status='verified',capability_version=?,
                    capability_verified_at=?,capability_expires_at=?,
                    capability_evidence_source=?,capability_evidence_ref=?,
                    cost_verified_at=?,cost_expires_at=?,cost_evidence_source=?,cost_evidence_ref=?,
                    state_version=state_version+1,updated_at=?
                   WHERE capability_id=? AND state_version=?""",
                (
                    version, evidence.verified_at, evidence.expires_at,
                    evidence.capability_source, evidence.capability_reference,
                    evidence.cost_verified_at, evidence.cost_expires_at,
                    evidence.cost_source, evidence.cost_reference, now,
                    capability_id, expected_state_version,
                ),
            )
            if cursor.rowcount != 1:
                raise CapabilityStateConflict("Capability changed during verification.")
            self._event(
                db, capability_id=capability_id, event_type="verified",
                from_state=before, to_state="verified:disabled", reason_code=None,
                previous_version=row["capability_version"], capability_version=version,
                created_at=now,
            )
        return self.get(capability_id)

    def revise(
        self, capability_id: str, replacement: ModelCapabilitySpec,
        expected_state_version: int,
    ) -> ModelCapability:
        if not isinstance(replacement, ModelCapabilitySpec):
            raise AdultModelValidationError("replacement must be a ModelCapabilitySpec.")
        now = self._now()
        encoded = json.dumps(
            replacement.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        version = _capability_version(replacement, None)
        try:
            with self._store._transaction() as db:
                row = self._load_row(db, capability_id)
                self._expect_version(row, expected_state_version)
                if row["verification_status"] == "revoked":
                    raise CapabilityNotEligible("Revoked capabilities cannot be revised.")
                before = _state(row)
                cursor = db.execute(
                    """UPDATE adult_model_capabilities SET
                        adapter_id=?,adapter_version=?,provider_id=?,model_id=?,model_version=?,
                        task_type=?,capability_json=?,cost_class=?,enabled=0,
                        verification_status='unverified',capability_version=?,
                        capability_verified_at=NULL,capability_expires_at=NULL,
                        capability_evidence_source=NULL,capability_evidence_ref=NULL,
                        cost_verified_at=NULL,cost_expires_at=NULL,
                        cost_evidence_source=NULL,cost_evidence_ref=NULL,
                        state_version=state_version+1,updated_at=?
                       WHERE capability_id=? AND state_version=?""",
                    (
                        replacement.adapter_id, replacement.adapter_version,
                        replacement.provider_id, replacement.model_id, replacement.model_version,
                        replacement.task_type, encoded, replacement.cost_class, version, now,
                        capability_id, expected_state_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise CapabilityStateConflict("Capability changed during revision.")
                self._event(
                    db, capability_id=capability_id, event_type="revised",
                    from_state=before, to_state="unverified:disabled", reason_code=None,
                    previous_version=row["capability_version"], capability_version=version,
                    created_at=now,
                )
        except sqlite3.IntegrityError as exc:
            raise CapabilityConflict("Revised capability conflicts with an existing identity.") from exc
        return self.get(capability_id)

    def enable(self, capability_id: str, expected_state_version: int) -> ModelCapability:
        now = self._now()
        with self._store._transaction() as db:
            row = self._load_row(db, capability_id)
            self._expect_version(row, expected_state_version)
            if row["verification_status"] != "verified":
                raise CapabilityNotEligible("Only verified capabilities may be enabled.")
            if row["cost_class"] not in {"local", "free_verified"}:
                raise CapabilityNotEligible("Paid and unknown-cost routes are disabled in this MVP.")
            if row["capability_expires_at"] is None or row["capability_expires_at"] <= now:
                raise CapabilityEvidenceError("Capability evidence has expired.")
            if row["cost_class"] == "free_verified" and (
                row["cost_expires_at"] is None or row["cost_expires_at"] <= now
            ):
                raise CapabilityEvidenceError("Zero-cost evidence has expired.")
            before = _state(row)
            cursor = db.execute(
                """UPDATE adult_model_capabilities SET enabled=1,
                   state_version=state_version+1,updated_at=?
                   WHERE capability_id=? AND state_version=?""",
                (now, capability_id, expected_state_version),
            )
            if cursor.rowcount != 1:
                raise CapabilityStateConflict("Capability changed during enablement.")
            self._event(
                db, capability_id=capability_id, event_type="enabled",
                from_state=before, to_state="verified:enabled", reason_code=None,
                previous_version=row["capability_version"],
                capability_version=row["capability_version"], created_at=now,
            )
        return self.get(capability_id)

    def disable(
        self, capability_id: str, reason_code: str, expected_state_version: int
    ) -> ModelCapability:
        reason = _validate_reason(reason_code)
        now = self._now()
        with self._store._transaction() as db:
            row = self._load_row(db, capability_id)
            self._expect_version(row, expected_state_version)
            if row["verification_status"] == "revoked":
                raise CapabilityNotEligible("Revoked capabilities are terminal and cannot be changed.")
            before = _state(row)
            cursor = db.execute(
                """UPDATE adult_model_capabilities SET enabled=0,
                   state_version=state_version+1,updated_at=?
                   WHERE capability_id=? AND state_version=?""",
                (now, capability_id, expected_state_version),
            )
            if cursor.rowcount != 1:
                raise CapabilityStateConflict("Capability changed during disablement.")
            self._event(
                db, capability_id=capability_id, event_type="disabled",
                from_state=before, to_state=f"{row['verification_status']}:disabled",
                reason_code=reason, previous_version=row["capability_version"],
                capability_version=row["capability_version"], created_at=now,
            )
        return self.get(capability_id)

    def revoke(
        self, capability_id: str, reason_code: str, expected_state_version: int
    ) -> ModelCapability:
        reason = _validate_reason(reason_code)
        now = self._now()
        with self._store._transaction() as db:
            row = self._load_row(db, capability_id)
            self._expect_version(row, expected_state_version)
            if row["verification_status"] == "revoked":
                raise CapabilityNotEligible("Capability is already revoked.")
            before = _state(row)
            cursor = db.execute(
                """UPDATE adult_model_capabilities SET enabled=0,verification_status='revoked',
                   capability_verified_at=NULL,capability_expires_at=NULL,
                   capability_evidence_source=NULL,capability_evidence_ref=NULL,
                   cost_verified_at=NULL,cost_expires_at=NULL,
                   cost_evidence_source=NULL,cost_evidence_ref=NULL,
                   state_version=state_version+1,updated_at=?
                   WHERE capability_id=? AND state_version=?""",
                (now, capability_id, expected_state_version),
            )
            if cursor.rowcount != 1:
                raise CapabilityStateConflict("Capability changed during revocation.")
            self._event(
                db, capability_id=capability_id, event_type="revoked",
                from_state=before, to_state="revoked:disabled", reason_code=reason,
                previous_version=row["capability_version"],
                capability_version=row["capability_version"], created_at=now,
            )
        return self.get(capability_id)

    def list_eligible(
        self, query: CapabilityQuery, cost_policy: CostPolicy,
    ) -> list[RouteCandidate]:
        if not isinstance(query, CapabilityQuery) or not isinstance(cost_policy, CostPolicy):
            raise AdultModelValidationError("Validated CapabilityQuery and CostPolicy are required.")
        now = self._now()
        instant = _parse_utc(now)
        db = self._store._connect()
        try:
            rows = db.execute(
                """SELECT * FROM adult_model_capabilities
                   WHERE enabled=1 AND verification_status='verified'
                   ORDER BY adapter_id,ifnull(provider_id,''),ifnull(model_id,''),capability_id"""
            ).fetchall()
        finally:
            db.close()

        eligible: list[RouteCandidate] = []
        for row in rows:
            capability = _to_capability(row)
            if not capability.enabled or capability.verification_status != "verified":
                continue
            if capability.evidence is None:
                continue
            spec = capability.spec
            if spec.task_type != query.task_type:
                continue
            if not set(query.input_modalities).issubset(set(spec.input_modalities)):
                continue
            if query.output_modality not in spec.output_modalities:
                continue
            if (
                row["capability_verified_at"] is None
                or _parse_utc(row["capability_verified_at"]) > instant
                or row["capability_expires_at"] is None
                or _parse_utc(row["capability_expires_at"]) <= instant
            ):
                continue
            if spec.cost_class == "local":
                if not cost_policy.allow_local:
                    continue
            elif spec.cost_class == "free_verified":
                if (
                    capability.evidence.cost_is_zero is not True
                    or cost_policy.max_provider_calls <= 0
                    or cost_policy.max_cost_microusd != 0
                ):
                    continue
                if (
                    row["cost_verified_at"] is None
                    or _parse_utc(row["cost_verified_at"]) > instant
                    or row["cost_expires_at"] is None
                    or _parse_utc(row["cost_expires_at"]) <= instant
                ):
                    continue
            else:
                continue
            if "image" in query.input_modalities:
                if query.mime_type not in spec.accepted_mime_types:
                    continue
                if spec.max_input_bytes is None or query.input_bytes > spec.max_input_bytes:
                    continue
                if spec.max_input_width is None or query.width > spec.max_input_width:
                    continue
                if spec.max_input_height is None or query.height > spec.max_input_height:
                    continue
            eligible.append(RouteCandidate(
                adapter_id=spec.adapter_id, adapter_version=spec.adapter_version,
                provider_id=spec.provider_id, model_id=spec.model_id,
                model_version=spec.model_version, tool_id=None, tool_version=None,
                task_type=spec.task_type, cost_class=spec.cost_class,
                capability_version=row["capability_version"],
                verified_at=row["capability_verified_at"],
            ))
        eligible.sort(key=lambda r: (
            0 if r.cost_class == "local" else 1,
            r.adapter_id, r.provider_id or "", r.model_id or "", r.capability_version,
        ))
        return eligible
