"""Real-store generic feedback bridge, offline and no caller payload reads."""
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from agent.adult.adult_feedback import AdultFeedback, FeedbackProposal, _LESSONS
from agent.adult.adult_memory import AdultMemory
from agent.memory_store import MemoryStore, MemoryAuthorizationError
from tests import test_adult_text_executor as fixtures
CONFIG = fixtures.CONFIG


class FeedbackTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    enable = fixtures.ExecutorTests.enable

    def prepare(self):
        self.enable()
        task = self.executor.create('text.analyze', self.data)
        with patch.object(self.adapter, 'invoke', side_effect=TimeoutError()):
            result = self.executor.run(task.task_id, self.data, CONFIG)
        self.assertEqual('paused', result.status)
        self.memdb = self.root / 'memory.sqlite3'
        self.memory_store = MemoryStore(self.memdb)
        self.memory = AdultMemory(self.memory_store.adult_scope())
        self.feedback = AdultFeedback(self.tasks, self.tracker,
            self.memory_store.feedback_publisher_scope(), self.memory, self.cipher)
        return task

    def test_failure_private_proposal_publish_restart_recall_next_task(self):
        task = self.prepare()
        with patch.object(self.artifacts, 'load_input', side_effect=AssertionError('no input read')):
            proposal = self.feedback.build_proposal(task.task_id)
            self.assertIsNotNone(proposal)
            self.assertEqual([], self.memory.recent_shareable_workspace())
            clean = self.feedback.sanitize(proposal)
            self.assertEqual('transient_failure', clean.category)
            self.assertEqual(64, len(clean.source_fingerprint))
            published = self.feedback.publish(proposal.feedback_id)
        store = MemoryStore(self.memdb)
        adult = AdultMemory(store.adult_scope())
        reopened = AdultFeedback(self.tasks, self.tracker, store.feedback_publisher_scope(), adult, self.cipher)
        self.assertEqual(published.id, reopened.publish(proposal.feedback_id).id)
        next_task = self.executor.create('text.analyze', self.data)
        with patch.object(self.adapter, 'invoke', side_effect=AssertionError('recall cannot call')):
            lessons = adult.recall_shareable_workspace('text.analyze provider_timeout')
        self.assertEqual([published.id], [row.id for row in lessons])
        self.assertEqual(0, self.tasks.get_task(next_task.task_id).attempt_count)
        self.assertEqual('feedback_publisher', lessons[0].owner_agent)
        self.assertEqual('workspace', lessons[0].namespace)
        self.assertEqual('shareable', lessons[0].visibility)
        self.assertEqual(1, len(store.recent()))
        with sqlite3.connect(self.memdb) as db:
            data = str(db.execute('SELECT content,metadata_json FROM memories').fetchall())
        for forbidden in ('Safe private test text.', task.task_id, 'Deterministic local plumbing result.'):
            # Task ID is a structured private row column only, never content/publication.
            self.assertNotIn(forbidden, data)
        self.assertEqual(0, self.adapter.calls)

    def test_publication_is_atomic_idempotent_for_rebuild_and_concurrent_callers(self):
        task = self.prepare()
        first = self.feedback.build_proposal(task.task_id)
        second = self.feedback.build_proposal(task.task_id)
        def publish(proposal):
            store = MemoryStore(self.memdb)
            bridge = AdultFeedback(self.tasks, self.tracker, store.feedback_publisher_scope(),
                AdultMemory(store.adult_scope()), self.cipher)
            return bridge.publish(proposal.feedback_id).id
        with ThreadPoolExecutor(max_workers=2) as pool:
            refs = list(pool.map(publish, (first, second)))
        self.assertEqual(refs[0], refs[1])
        with sqlite3.connect(self.memdb) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM memories WHERE owner_agent='feedback_publisher'").fetchone()[0])

    def test_forged_private_proposal_and_wrong_scope_ids_cannot_publish(self):
        task = self.prepare()
        proposal = self.feedback.build_proposal(task.task_id)
        row = self.memory.get_private(proposal.feedback_id)
        value = json.loads(row.content)
        value['feedback']['lesson'] = 'Unverified proposed replacement text.'
        fake = self.memory.remember_private(json.dumps(value), 'lesson', task.task_id, row.metadata)
        with self.assertRaisesRegex(ValueError, 'feedback_proposal_invalid'):
            self.feedback.publish(fake.id)
        workspace_ref = self.memory_store.workspace_scope().remember('Workspace private note')
        self.assertIsNone(self.memory.get_private(workspace_ref.id))
        self.assertIsNone(self.memory_store.workspace_scope().get_owned(proposal.feedback_id))
        with self.assertRaises(ValueError): self.feedback.publish(workspace_ref.id)
        self.assertEqual([], self.memory.recent_shareable_workspace())

    def test_sanitizer_rejects_sensitive_and_malformed_publication_without_insert(self):
        task = self.prepare(); proposal = self.feedback.build_proposal(task.task_id)
        clean = self.feedback.sanitize(proposal)
        publisher = self.memory_store.feedback_publisher_scope()
        for bad in ('https://example.com/private', 'user@example.com', '/tmp/private', 'api_key synthetic'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                publisher.publish_sanitized_feedback(replace(clean, lesson=bad), idempotent=True)
        self.assertEqual([], self.memory.recent_shareable_workspace())

    def test_unknown_policy_budget_and_active_tasks_produce_no_proposal(self):
        task = self.prepare()
        original = self.tracker.list_summaries(task.task_id)[0]
        for failure, code, status in (
            ('unknown', 'PROVIDER_ERROR_UNRECOGNIZED', 'failed'),
            ('policy_rejection', 'POLICY_REJECTED', 'rejected'),
            ('budget_exhaustion', 'COST_POLICY_BLOCKED', 'failed'),
        ):
            with self.subTest(failure=failure), patch.object(self.tracker, 'list_summaries',
                return_value=(replace(original, failure_class=failure, error_code=code, status=status),)):
                self.assertIsNone(self.feedback.build_proposal(task.task_id))
        queued = self.executor.create('text.analyze', self.data)
        self.assertIsNone(self.feedback.build_proposal(queued.task_id))
        self.assertEqual([], self.memory.recent_private())

    def test_idempotent_retry_checks_acl_and_stored_integrity(self):
        task = self.prepare(); proposal = self.feedback.build_proposal(task.task_id)
        self.feedback.publish(proposal.feedback_id)
        with sqlite3.connect(self.memdb) as db:
            db.execute("UPDATE memory_namespace_acl SET can_write=0 WHERE principal='feedback_publisher'")
        with self.assertRaises(MemoryAuthorizationError):self.feedback.publish(proposal.feedback_id)
        with sqlite3.connect(self.memdb) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM memories WHERE owner_agent='feedback_publisher'").fetchone()[0])

    def test_fixed_mapping_outputs_remain_in_publisher_allowlists(self):
        from agent.memory_store import FEEDBACK_CATEGORIES
        for category, _, lesson, action in _LESSONS.values():
            self.assertIn(category, FEEDBACK_CATEGORIES)
            self.assertLessEqual(len(lesson), 1000); self.assertLessEqual(len(action), 256)

    def test_publication_commit_then_lost_response_retries_same_ref(self):
        task = self.prepare(); proposal = self.feedback.build_proposal(task.task_id)
        publisher = self.feedback.publisher
        original = publisher.publish_sanitized_feedback
        committed = []
        def commit_then_error(*args, **kwargs):
            committed.append(original(*args, **kwargs).id)
            raise OSError('synthetic lost response')
        with patch.object(publisher, 'publish_sanitized_feedback', side_effect=commit_then_error):
            with self.assertRaises(OSError): self.feedback.publish(proposal.feedback_id)
        self.assertEqual(committed[0], self.feedback.publish(proposal.feedback_id).id)
        self.assertEqual(1, len(self.memory.recent_shareable_workspace()))

    def test_idempotent_lookup_rejects_corrupted_publication(self):
        from agent.memory_store import MemoryDataIntegrityError
        task = self.prepare(); proposal = self.feedback.build_proposal(task.task_id)
        published = self.feedback.publish(proposal.feedback_id)
        with sqlite3.connect(self.memdb) as db:
            db.execute('UPDATE memories SET content=? WHERE id=?', ('Changed stored lesson.', published.id))
        with self.assertRaises(MemoryDataIntegrityError): self.feedback.publish(proposal.feedback_id)

    def test_publisher_default_keeps_legacy_duplicate_semantics(self):
        task = self.prepare(); proposal = self.feedback.build_proposal(task.task_id)
        clean = self.feedback.sanitize(proposal); publisher = self.feedback.publisher
        self.assertNotEqual(publisher.publish_sanitized_feedback(clean).id,
                            publisher.publish_sanitized_feedback(clean).id)
