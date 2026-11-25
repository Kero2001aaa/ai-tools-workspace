"""Actual durable snapshot to existing supervisor SDK, mock network only."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from openai import RateLimitError
from agent.model import build_chat_model
from agent.plan_recovery import copy_database
from agent.plan_store import PlanStore
from agent.saved_code_context import saved_code_context
from agent.supervisor_worker import supervise_request
from scripts.local_code_journey import run_journey


class SavedSupervisorSDKJourneyTests(unittest.TestCase):
    def test_saved_recovery_to_sdk_success_and_429_never_replays_durable_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            # Fixed reviewed project source and existing private acceptance retention.
            outcome=run_journey(root/'acceptance')
            source=root/'acceptance/restored.sqlite3'
            backup=root/'second-restored.sqlite3'
            copy_database(source,backup)
            store=PlanStore(backup)
            before=store.get_plan(outcome['plan_id'])
            task_before=store.queue.get(outcome['task_id'])
            context=saved_code_context(backup,outcome['plan_id'],outcome['step_id'])
            self.assertLessEqual(len(context),4000)
            evidence=json.loads(context)
            self.assertIn('def text_stats(',evidence['citations'][0]['excerpt'])
            self.assertEqual(outcome['task_id'],evidence['task_id'])
            for fail_call in (None,1,2,3):
                seen=[]
                def respond(request):
                    body=json.loads(request.content);seen.append(body)
                    if len(seen)==fail_call:
                        return httpx.Response(429,json={'error':{'message':'PRIVATE_PROVIDER_BODY'}})
                    if len(seen)==1:
                        text='{"assignments":[{"specialist":"coding","task":"Assess only supplied saved quote"}]}'
                    elif len(seen)==2:
                        text='Synthetic specialist: the stored quote contains text_stats. Not independent understanding.'
                    else:
                        text='Synthetic final review: stored snapshot only, not current source or qualified model analysis.'
                    return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,
                        'model':'openrouter/free','choices':[{'index':0,'finish_reason':'stop',
                        'message':{'role':'assistant','content':text}}]})
                model=build_chat_model(api_key='synthetic-not-a-key',model='openrouter/free',base_url='https://openrouter.ai/api/v1')
                model.http_client._transport.inner=httpx.MockTransport(respond)
                try:
                    with patch('agent.supervisor_worker.load_chat_model',return_value=model),patch(
                        'socket.socket.connect',side_effect=AssertionError('network forbidden')),patch(
                        'agent.code_task.execute_code_report',side_effect=AssertionError('no worker replay')):
                        if fail_call is None:
                            result=supervise_request('Assess only the saved project quote',context)
                            self.assertEqual('completed',result['status'])
                            self.assertEqual(3,result['input_budget']['attempted_calls'])
                            self.assertEqual(sum(len(m['content'].encode()) for b in seen for m in b['messages']),result['input_budget']['content_utf8_bytes'])
                        else:
                            with self.assertRaises(RateLimitError) as error:
                                supervise_request('Assess only the saved project quote',context)
                            self.assertNotIn('PRIVATE_PROVIDER_BODY',str(error.exception))
                    self.assertEqual(3 if fail_call is None else fail_call,len(seen))
                    if len(seen)>=2:
                        self.assertIn(context,seen[1]['messages'][1]['content'])
                    for body in seen:
                        self.assertEqual('openrouter/free',body['model'])
                        self.assertEqual(False,body['provider']['allow_fallbacks'])
                        self.assertEqual('deny',body['provider']['data_collection'])
                        self.assertEqual({'prompt':0,'completion':0},body['provider']['max_price'])
                        self.assertNotIn('tools',body)
                        self.assertEqual(512,body.get('max_tokens',body.get('max_completion_tokens')))
                finally:
                    model.http_client.close()
                    asyncio.run(model.http_async_client.aclose())
                self.assertEqual(before,store.get_plan(outcome['plan_id']))
                self.assertEqual(task_before,store.queue.get(outcome['task_id']))
                self.assertEqual(1,store.queue.get(outcome['task_id'])['attempts'])
