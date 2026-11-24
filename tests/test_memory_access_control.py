"""Namespace isolation, fixed-scope, audit, and feedback publication tests."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.adult.adult_memory import AdultMemory
from agent.memory_store import (
    FeedbackPublisherStore,
    MemoryAuthorizationError,
    MemoryStore,
    SanitizedFeedback,
)


VALID_FEEDBACK = SanitizedFeedback(
    category="compatibility_issue",
    component_id="provider.example",
    version="1.2.3",
    operation="text.generate",
    failure_code="invalid_response_shape",
    lesson="A provider adapter may return a structured response envelope.",
    recommended_action="Normalize the response envelope before extracting text.",
    verification_status="reproduced",
    source_fingerprint="a" * 64,
)


class MemoryAccessControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.sqlite3"
        self.store = MemoryStore(self.db_path)
        self.workspace = self.store.workspace_scope()
        self.adult_scope = self.store.adult_scope()
        self.adult = AdultMemory(self.adult_scope)
        self.publisher = self.store.feedback_publisher_scope()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_workspace_and_adult_writes_use_fixed_namespaces(self):
        workspace_ref = self.workspace.remember("Workspace-only project note")
        adult_ref = self.adult.remember_private("Private Adult task strategy", "strategy")
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT id, namespace, visibility, owner_agent FROM memories ORDER BY id"
        ).fetchall()
        connection.close()
        self.assertEqual(
            [(workspace_ref.id, "workspace", "internal", "workspace"),
             (adult_ref.id, "adult", "internal", "adult_agent")],
            rows,
        )

    def test_workspace_cannot_read_adult_records(self):
        self.adult.remember_private("secret-only-adult-phrase", "fact")
        self.assertEqual([], self.store.recall("secret-only-adult-phrase"))
        self.assertEqual([], self.store.recent())

    def test_adult_reads_own_private_records_and_only_shareable_workspace_records(self):
        self.adult.remember_private("adult-private-needle", "fact")
        self.workspace.remember("workspace-internal-needle")
        self.publisher.publish_sanitized_feedback(VALID_FEEDBACK)

        private = self.adult.recall_private("adult-private-needle")
        shareable = self.adult.recall_shareable_workspace("structured response envelope")
        self.assertEqual(["adult-private-needle"], [item.content for item in private])
        self.assertEqual(["A provider adapter may return a structured response envelope."],
                         [item.content for item in shareable])
        self.assertEqual([], self.adult.recall_shareable_workspace("workspace-internal-needle"))

    def test_workspace_can_read_published_shareable_feedback(self):
        self.publisher.publish_sanitized_feedback(VALID_FEEDBACK)
        records = self.store.recall("structured response envelope")
        self.assertEqual(1, len(records))
        self.assertEqual(
            "A provider adapter may return a structured response envelope.",
            records[0]["content"],
        )
        connection = sqlite3.connect(self.db_path)
        memory_type = connection.execute(
            "SELECT memory_type FROM memories WHERE id=?", (records[0]["id"],)
        ).fetchone()[0]
        connection.close()
        self.assertEqual("lesson", memory_type)

    def test_feedback_publisher_is_write_only_and_has_no_generic_memory_api(self):
        self.assertFalse(hasattr(self.publisher, "recall"))
        self.assertFalse(hasattr(self.publisher, "recent"))
        self.assertFalse(hasattr(self.publisher, "remember"))
        self.assertFalse(hasattr(self.publisher, "get"))
        self.assertIsInstance(self.publisher, FeedbackPublisherStore)

    def test_feedback_publisher_rejects_invalid_or_sensitive_fields(self):
        invalid = [
            SanitizedFeedback(**{**VALID_FEEDBACK.__dict__, "category": "other"}),
            SanitizedFeedback(**{**VALID_FEEDBACK.__dict__, "lesson": "See https://example.com/private"}),
            SanitizedFeedback(**{**VALID_FEEDBACK.__dict__, "recommended_action": "Email user@example.com"}),
            SanitizedFeedback(**{**VALID_FEEDBACK.__dict__, "source_fingerprint": "not-a-fingerprint"}),
            SanitizedFeedback(**{**VALID_FEEDBACK.__dict__, "verification_status": "unknown"}),
        ]
        for feedback in invalid:
            with self.subTest(feedback=feedback):
                with self.assertRaises(ValueError):
                    self.publisher.publish_sanitized_feedback(feedback)

    def test_local_path_forms_rejected_in_feedback_and_metadata(self):
        paths = ['/tmp/synthetic.txt', '/workspaces/project/private.md', '/etc/synthetic',
                 '/var/data/private', '//server/private', '~/private', '../private', './private',
                 r'C:\private\file', 'C:/private/file', r'\\server\private']
        for path in paths:
            for text in (path, 'Inspect '+path, 'path="'+path+'"', '('+path+')'):
                with self.subTest(text=text):
                    for field in ('lesson', 'recommended_action'):
                        feedback = SanitizedFeedback(**{**VALID_FEEDBACK.__dict__, field:text})
                        with self.assertRaises(ValueError):
                            self.publisher.publish_sanitized_feedback(feedback)
                    with self.assertRaises(ValueError):
                        self.adult_scope.remember('synthetic fact', metadata={'component_id':text})
                    with self.assertRaises(ValueError):
                        self.adult_scope.remember('synthetic fact', metadata={'tags':[text]})
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM memories').fetchone()[0])

    def test_technical_slash_identifiers_remain_allowed(self):
        for value in ('owner/repo', 'text/generate', 'provider.example/v1', 'application/json'):
            with self.subTest(value=value):
                self.adult_scope.remember('synthetic fact', metadata={'component_id':value})
                feedback = SanitizedFeedback(**{**VALID_FEEDBACK.__dict__, 'component_id':value, 'lesson':'Normalize '+value+' before parsing.'})
                self.publisher.publish_sanitized_feedback(feedback)

    def test_explicit_credential_assignments_rejected_in_metadata_values(self):
        markers = ['password=synthetic', 'API_KEY: synthetic', 'access-token = synthetic',
                   'client_secret="synthetic"', 'Authorization: Bearer synthetic',
                   '{"password": "synthetic"}']
        for value in markers:
            for metadata in ({'source':value}, {'tags':[value]}, {'source':{'source':value}}):
                with self.subTest(metadata=metadata):
                    with self.assertRaises(ValueError):
                        self.adult_scope.remember('synthetic fact', metadata=metadata)
        for value in ('password-policy', 'api_key_rotation', 'oauth/access-token',
                      'provider.example/v1', 'owner/repo'):
            self.adult_scope.remember('synthetic fact', metadata={'source':value})

    def test_mem_acl_10_extra_fields_and_oversized_prose_rejected(self):
        with self.assertRaises(TypeError):
            SanitizedFeedback(**{**VALID_FEEDBACK.__dict__, 'raw_content':'synthetic'})
        for field in ('lesson', 'recommended_action'):
            with self.subTest(field=field):
                feedback = SanitizedFeedback(**{**VALID_FEEDBACK.__dict__, field:'x'*1001})
                with self.assertRaises(ValueError):
                    self.publisher.publish_sanitized_feedback(feedback)

    def test_mem_acl_07_startup_rejects_modified_schema_policy(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM memory_namespace_acl WHERE principal='adult_agent'")
        with self.assertRaises(MemoryAuthorizationError):
            MemoryStore(self.db_path)

    def test_mem_acl_12_sql_trace_has_scope_predicates_before_return(self):
        self.workspace.remember('shared synthetic needle')
        self.adult.remember_private('private synthetic needle', 'fact')
        self.publisher.publish_sanitized_feedback(VALID_FEEDBACK)
        original_connect = self.store._connect
        trace = []
        def traced_connect():
            connection = original_connect()
            connection.set_trace_callback(trace.append)
            return connection
        with patch.object(self.store, '_connect', side_effect=traced_connect):
            cases = [
                (lambda:self.workspace.recent(), "m.namespace='workspace'", 'can_read=1'),
                (lambda:self.adult.recall_private('synthetic'), "m.namespace='adult'", 'can_read=1'),
                (lambda:self.adult.recall_shareable_workspace('provider'), "m.visibility='shareable'", 'can_read_shareable=1'),
            ]
            for call, namespace, permission in cases:
                trace.clear()
                call()
                selects = [sql for sql in trace if sql.startswith('SELECT m.id')]
                self.assertEqual(1, len(selects))
                where = selects[0].split(' WHERE ',1)[1]
                self.assertIn(namespace, where)
                self.assertIn(permission, where)
                self.assertIn('memory_namespace_acl', where)
                if permission == 'can_read_shareable=1':
                    self.assertIn("m.namespace='workspace'", where)

    def test_feedback_publication_writes_only_sanitized_shareable_lesson(self):
        ref = self.publisher.publish_sanitized_feedback(VALID_FEEDBACK)
        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT content, tags, namespace, memory_type, visibility, sensitivity, "
            "task_id, content_hash, metadata_json, owner_agent FROM memories WHERE id=?",
            (ref.id,),
        ).fetchone()
        connection.close()
        self.assertEqual("A provider adapter may return a structured response envelope.", row[0])
        self.assertEqual('["compatibility_issue"]', row[1])
        self.assertEqual(("workspace", "lesson", "shareable", "unknown", None, None, "feedback_publisher"),
                         (row[2], row[3], row[4], row[5], row[6], row[7], row[9]))
        self.assertIn('"verification_status": "reproduced"', row[8])

    def test_adult_memory_rejects_workspace_scope(self):
        with self.assertRaises(TypeError):
            AdultMemory(self.workspace)

    def test_fixed_acl_matrix_is_seeded_exactly(self):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT principal, namespace, can_read, can_write, can_read_shareable "
            "FROM memory_namespace_acl ORDER BY principal, namespace"
        ).fetchall()
        connection.close()
        self.assertEqual(6, len(rows))
        self.assertIn(("workspace_agent", "workspace", 1, 1, 0), rows)
        self.assertIn(("workspace_agent", "adult", 0, 0, 0), rows)
        self.assertIn(("adult_agent", "adult", 1, 1, 0), rows)
        self.assertIn(("adult_agent", "workspace", 0, 0, 1), rows)
        self.assertIn(("feedback_publisher", "workspace", 0, 1, 0), rows)
        self.assertIn(("feedback_publisher", "adult", 0, 0, 0), rows)

    def test_legacy_workspace_wrapper_return_shapes_are_unchanged(self):
        saved = self.store.remember("Compatibility wrapper note", ["compat"])
        self.assertEqual({"id", "created_at", "tags"}, set(saved))
        recalled = self.store.recall("Compatibility wrapper")
        self.assertEqual(
            {"id", "content", "tags", "created_at", "matched_terms"},
            set(recalled[0]),
        )
        recent = self.store.recent()
        self.assertEqual({"id", "content", "tags", "created_at"}, set(recent[0]))

    def test_feedback_publisher_acl_tampering_fails_closed_and_is_audited(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "UPDATE memory_namespace_acl SET can_write=0 "
            "WHERE principal='feedback_publisher' AND namespace='workspace'"
        )
        connection.commit()
        connection.close()
        with self.assertRaises(MemoryAuthorizationError):
            self.publisher.publish_sanitized_feedback(VALID_FEEDBACK)
        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT principal, operation, namespace, memory_id "
            "FROM memory_access_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        connection.close()
        self.assertEqual(("feedback_publisher", "denied", "workspace", None), row)

    def test_workspace_scope_cannot_call_adult_shareable_reader(self):
        with self.assertRaises(MemoryAuthorizationError):
            self.workspace.recall_shareable_workspace("anything")

    def test_acl_tampering_fails_closed_and_denial_audit_has_no_query(self):
        self.workspace.remember("safe baseline")
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "DELETE FROM memory_namespace_acl WHERE principal='workspace_agent' AND namespace='workspace'"
        )
        connection.commit()
        connection.close()
        with self.assertRaises(MemoryAuthorizationError):
            self.workspace.recall("distinctive private query phrase")
        connection = sqlite3.connect(self.db_path)
        audits = connection.execute(
            "SELECT principal, operation, namespace, memory_id FROM memory_access_audit"
        ).fetchall()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(memory_access_audit)")]
        connection.close()
        self.assertIn(("workspace_agent", "denied", "workspace", None), audits)
        self.assertNotIn("query", columns)
        self.assertNotIn("content", columns)

    def test_audit_contains_only_operation_metadata(self):
        secret_query = "query-secret-987"
        secret_content = "content-secret-654"
        self.workspace.remember(secret_content)
        self.workspace.recall(secret_query)
        self.workspace.recent()
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT principal, operation, namespace, memory_id, created_at FROM memory_access_audit"
        ).fetchall()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(memory_access_audit)")]
        connection.close()
        serialized = repr(rows)
        self.assertNotIn(secret_query, serialized)
        self.assertNotIn(secret_content, serialized)
        self.assertEqual(
            {"id", "principal", "operation", "namespace", "memory_id", "created_at"},
            set(columns),
        )
        self.assertTrue(any(row[1] == "remember" for row in rows))
        self.assertTrue(any(row[1] == "recall" for row in rows))
        self.assertTrue(any(row[1] == "recent" for row in rows))

    def test_zero_result_and_zero_token_reads_are_audited(self):
        self.assertEqual([], self.workspace.recall("query-with-no-matches"))
        self.assertEqual([], self.workspace.recall("!!!"))
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT operation, memory_id FROM memory_access_audit ORDER BY id"
        ).fetchall()
        connection.close()
        self.assertEqual([("recall", None), ("recall", None)], rows)

    def test_connections_enable_foreign_keys_and_keep_delete_journal(self):
        connection = self.store._connect()
        self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
        self.assertEqual("delete", connection.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual(10_000, connection.execute("PRAGMA busy_timeout").fetchone()[0])
        connection.close()

    def test_metadata_rejects_unapproved_keys_and_raw_prompt_fields(self):
        with self.assertRaises(ValueError):
            self.adult_scope.remember("private fact", metadata={"prompt": "raw"})
        with self.assertRaises(ValueError):
            self.adult_scope.remember("private fact", metadata={"unknown_key": "value"})

    def test_scope_cannot_be_constructed_with_a_caller_selected_principal(self):
        with self.assertRaises(TypeError):
            type(self.workspace)(object(), self.store, "adult_agent", "adult", "adult_agent")


if __name__ == "__main__":
    unittest.main()
