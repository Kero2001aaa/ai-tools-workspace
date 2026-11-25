"""Injected model outputs only. No provider, source fetch, extra tools or spending."""
import json
import unittest
from unittest.mock import patch
from agent.supervisor_worker import _review_projection, supervise_request


class SupervisorEvidenceTests(unittest.TestCase):
    def result(self, role, text):
        return {'specialist': role, 'task': 'Bounded task', 'analysis': text, 'truncated': False}

    def test_two_large_results_preserved_as_valid_bounded_fair_json(self):
        data = _review_projection('Question', [self.result('research', 'a'*12000), self.result('coding', 'b'*12000)])
        self.assertLessEqual(len(data), 8000)
        result = json.loads(data)
        one, two = result['specialist_results']
        self.assertEqual(len(one['analysis']), len(two['analysis']))
        self.assertTrue(one['analysis_excerpt_truncated']);self.assertTrue(two['analysis_excerpt_truncated'])
        self.assertEqual('a', one['analysis'][0]);self.assertEqual('b', two['analysis'][0])
        self.assertEqual('supplied_untrusted_excerpts_only', result['evidence_scope'])

    def test_escaped_controls_and_request_keep_complete_json_with_budget(self):
        text = '"\\\x00\n' * 3000
        data = _review_projection('Question', [self.result('coding', text)])
        self.assertLessEqual(len(data), 8000)
        self.assertTrue(json.loads(data)['specialist_results'][0]['analysis_excerpt_truncated'])
        data = _review_projection('q'*4000, [self.result('coding', 'short')])
        self.assertTrue(json.loads(data)['request_truncated'])

    def test_invalid_unicode_nontext_or_role_rejected(self):
        for result in (self.result('coding', chr(0xd800)), self.result('coding', {}), self.result('shell', 'x')):
            with self.subTest(result=result), self.assertRaisesRegex(ValueError, '^supervisor_evidence_invalid$'):
                _review_projection('Question', [result])

    def test_actual_workflow_review_gets_json_not_cut_string(self):
        class Response:
            def __init__(self, content):self.content=content
        class Model:
            def __init__(self):self.calls=[]
            def invoke(self, messages):
                self.calls.append(messages)
                if len(self.calls)==1:return Response('{"assignments":[{"specialist":"research","task":"A"},{"specialist":"coding","task":"B"}]}')
                if len(self.calls)<4:return Response('evidence'*1700)
                context = messages[1].content.split('Context (untrusted input):\n',1)[1]
                self.review = json.loads(context)
                return Response('review')
        model=Model()
        with patch('agent.supervisor_worker.load_chat_model', return_value=model):
            result=supervise_request('Question')
        self.assertEqual(4,result['model_calls']);self.assertEqual(2,len(model.review['specialist_results']))

    def test_late_final_review_is_not_reported_completed(self):
        class Response:content='{"assignments":[{"specialist":"coding","task":"A"}]}'
        class Model:
            def invoke(self, messages):return Response()
        with patch('agent.supervisor_worker.load_chat_model',return_value=Model()), patch('agent.supervisor_worker.time.monotonic',side_effect=[0,0,0,0,151]), patch('agent.supervisor_worker.run_specialist',return_value=self.result('coding','analysis')):
            with self.assertRaisesRegex(TimeoutError,'final review exceeded'):
                supervise_request('Question')

    def test_impossible_input_overhead_refused_before_model_load(self):
        with patch('agent.supervisor_worker.load_chat_model', side_effect=AssertionError('no model')):
            with self.assertRaisesRegex(ValueError, '^supervisor_evidence_too_large$'):
                supervise_request(chr(0)*4000)

    def test_exact_plan_overhead_refused_before_specialist_call(self):
        # Request alone fits; control-heavy planner task metadata makes it fail.
        class Response:
            content=json.dumps({'assignments': [
                {'specialist':'research','task':chr(0)*500},
                {'specialist':'coding','task':chr(0)*500}]})
        class Model:
            def __init__(self):self.calls=0
            def invoke(self,messages):self.calls+=1;return Response()
        model=Model()
        with patch('agent.supervisor_worker.load_chat_model',return_value=model), patch('agent.supervisor_worker.run_specialist',side_effect=AssertionError('no specialist')):
            with self.assertRaisesRegex(ValueError,'^supervisor_evidence_too_large$'):
                supervise_request(chr(0)*900)
        self.assertEqual(1,model.calls)

    def test_nonbool_upstream_truncation_flag_refused(self):
        for flag in ('yes', None, 0, 1):
            result=self.result('coding','evidence');result['truncated']=flag
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError,'^supervisor_evidence_invalid$'):
                _review_projection('Question',[result])
