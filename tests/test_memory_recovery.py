"""Whole memory restore preserves existing fixed scope and validates stored rows."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from agent.memory_recovery import copy_memory_database
from agent.memory_store import MemoryStore, MemoryAuthorizationError, MemoryDataIntegrityError
from agent.plan_store import PlanStore


class MemoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'memory.sqlite3'
        self.backup = self.root / 'backup.sqlite3'
        self.restored = self.root / 'restored.sqlite3'
        self.store = MemoryStore(self.source)
        self.public = self.store.remember('general-private-note')
        self.private = self.store.adult_scope().remember('adult-private-marker', memory_type='note')

    def test_backup_restore_preserves_scopes_ids_audit_and_no_content_in_receipt(self):
        with sqlite3.connect(self.source) as db:
            before = {table: db.execute('SELECT * FROM ' + table).fetchall() for table in ('memories', 'memory_namespace_acl', 'memory_access_audit', 'memory_schema_migrations')}
        receipt = copy_memory_database(self.source, self.backup)
        self.assertNotIn('private', json.dumps(receipt))
        copy_memory_database(self.backup, self.restored)
        with sqlite3.connect(self.restored) as db:
            for table, rows in before.items():
                self.assertEqual(rows, db.execute('SELECT * FROM ' + table).fetchall())
        recovered = MemoryStore(self.restored)
        self.assertEqual([], recovered.recall('adult-private-marker'))
        self.assertEqual(['general-private-note'], [row['content'] for row in recovered.recent()])
        self.assertEqual(['adult-private-marker'], [row.content for row in recovered.adult_scope().recent()])
        self.assertEqual(0o600, self.restored.stat().st_mode & 0o777)

    def test_acl_tampering_rejected_not_repaired(self):
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE memory_namespace_acl SET can_read=1 WHERE principal='workspace_agent' AND namespace='adult'")
        with self.assertRaises(MemoryAuthorizationError):
            copy_memory_database(self.source, self.backup)
        self.assertFalse(self.backup.exists())

    def test_invalid_stored_row_rejected_with_no_content_leak(self):
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE memories SET metadata_json='secret invalid json' WHERE namespace='adult'")
        with self.assertRaises(MemoryDataIntegrityError) as failure:
            copy_memory_database(self.source, self.backup)
        self.assertNotIn('secret', str(failure.exception))
        self.assertFalse(self.backup.exists())

    def test_wal_committed_memory_included(self):
        db = sqlite3.connect(self.source)
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("UPDATE memories SET content='wal committed' WHERE namespace='workspace'")
        db.commit()
        copy_memory_database(self.source, self.backup)
        self.assertEqual('wal committed', MemoryStore(self.backup).recent()[0]['content'])

    def test_wrong_db_or_extra_namespace_table_rejected(self):
        plan = self.root / 'plans.sqlite3'
        PlanStore(plan)
        with self.assertRaises(ValueError):
            copy_memory_database(plan, self.backup)
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE unrelated(secret TEXT)')
        with self.assertRaises(ValueError):
            copy_memory_database(self.source, self.backup)

    def test_existing_destination_untouched(self):
        self.backup.write_bytes(b'live')
        with self.assertRaises(FileExistsError):
            copy_memory_database(self.source, self.backup)
        self.assertEqual(b'live', self.backup.read_bytes())

    def test_unknown_columns_and_active_schema_refused(self):
        with sqlite3.connect(self.source) as db:
            db.execute('ALTER TABLE memories ADD COLUMN unrelated_secret TEXT')
        with self.assertRaises(ValueError):
            copy_memory_database(self.source, self.backup)
        self.assertFalse(self.backup.exists())

    def test_future_version_refused(self):
        with sqlite3.connect(self.source) as db:
            db.execute("INSERT INTO memory_schema_migrations VALUES(2,'future','today')")
        with self.assertRaises(ValueError):
            copy_memory_database(self.source, self.backup)
        self.assertFalse(self.backup.exists())


if __name__ == '__main__':
    unittest.main()
