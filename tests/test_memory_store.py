"""Tests for the local SQLite memory store."""
import tempfile
import unittest
from pathlib import Path

from agent.memory_store import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "memory.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_remember_and_recall(self):
        saved = self.store.remember(
            "The CNC plasma project uses Mach3 and a 1200 by 1000 mm work area.",
            ["project:cnc", "engineering"],
        )
        results = self.store.recall("Mach3 plasma")
        self.assertEqual(saved["id"], results[0]["id"])
        self.assertIn("project:cnc", results[0]["tags"])

    def test_recall_ranks_more_matching_terms_first(self):
        self.store.remember("Python is used for automation.")
        self.store.remember("Python and SQLite support local memory automation.")
        results = self.store.recall("Python SQLite automation")
        self.assertIn("SQLite", results[0]["content"])

    def test_rejects_empty_memory_and_query(self):
        with self.assertRaises(ValueError):
            self.store.remember("   ")
        with self.assertRaises(ValueError):
            self.store.recall(" ")

    def test_recent_memories_returns_newest_first(self):
        first = self.store.remember("First note")
        second = self.store.remember("Second note")
        recent = self.store.recent()
        self.assertEqual([second["id"], first["id"]], [item["id"] for item in recent])


if __name__ == "__main__":
    unittest.main()
