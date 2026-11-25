"""Full application through real SDK and guarded HTTP, network replaced by mock."""
import asyncio
import base64
import json
import unittest
from unittest.mock import patch

import httpx

from agent.adult.fixed_free_text_application import FixedFreeTextApplication
from agent.adult.models import TaskBudget
from agent.free_route_guard import PROVIDER, URL
from agent.model import build_chat_model
from tests import test_fixed_free_text_service as fixtures


class FullWireTests(unittest.TestCase):
    setUp = fixtures.FixedServiceTests.setUp
    service = fixtures.FixedServiceTests.service
    enable_route = fixtures.FixedServiceTests.enable_route

    def journey(self, status, response):
        service=self.service();self.enable_route(service)
        app=FixedFreeTextApplication(service,budget=TaskBudget(max_provider_calls=1))
        seen=[];clients=[]
        def inner(request):
            seen.append(request)
            return httpx.Response(status,json=response,headers={'x-private':'provider-private-detail',
                'location':'https://other.invalid/never-follow'})
        def factory(**kwargs):
            client=build_chat_model(**kwargs)
            # Keep production FreeRouteTransport in place. Replace only its
            # lowest network transport; no request can leave this process.
            client.http_client._transport.inner=httpx.MockTransport(inner)
            client.http_async_client._transport.inner=httpx.MockTransport(lambda request:self.fail('unexpected async request'))
            clients.append(client)
            return client
        try:
            with patch.dict('os.environ',{'ADULT_PAYLOAD_ENCRYPTION_KEY':self.key}),patch(
                'agent.adult.integrations.omniroute_chat.build_chat_model',side_effect=factory):
                task=app.submit_text_task('text.analyze','Private source test sentence.',retain_payload=True)
                self.assertEqual([],seen)
                result=app.run_task(task['task_id'])
                repeated=app.run_task(task['task_id'])
                self.assertEqual(1,len(seen))
                self.assertEqual(1,repeated['task']['attempt_count'])
            self.assertEqual(URL,str(seen[0].url))
            body=json.loads(seen[0].content)
            self.assertEqual('openrouter/free',body['model'])
            self.assertEqual(PROVIDER,body['provider'])
            self.assertFalse(body['stream'])
            self.assertNotIn('tools',body);self.assertNotIn('plugins',body)
            self.assertEqual('system',body['messages'][0]['role'])
            envelope=json.loads(body['messages'][1]['content'])
            self.assertEqual('Private source test sentence.',envelope['source_text'])
            safe=json.dumps([result,app.get_attempts(task['task_id'])])
            self.assertNotIn('provider-private-detail',safe)
            self.assertNotIn('Private source test sentence.',safe)
            self.assertEqual((1,1),(result['task']['attempt_count'],result['task']['provider_calls']))
            return app,task['task_id'],result
        finally:
            for client in clients:
                client.http_client.close()
                asyncio.run(client.http_async_client.aclose())

    def test_success_real_sdk_through_wire_guard_to_encrypted_result(self):
        app,task,result=self.journey(200,{'id':'synthetic','object':'chat.completion','created':0,
            'model':'openrouter/free','choices':[{'index':0,'finish_reason':'stop',
            'message':{'role':'assistant','content':'Structurally accepted mock HTTP output.'}}],
            'usage':{'prompt_tokens':5,'completion_tokens':5,'total_tokens':10}})
        self.assertEqual('completed',result['task']['status'])
        chunk=app.get_result_chunk(task)
        self.assertEqual(b'Structurally accepted mock HTTP output.',base64.b64decode(chunk['data_base64']))

    def test_429_sanitized_no_sdk_or_application_retry(self):
        app,task,result=self.journey(429,{'error':{'message':'provider-private-detail'}})
        self.assertEqual('failed',result['task']['status'])
        with self.assertRaises(ValueError):app.get_result_chunk(task)

    def test_redirect_not_followed_and_never_completed(self):
        app,task,result=self.journey(307,{'error':{'message':'provider-private-detail'}})
        self.assertEqual('failed',result['task']['status'])
        with self.assertRaises(ValueError):app.get_result_chunk(task)
