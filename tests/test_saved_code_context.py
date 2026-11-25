"""Saved snapshot projection stays read-only and within supervisor input bounds."""
import hashlib
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from agent.saved_code_context import saved_code_context
from agent.supervisor_worker import supervise_request
from tests import test_code_report_retrieval as fixtures


class SavedCodeContextTests(unittest.TestCase):
    setUp = fixtures.SavedCodeReportTests.setUp

    def test_actual_saved_report_source_deleted_and_plan_unchanged(self):
        before = self.store.get_plan(self.plan['id'])
        self.source.unlink()
        result = json.loads(saved_code_context(self.store.db_path, self.plan['id'], self.step))
        self.assertEqual(before, self.store.get_plan(self.plan['id']))
        self.assertEqual(self.first['task_id'], result['task_id'])
        self.assertEqual('def sample():\n', result['citations'][0]['excerpt'])
        self.assertIs(False, result['citations'][0]['excerpt_truncated'])
        self.assertNotIn(str(self.root), str(result))

    def test_long_escaped_unicode_quotes_keep_fair_complete_json_and_hashes(self):
        from agent.code_report_cli import read_saved_report
        report = read_saved_report(self.store.db_path, self.plan['id'], self.step)
        one = report['citations'][0]
        two = {**one, 'path': 'second.py'}
        report['citations'] = [one, two]
        for c, marker in ((one, 'FIRST'), (two, 'SECOND')):
            c['quote'] = marker + ('ع😀\x00"\\\n' * 1500)
            c['excerpt_sha256'] = hashlib.sha256(c['quote'].encode()).hexdigest()
        with patch('agent.saved_code_context.read_saved_report', return_value=report):
            raw = saved_code_context('unused', self.plan['id'], self.step)
        self.assertLessEqual(len(raw), 4000)
        result = json.loads(raw)
        self.assertEqual(2, len(result['citations']))
        self.assertEqual(len(result['citations'][0]['excerpt']), len(result['citations'][1]['excerpt']))
        for original, entry in zip(report['citations'], result['citations']):
            self.assertTrue(entry['excerpt_truncated'])
            self.assertEqual(original['excerpt_sha256'], entry['stored_quote_sha256'])
            self.assertEqual(hashlib.sha256(entry['excerpt'].encode()).hexdigest(), entry['excerpt_sha256'])
            self.assertEqual(original['start_line'], entry['stored_start_line'])

    def test_invalid_unicode_stored_tail_refused_before_projection(self):
        from agent.code_report_cli import read_saved_report
        report = read_saved_report(self.store.db_path, self.plan['id'], self.step)
        report['question'] = 'x' * 900 + '\ud800'
        with patch('agent.saved_code_context.read_saved_report', return_value=report):
            with self.assertRaisesRegex(ValueError, '^saved_code_context_invalid$'):
                saved_code_context('unused', self.plan['id'], self.step)

    def test_real_supervisor_injected_model_receives_saved_evidence_without_rerun(self):
        context = saved_code_context(self.store.db_path, self.plan['id'], self.step)
        class Model:
            def __init__(self):self.calls=[]
            def invoke(self, messages):
                self.calls.append(messages)
                if len(self.calls)==1:
                    return SimpleNamespace(content='{"assignments":[{"specialist":"coding","task":"Assess supplied stored quote"}]}')
                return SimpleNamespace(content='Synthetic analysis only, not verified understanding.')
        model = Model()
        with patch('agent.supervisor_worker.load_chat_model', return_value=model):
            result = supervise_request('Assess the supplied saved code only', context)
        self.assertEqual(3, len(model.calls))
        self.assertIn(context, model.calls[1][1].content)
        self.assertEqual('completed', result['status'])
        self.assertEqual(1, self.store.queue.get(self.first['task_id'])['attempts'])

    def test_missing_report_refused_without_creating_database(self):
        missing=self.root/'missing.sqlite3'
        with self.assertRaisesRegex(ValueError, '^saved_code_report_unavailable$'):
            saved_code_context(missing, self.plan['id'], self.step)
        self.assertFalse(missing.exists())

    def test_impossible_metadata_budget_refuses_not_broken_json(self):
        with patch('agent.saved_code_context.MAX_CONTEXT_CHARACTERS', 1):
            with self.assertRaisesRegex(ValueError, '^saved_code_context_too_large$'):
                saved_code_context(self.store.db_path, self.plan['id'], self.step)
