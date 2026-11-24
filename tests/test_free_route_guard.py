import asyncio
import json
import unittest
from unittest.mock import patch
import httpx
from agent.free_route_guard import FreeRouteTransport, AsyncFreeRouteTransport, PROVIDER, URL
from agent.model import load_chat_model


class FreeRouteGuardTests(unittest.TestCase):
    def body(self):
        return {'model': 'openrouter/free', 'provider': PROVIDER, 'stream': False, 'max_tokens': 24, 'messages': [{'role': 'user', 'content': 'test'}]}

    def test_valid_at_real_http_boundary(self):
        seen=[]
        transport=FreeRouteTransport(httpx.MockTransport(lambda req: (seen.append(req) or httpx.Response(200,json={}))))
        with httpx.Client(transport=transport) as client:
            self.assertEqual(200,client.post(URL,json=self.body()).status_code)
        self.assertEqual(1,len(seen))

    def test_invoke_overrides_rejected_before_network(self):
        variants=[{'model':'paid-model'},{'models':['paid-model']},{'provider':{}},{'stream':True},{'max_tokens':999},{'tools':[]},{'plugins':[]}]
        for override in variants:
            with self.subTest(override=override):
                def inner(req):self.fail('network called')
                with httpx.Client(transport=FreeRouteTransport(httpx.MockTransport(inner))) as client:
                    with self.assertRaises(ValueError):client.post(URL,json={**self.body(),**override})

    def test_wrong_destination_and_redirect_route_rejected(self):
        with httpx.Client(transport=FreeRouteTransport(httpx.MockTransport(lambda req:self.fail('network')))) as client:
            with self.assertRaises(ValueError):client.post('https://other.invalid/api',json=self.body())

    def test_async_boundary(self):
        async def run():
            async def inner(req):self.fail('network called')
            async with httpx.AsyncClient(transport=AsyncFreeRouteTransport(httpx.MockTransport(inner))) as client:
                with self.assertRaises(ValueError):await client.post(URL,json={**self.body(),'provider':{}})
        asyncio.run(run())

    def test_loader_rejects_unverified_route(self):
        with patch.dict('os.environ',{'MODEL_API_KEY':'test','MODEL_NAME':'paid','MODEL_BASE_URL':'https://other.invalid/v1'},clear=True),patch('agent.model.load_dotenv'):
            with self.assertRaises(RuntimeError):load_chat_model()

    def test_sdk_runtime_override_cannot_bypass_guard(self):
        import os
        with patch.dict(os.environ,{'MODEL_API_KEY':'test','MODEL_NAME':'openrouter/free','MODEL_BASE_URL':'https://openrouter.ai/api/v1'},clear=True),patch('agent.model.load_dotenv'):
            model=load_chat_model()
        model.http_client._transport.inner=httpx.MockTransport(lambda req:self.fail('network called'))
        with self.assertRaises(ValueError):model.invoke('test',extra_body={'provider':{}},max_tokens=24)

    def test_direct_factory_rejects_unverified_route(self):
        from agent.model import build_chat_model
        with self.assertRaises(RuntimeError):
            build_chat_model(api_key='x',model='paid',base_url='https://other.invalid/v1')

    def test_sdk_valid_payload_reaches_mock_once(self):
        import os
        seen=[]
        def inner(req):
            seen.append(req)
            return httpx.Response(200,json={'id':'test','object':'chat.completion','created':0,'model':'openrouter/free','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'WORKSPACE_OK'}}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
        with patch.dict(os.environ,{'MODEL_API_KEY':'test','MODEL_NAME':'openrouter/free','MODEL_BASE_URL':'https://openrouter.ai/api/v1'},clear=True),patch('agent.model.load_dotenv'):
            model=load_chat_model()
        model.http_client._transport.inner=httpx.MockTransport(inner)
        self.assertEqual('WORKSPACE_OK',model.invoke('test',max_tokens=24).content)
        self.assertEqual(1,len(seen))

    def test_unverified_extensions_and_dual_caps_fail(self):
        for override in [{'modalities':['image']},{'transforms':['middle-out']},{'max_completion_tokens':999},{'messages':[{'role':'user','content':[{'type':'image_url'}]}]}]:
            with self.subTest(override=override):
                with httpx.Client(transport=FreeRouteTransport(httpx.MockTransport(lambda req:self.fail('network')))) as client:
                    with self.assertRaises(ValueError):client.post(URL,json={**self.body(),**override})

    def test_error_content_and_headers_sanitized(self):
        seen=[]
        def inner(req):
            seen.append(req)
            return httpx.Response(429,text='private-error-content',headers={'x-private':'private'})
        with httpx.Client(transport=FreeRouteTransport(httpx.MockTransport(inner)),follow_redirects=True) as client:
            response=client.post(URL,json=self.body())
        self.assertEqual(429,response.status_code)
        self.assertNotIn('private',response.text)
        self.assertNotIn('x-private',response.headers)
        self.assertEqual(1,len(seen))

    def test_redirect_never_followed_even_when_client_enabled(self):
        seen=[]
        def inner(req):
            seen.append(req)
            return httpx.Response(307,headers={'location':'https://other.invalid'})
        with httpx.Client(transport=FreeRouteTransport(httpx.MockTransport(inner)),follow_redirects=True) as client:
            response=client.post(URL,json=self.body())
        self.assertEqual(307,response.status_code)
        self.assertEqual(1,len(seen))
