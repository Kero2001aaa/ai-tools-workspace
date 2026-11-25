"""Actual supervisor plus specialists, injected model only, shared call budget."""
import json
import unittest
from unittest.mock import patch
from agent.supervisor_worker import supervise_request


class SupervisorEvidenceJourneyTests(unittest.TestCase):
    def test_adversarial_large_specialist_outputs_preserve_both_roles_and_call_budget(self):
        class Response:
            def __init__(self, value):self.content=value
        class Model:
            def __init__(self):self.calls=[]
            def invoke(self, messages):
                self.calls.append(messages)
                count=len(self.calls)
                if count==1:return Response('{"assignments":[{"specialist":"research","task":"Analyze provided evidence"},{"specialist":"coding","task":"Analyze supplied code"}]}')
                if count==2:return Response('Research marker ' + ('"\\\x00\n' * 3000))
                if count==3:return Response('Coding marker ' + ('ع'*12000))
                self.raw_context=messages[1].content.split('Context (untrusted input):\n',1)[1]
                self.review_context=json.loads(self.raw_context)
                return Response('Synthetic review, evidence is incomplete.')
        model=Model()
        with patch('agent.supervisor_worker.load_chat_model',return_value=model):
            result=supervise_request('Compare supplied evidence only')
        self.assertEqual(4,len(model.calls));self.assertEqual(4,result['model_calls'])
        self.assertLessEqual(len(model.raw_context),8000)
        one,two=model.review_context['specialist_results']
        self.assertEqual(['research','coding'],[one['specialist'],two['specialist']])
        self.assertTrue(one['analysis'].startswith('Research marker'))
        self.assertTrue(two['analysis'].startswith('Coding marker'))
        self.assertTrue(one['analysis_excerpt_truncated']);self.assertTrue(two['analysis_excerpt_truncated'])
        self.assertEqual(len(one['analysis']),len(two['analysis']))
        self.assertEqual('untrusted_specialist_output',one['trust'])
        self.assertEqual('supplied_untrusted_excerpts_only',model.review_context['evidence_scope'])

    def test_workflow_stops_if_fixed_review_overhead_does_not_fit(self):
        class Response:
            def __init__(self, value):self.content=value
        class Model:
            def __init__(self):self.count=0
            def invoke(self, messages):
                self.count+=1
                if self.count==1:return Response('{"assignments":[{"specialist":"coding","task":"Analyze"}]}')
                return Response('Synthetic evidence')
        model=Model()
        with patch('agent.supervisor_worker.load_chat_model',return_value=model):
            with self.assertRaisesRegex(ValueError,'^supervisor_evidence_too_large$'):
                supervise_request('\x00'*4000)
        self.assertEqual(0,model.count)
