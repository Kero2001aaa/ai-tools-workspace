"""Actual SDK/free-route transport with injected HTTP responses only."""
import json
import unittest
from unittest.mock import patch
import httpx
from agent.model import build_chat_model
from agent.supervisor_worker import supervise_request


class SharedBudgetSDKTests(unittest.TestCase):
    def test_all_model_hops_use_same_free_guard_and_account_actual_sdk_content(self):
        seen=[]
        def respond(request):
            body=json.loads(request.content);seen.append(body)
            text='{"assignments":[{"specialist":"coding","task":"Assess this supplied code"}]}' if len(seen)==1 else 'Injected analysis only'
            return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,'model':'openrouter/free','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':text}}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
        model=build_chat_model(api_key='synthetic-not-a-key',model='openrouter/free',base_url='https://openrouter.ai/api/v1')
        model.http_client._transport.inner=httpx.MockTransport(respond)
        try:
            with patch('agent.supervisor_worker.load_chat_model',return_value=model),patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
                result=supervise_request('راجع الكود المقدم','def sample(): return 1')
            self.assertEqual(3,len(seen))
            self.assertEqual(sum(len(m['content'].encode('utf8')) for body in seen for m in body['messages']),result['input_budget']['content_utf8_bytes'])
            self.assertEqual(3,result['input_budget']['attempted_calls'])
            for body in seen:
                self.assertEqual('openrouter/free',body['model']);self.assertEqual(False,body['provider']['allow_fallbacks'])
                self.assertEqual('deny',body['provider']['data_collection']);self.assertEqual({'prompt':0,'completion':0},body['provider']['max_price'])
                self.assertEqual(512,body.get('max_tokens',body.get('max_completion_tokens')))
                self.assertNotIn('tools',body)
        finally:model.http_client.close()
