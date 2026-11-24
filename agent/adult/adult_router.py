"""Deterministic Adult route selection; this module never executes providers."""
from __future__ import annotations

import re
from typing import Callable

from agent.adult.capability_registry import ModelCapabilityRegistry
from agent.adult.models import AdultModelValidationError, CapabilityQuery, CostPolicy, RouteCandidate


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class AdultModelRouter:
    def __init__(
        self,
        registry: ModelCapabilityRegistry,
        *,
        approach_key_builder: Callable[[str, RouteCandidate, str, str], tuple[str, str]],
        local_adapter_available: Callable[[str], bool] | None = None,
    ) -> None:
        if not isinstance(registry, ModelCapabilityRegistry):
            raise TypeError("registry must be a ModelCapabilityRegistry.")
        if not callable(approach_key_builder):
            raise TypeError("approach_key_builder must be callable.")
        if local_adapter_available is not None and not callable(local_adapter_available):
            raise TypeError("local_adapter_available must be callable.")
        self._registry = registry
        self._approach_key_builder = approach_key_builder
        self._local_adapter_available = local_adapter_available or (lambda _adapter_id: False)

    def choose_routes(
        self,
        query: CapabilityQuery,
        cost_policy: CostPolicy,
        excluded_approach_keys: set[str],
    ) -> list[RouteCandidate]:
        if not isinstance(query, CapabilityQuery) or not isinstance(cost_policy, CostPolicy):
            raise AdultModelValidationError("Validated CapabilityQuery and CostPolicy are required.")
        if not isinstance(excluded_approach_keys, set) or any(
            not isinstance(value, str) or not _HASH_RE.fullmatch(value)
            for value in excluded_approach_keys
        ):
            raise AdultModelValidationError("excluded_approach_keys must be SHA-256 hashes.")
        routes = self._registry.list_eligible(query, cost_policy)
        selected: list[RouteCandidate] = []
        for candidate in routes:
            if candidate.cost_class == "local" and self._local_adapter_available(candidate.adapter_id) is not True:
                continue
            approach_key, _route_family_key = self._approach_key_builder(
                query.task_type, candidate, query.input_fingerprint, query.config_fingerprint,
            )
            if not isinstance(approach_key, str) or not _HASH_RE.fullmatch(approach_key):
                raise AdultModelValidationError("Approach-key builder returned an invalid hash.")
            if approach_key not in excluded_approach_keys:
                selected.append(candidate)
        selected.sort(key=lambda r: (
            0 if r.cost_class == "local" else 1,
            r.adapter_id, r.provider_id or "", r.model_id or "", r.capability_version,
        ))
        return selected
