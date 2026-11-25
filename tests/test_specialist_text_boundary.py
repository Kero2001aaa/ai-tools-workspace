"""Tool-less model responses must be text, including the final reviewer."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.specialist_worker import run_specialist
from agent.supervisor_worker import supervise_request


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return SimpleNamespace(content=next(self.responses))


class SpecialistTextBoundaryTests(unittest.TestCase):
    def test_non_text_and_surrogate_responses_refused_for_each_role(self):
        for role in ('research', 'coding', 'review'):
            for value in (None, True, 42, {}, [], [{'text': 'looks valid'}], '\ud800', 'x' * 12000 + '\ud800'):
                with self.subTest(role=role, kind=type(value).__name__):
                    model = Model([value])
                    with self.assertRaisesRegex(ValueError, '^specialist_response_invalid$'):
                        run_specialist(role, 'Analyze', model=model)
                    self.assertEqual(1, model.calls)

    def test_arbitrary_object_is_not_stringified(self):
        class Content:
            def __str__(self):
                raise AssertionError('stringification must not run')
        with self.assertRaisesRegex(ValueError, '^specialist_response_invalid$'):
            run_specialist('review', 'Review', model=Model([Content()]))

    def test_valid_unicode_truncation_contract_unchanged(self):
        content = 'تحليل آمن 😀' * 1500
        result = run_specialist('coding', 'Analyze', model=Model([content]))
        self.assertEqual(content[:12000], result['analysis'])
        self.assertIs(True, result['truncated'])
        result['analysis'].encode('utf-8')

    def test_supervisor_stops_at_bad_specialist_or_final_review_without_retry(self):
        plan = '{"assignments":[{"specialist":"coding","task":"Inspect"}]}'
        for responses, calls in (([plan, {'text': 'bad'}], 2), ([plan, 'valid evidence', []], 3), ([plan, 'valid evidence', '\udfff'], 3)):
            with self.subTest(calls=calls):
                model = Model(responses)
                with patch('agent.supervisor_worker.load_chat_model', return_value=model):
                    with self.assertRaisesRegex(ValueError, '^specialist_response_invalid$'):
                        supervise_request('Review the supplied code')
                self.assertEqual(calls, model.calls)


if __name__ == '__main__':
    unittest.main()
