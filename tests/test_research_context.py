"""Actual temporary SQLite plans. The report never becomes an executed result."""
import hashlib
import tempfile
from pathlib import Path
import unittest
import uuid
from agent.plan_store import PlanStore
from agent.research_context import plan_evidence_envelope


class ResearchContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'plans.sqlite3'
        self.store = PlanStore(self.path)
        self.plan = self.store.create_plan('Private plan title', [{'task_type': 'workspace_status'}])
        self.step = self.plan['steps'][0]['id']
        self.source = {'title': 'Example', 'url': 'https://example.com', 'snippet': 'Exact evidence',
            'source': 'Exa', 'truncated': False, 'evidence_sha256': hashlib.sha256(b'Exact evidence').hexdigest()}

    def envelope(self, plan=None, step=None, path=None):
        return plan_evidence_envelope(path or self.path, plan or self.plan['id'], step or self.step,
            'Question', [self.source], [{'source_index': 0, 'quote': 'Exact evidence'}])

    def test_existing_pair_bound_without_execution_or_storage_mutation(self):
        before = self.path.read_bytes()
        result = self.envelope()
        self.assertEqual(self.step, result['step_id'])
        self.assertFalse(result['persisted']);self.assertFalse(result['execution_provenance_verified'])
        self.assertEqual('pending', result['step_status_snapshot'])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.plan, self.store.get_plan(self.plan['id']))
        self.assertNotIn('Private plan title', str(result))
        self.assertNotIn(str(self.path), str(result))

    def test_wrong_plan_step_pair_is_not_an_association(self):
        other = self.store.create_plan('Other', [{'task_type': 'workspace_status'}])
        with self.assertRaisesRegex(ValueError, '^research_context_unavailable$'):
            self.envelope(step=other['steps'][0]['id'])

    def test_missing_database_not_created_and_errors_are_sanitized(self):
        missing = Path(self.temp.name) / 'missing.sqlite3'
        with self.assertRaisesRegex(ValueError, '^research_context_unavailable$'):
            self.envelope(path=missing)
        self.assertFalse(missing.exists())
        with self.assertRaisesRegex(ValueError, '^research_context_unavailable$'):
            self.envelope(step=str(uuid.uuid4()))

    def test_actual_completed_state_not_report_completion_claim(self):
        self.store.run_next_step(self.plan['id'])
        result = self.envelope()
        self.assertEqual('succeeded', result['step_status_snapshot'])
        self.assertFalse(result['execution_provenance_verified'])
        self.assertEqual('workspace_status', result['step_task_type'])

    def test_invalid_quote_and_mismatched_scrape_refused_before_context(self):
        self.source['snippet'] = 'tampered'
        with self.assertRaisesRegex(ValueError, '^research_evidence_invalid$'):
            self.envelope()

    def test_checkpointed_plan_supported_without_any_step_replay(self):
        plan = self.store.create_plan('Two steps', [{'task_type': 'workspace_status'}, {'task_type': 'workspace_status'}])
        self.store.run_next_step(plan['id'])
        result = self.envelope(plan=plan['id'], step=plan['steps'][1]['id'])
        self.assertEqual('checkpointed', result['plan_status_snapshot'])
        self.assertEqual('pending', result['step_status_snapshot'])
        self.assertEqual('pending', self.store.get_plan(plan['id'])['steps'][1]['status'])

    def test_invalid_identity_never_reaches_database(self):
        for identity in ('../private.sqlite3', 3, 'not-an-id'):
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, '^research_context_unavailable$'):
                self.envelope(step=identity)
