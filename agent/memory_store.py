"""Small, local SQLite memory store with keyword retrieval.

No embeddings, hosted services, or network calls are used.
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB = Path(".data") / "memory.sqlite3"


class MemoryStore:
    def __init__(self, db_path: str | Path | None = None):
        configured = db_path or os.getenv("WORKSPACE_MEMORY_DB") or str(DEFAULT_DB)
        self.db_path = Path(configured).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_created_at "
                "ON memories(created_at DESC)"
            )

    def remember(self, content: str, tags: list[str] | None = None) -> dict:
        cleaned = content.strip()
        if not cleaned:
            raise ValueError("Memory content must not be empty.")
        if len(cleaned) > 10000:
            raise ValueError("Memory content must be 10,000 characters or fewer.")
        clean_tags = sorted({tag.strip() for tag in (tags or []) if tag.strip()})
        if len(clean_tags) > 20 or any(len(tag) > 64 for tag in clean_tags):
            raise ValueError("Use at most 20 tags, each 64 characters or fewer.")
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO memories(content, tags, created_at) VALUES (?, ?, ?)",
                (cleaned, json.dumps(clean_tags), created_at),
            )
            memory_id = cursor.lastrowid
        return {"id": memory_id, "created_at": created_at, "tags": clean_tags}

    def recall(self, query: str, limit: int = 5) -> list[dict]:
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("Recall query must not be empty.")
        if not 1 <= limit <= 20:
            raise ValueError("Recall limit must be between 1 and 20.")
        terms = list(dict.fromkeys(re.findall(r"[\w-]{2,}", cleaned.casefold())))
        if not terms:
            return []
        clauses = " OR ".join("LOWER(content) LIKE ? ESCAPE '\\'" for _ in terms)
        parameters = [f"%{term.replace('%', r'\%').replace('_', r'\_')}%" for term in terms]
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id, content, tags, created_at FROM memories WHERE {clauses}",
                parameters,
            ).fetchall()
        ranked = []
        for row in rows:
            lowered = row["content"].casefold()
            score = sum(term in lowered for term in terms)
            ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], item[1]["created_at"]), reverse=False)
        return [
            {
                "id": row["id"],
                "content": row["content"],
                "tags": json.loads(row["tags"]),
                "created_at": row["created_at"],
                "matched_terms": score,
            }
            for score, row in ranked[:limit]
        ]

    def recent(self, limit: int = 10) -> list[dict]:
        if not 1 <= limit <= 20:
            raise ValueError("Recent limit must be between 1 and 20.")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, content, tags, created_at FROM memories "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "content": row["content"],
                "tags": json.loads(row["tags"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
