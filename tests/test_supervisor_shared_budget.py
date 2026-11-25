"""Actual invocation boundary shared across planner, specialists and reviewer."""
import unittest
from unittest.mock import patch
from langchain_core.messages import HumanMessage
from agent.supervisor_worker import _SharedModelBudget, supervise_request


class SharedBudgetTests(unittest.TestCase):
    def test_exact_utf8_bytes_and_attempt_charge_before_failed_call(self):
        class Model:
            calls=0
            def invoke(self,messages):self.calls+=1;raise RuntimeError('provider failed')
        source=Model();budget=_SharedModelBudget(source)
        with patch('agent.supervisor_worker.MAX_WORKFLOW_INPUT_BYTES',4):
            with self.assertRaisesRegex(RuntimeError,'provider failed'):budget.invoke([HumanMessage(content='أأ')])
            self.assertEqual((1,4),(budget.attempted_calls,budget.input_bytes))
            with self.assertRaisesRegex(ValueError,'budget_exceeded'):budget.invoke([HumanMessage(content='a')])
        self.assertEqual(1,source.calls)

    def test_calls_are_independent_of_bytes_no_retry(self):
        class Model:
            def invoke(self,messages):return 'result'
        budget=_SharedModelBudget(Model())
        for unused in range(4):budget.invoke([HumanMessage(content='')])
        with self.assertRaisesRegex(ValueError,'budget_exceeded'):budget.invoke([HumanMessage(content='')])
        self.assertEqual(4,budget.attempted_calls)

    def test_real_workflow_shares_same_ledger_all_four_calls(self):
        class Model:
            def __init__(self):self.calls=[]
            def invoke(self,messages):
                self.calls.append(messages)
                return '{"assignments":[{"specialist":"coding","task":"inspect"},{"specialist":"research","task":"assess"}]}' if len(self.calls)==1 else 'analysis'
        source=Model()
        with patch('agent.supervisor_worker.load_chat_model',return_value=source):
            result=supervise_request('Check code','approved context')
        self.assertEqual(4,len(source.calls))
        self.assertEqual(4,result['input_budget']['attempted_calls'])
        self.assertEqual(sum(len(m.content.encode('utf8')) for messages in source.calls for m in messages),result['input_budget']['content_utf8_bytes'])

    def test_specialist_or_reviewer_stopped_before_model_when_shared_input_exhausted(self):
        class Model:
            calls=0
            def invoke(self,messages):
                self.calls+=1
                return '{"assignments":[{"specialist":"coding","task":"inspect"}]}' if self.calls==1 else 'analysis'
        baseline=Model()
        with patch('agent.supervisor_worker.load_chat_model',return_value=baseline):
            used=supervise_request('Check code')['input_budget']['content_utf8_bytes']
        for limit,expected in [(100,0),(used-1,2)]:
            source=Model()
            with patch('agent.supervisor_worker.load_chat_model',return_value=source),patch('agent.supervisor_worker.MAX_WORKFLOW_INPUT_BYTES',limit):
                with self.assertRaisesRegex(ValueError,'budget_exceeded'):supervise_request('Check code')
            self.assertEqual(expected,source.calls)

    def test_invalid_content_never_reaches_source(self):
        class Model:
            def invoke(self,messages):raise AssertionError('unexpected model call')
        budget=_SharedModelBudget(Model())
        for messages in ([],None,[object()],[HumanMessage(content='\ud800')]):
            with self.assertRaises(ValueError):budget.invoke(messages)
        self.assertEqual(0,budget.attempted_calls)
