"""Adult-only memory facade composed from the fixed Adult storage scope."""
from __future__ import annotations

from typing import Any

from agent.memory_store import MemoryRecord, MemoryRef, ScopedMemoryStore


class AdultMemory:
    """Expose private Adult memory and explicitly shareable Workspace lessons."""

    def __init__(self, store: ScopedMemoryStore):
        if not isinstance(store, ScopedMemoryStore) or store._scope_kind != "adult":
            raise TypeError("AdultMemory requires MemoryStore.adult_scope().")
        self.__store = store

    def remember_private(
        self,
        content: str,
        memory_type: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRef:
        return self.__store.remember(
            content=content,
            memory_type=memory_type,
            task_id=task_id,
            metadata=metadata,
        )

    def get_private(self, memory_id: int) -> MemoryRecord | None:
        return self.__store.get_owned(memory_id)

    def recall_private(self, query: str, limit: int = 5) -> list[MemoryRecord]:
        return self.__store.recall_owned(query, limit)

    def recent_private(self, limit: int = 10) -> list[MemoryRecord]:
        return self.__store.recent_owned(limit)

    def recall_shareable_workspace(self, query: str, limit: int = 5) -> list[MemoryRecord]:
        return self.__store.recall_shareable_workspace(query, limit)

    def recent_shareable_workspace(self, limit: int = 10) -> list[MemoryRecord]:
        return self.__store.recent_shareable_workspace(limit)
