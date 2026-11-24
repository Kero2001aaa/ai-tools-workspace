"""Additive proposal migration and cross-database receipt recovery."""
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from agent.adult.adult_task_store import AdultTaskStore, TaskStoreSchemaError
from agent.adult import adult_task_store as schema
from tests import test_adult_feedback as fixtures


class ProposalReceiptTests(unittest.TestCase):
    setUp=fixtures.FeedbackTests.setUp
    enable=fixtures.FeedbackTests.enable
    prepare=fixtures.FeedbackTests.prepare

    def test_proposal_status_publication_receipt_restart(self):
        task=self.prepare();proposal=self.feedback.build_proposal(task.task_id)
        before=self.feedback.publication_status(proposal.feedback_id)
        self.assertEqual('proposed',before['status']);self.assertIsNone(before['published_memory_id'])
        published=self.feedback.publish(proposal.feedback_id)
        after=self.feedback.publication_status(proposal.feedback_id)
        self.assertEqual(('published',published.id),(after['status'],after['published_memory_id']))
        self.assertEqual(before['feedback_id'],after['feedback_id'])
        from agent.adult.feedback_proposal_repository import FeedbackProposalRepository
        reopened=FeedbackProposalRepository(AdultTaskStore(self.root/'tasks.sqlite3',read_only=True))
        self.assertEqual(after,reopened.status(proposal.feedback_id))
        with self.tasks._connect() as db:
            row=dict(db.execute('SELECT * FROM adult_feedback_proposals').fetchone())
        self.assertEqual(task.task_id,row['task_id'])
        self.assertNotIn('Safe private test text.',json.dumps(row))

    def test_lost_receipt_after_publish_recovers_same_publication(self):
        task=self.prepare();proposal=self.feedback.build_proposal(task.task_id)
        with patch.object(self.feedback.proposals,'record_publication',side_effect=OSError('synthetic receipt failure')):
            with self.assertRaises(OSError):self.feedback.publish(proposal.feedback_id)
        self.assertEqual('proposed',self.feedback.publication_status(proposal.feedback_id)['status'])
        published=self.memory.recent_shareable_workspace()[0]
        self.assertEqual(published.id,self.feedback.publish(proposal.feedback_id).id)
        self.assertEqual(1,len(self.memory.recent_shareable_workspace()))
        self.assertEqual(published.id,self.feedback.publication_status(proposal.feedback_id)['published_memory_id'])

    def test_receipt_commit_then_error_is_idempotent_and_immutable(self):
        task=self.prepare();proposal=self.feedback.build_proposal(task.task_id)
        original=self.feedback.proposals.record_publication
        def commit_then_error(*args):
            original(*args);raise OSError('synthetic receipt response loss')
        with patch.object(self.feedback.proposals,'record_publication',side_effect=commit_then_error):
            with self.assertRaises(OSError):self.feedback.publish(proposal.feedback_id)
        before=self.feedback.publication_status(proposal.feedback_id)
        self.assertEqual(before['published_memory_id'],self.feedback.publish(proposal.feedback_id).id)
        self.assertEqual(before,self.feedback.publication_status(proposal.feedback_id))
        with self.tasks._connect() as db:
            for sql in ("UPDATE adult_feedback_proposals SET sanitized_summary='changed'",
                        "UPDATE adult_feedback_proposals SET published_memory_id=999",
                        "UPDATE adult_feedback_proposals SET status='proposed'",
                        'DELETE FROM adult_feedback_proposals'):
                with self.assertRaises(sqlite3.IntegrityError):db.execute(sql)

    def test_legacy_memory_only_proposal_is_recovered_on_explicit_publish(self):
        task=self.prepare()
        attempt_id,feedback=self.feedback._feedback(task.task_id)
        private=self.memory.remember_private(self.feedback._encode(attempt_id,feedback),'lesson',task.task_id,
            {'source':'adult-feedback-proposal-v1'})
        self.assertIsNone(self.feedback.proposals.status(private.id))
        published=self.feedback.publish(private.id)
        self.assertEqual(published.id,self.feedback.publication_status(private.id)['published_memory_id'])


class FeedbackMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'tasks.sqlite3'

    def old_database(self,version):
        migrations=((1,schema.SCHEMA_NAME,schema._SCHEMA),(2,schema.ATTEMPT_SCHEMA_NAME,schema._ATTEMPT_SCHEMA),
                    (3,schema.CAPABILITY_SCHEMA_NAME,schema._CAPABILITY_SCHEMA))
        with sqlite3.connect(self.db) as db:
            for number,name,statements in migrations[:version]:
                for statement in statements:db.execute(statement)
                db.execute('INSERT INTO adult_schema_migrations VALUES(?,?,?)',(number,name,'2026-11-25T00:00:00Z'))

    def test_v1_v2_v3_upgrade_and_readonly_preserve_history(self):
        for version in (1,2,3):
            with self.subTest(version=version):
                if self.db.exists():self.db.unlink()
                self.old_database(version)
                with sqlite3.connect(self.db) as db:before=db.execute('SELECT * FROM adult_schema_migrations').fetchall()
                AdultTaskStore(self.db)
                with sqlite3.connect(self.db) as db:
                    after=db.execute('SELECT * FROM adult_schema_migrations ORDER BY version').fetchall()
                    self.assertEqual(before,after[:version]);self.assertEqual([1,2,3,4],[row[0] for row in after])
                    self.assertEqual([],db.execute('PRAGMA foreign_key_check').fetchall())
                data=(self.db.read_bytes(),self.db.stat().st_mtime_ns)
                AdultTaskStore(self.db,read_only=True)
                self.assertEqual(data,(self.db.read_bytes(),self.db.stat().st_mtime_ns))

    def test_v4_failure_rolls_back_and_old_readonly_is_refused(self):
        self.old_database(3)
        with self.assertRaises(TaskStoreSchemaError):AdultTaskStore(self.db,read_only=True)
        with patch.object(schema,'_FEEDBACK_SCHEMA',('CREATE TABLE broken (',)):
            with self.assertRaises(sqlite3.OperationalError):AdultTaskStore(self.db)
        with sqlite3.connect(self.db) as db:
            self.assertEqual([(1,),(2,),(3,)],db.execute('SELECT version FROM adult_schema_migrations ORDER BY version').fetchall())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='adult_feedback_proposals'").fetchone())

    def test_same_name_noop_feedback_trigger_refused(self):
        AdultTaskStore(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute('DROP TRIGGER validate_adult_feedback_update')
            db.execute('CREATE TRIGGER validate_adult_feedback_update BEFORE UPDATE ON adult_feedback_proposals BEGIN SELECT 1; END')
        with self.assertRaises(TaskStoreSchemaError):AdultTaskStore(self.db)

    def test_same_name_feedback_table_without_constraints_is_refused(self):
        AdultTaskStore(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute('DROP TRIGGER validate_adult_feedback_update')
            db.execute('DROP TRIGGER prevent_adult_feedback_delete')
            db.execute('DROP TABLE adult_feedback_proposals')
            altered=schema._FEEDBACK_SCHEMA[0].replace("CHECK(length(feedback_type) BETWEEN 1 AND 64)", "")
            db.execute(altered)
            for statement in schema._FEEDBACK_SCHEMA[1:]:db.execute(statement)
        with self.assertRaises(TaskStoreSchemaError):AdultTaskStore(self.db)
