import copy
import unittest
import httpx
from agent.free_tool_contract import FreeToolContract, TOOL_PROVIDER
from agent.free_route_guard import URL, validate_request

SCHEMA = [{'type':'function','function':{'name':'workspace_status','description':'Read local status','parameters':{'type':'object','properties':{},'additionalProperties':False}}}]

class FreeToolContractTests(unittest.TestCase):
    def body(self):
        return {'model':'openrouter/free','provider':copy.deepcopy(TOOL_PROVIDER),'stream':False,'max_tokens':128,'tools':copy.deepcopy(SCHEMA),'tool_choice':'auto','messages':[{'role':'user','content':'Read status'}]}
    def check(self, body):
        FreeToolContract(SCHEMA).validate(httpx.Request('POST',URL,json=body))
    def test_production_still_denies_tools(self):
        with self.assertRaises(ValueError):validate_request(httpx.Request('POST',URL,json=self.body()))
    def test_candidate_accepts_pinned_schema(self):self.check(self.body())
    def test_contract_copies_schema(self):
        schema=copy.deepcopy(SCHEMA);contract=FreeToolContract(schema);schema[0]['function']['name']='shell'
        contract.validate(httpx.Request('POST',URL,json=self.body()))
    def test_unapproved_name_rejected(self):
        schema=copy.deepcopy(SCHEMA);schema[0]['function']['name']='shell'
        with self.assertRaises(ValueError):FreeToolContract(schema)
    def test_runtime_overrides_rejected(self):
        for change in [{'provider':{}},{'model':'paid'},{'plugins':[]},{'tools':[]},{'parallel_tool_calls':True},{'max_tokens':999},{'tool_choice':'required'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.check({**self.body(),**change})
    def test_matched_serial_result(self):
        body=self.body();body['messages'] += [{'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'workspace_status','arguments':'{}'}}]},{'role':'tool','content':'ready','tool_call_id':'call_1'}];self.check(body)
    def test_unmatched_tool_result_rejected(self):
        b=self.body();b['messages'].append({'role':'tool','content':'ready','tool_call_id':'unknown'})
        with self.assertRaises(ValueError):self.check(b)
    def test_wrong_destination_rejected(self):
        with self.assertRaises(ValueError):FreeToolContract(SCHEMA).validate(httpx.Request('POST','https://other.invalid',json=self.body()))

    def test_real_sdk_serial_tool_roundtrip_offline(self):
        import json
        from unittest.mock import patch
        from langchain_core.messages import HumanMessage, ToolMessage
        from agent.model import load_chat_model
        seen=[]
        contract=FreeToolContract(SCHEMA)
        def inner(request):
            contract.validate(request)
            seen.append(json.loads(request.content))
            message={'role':'assistant','content':'Status read'}
            finish='stop'
            if len(seen)==1:
                message={'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'workspace_status','arguments':'{}'}}]}
                finish='tool_calls'
            return httpx.Response(200,json={'id':'test','object':'chat.completion','created':0,'model':'openrouter/free','choices':[{'index':0,'finish_reason':finish,'message':message}]})
        with patch.dict('os.environ',{'MODEL_API_KEY':'test','MODEL_NAME':'openrouter/free','MODEL_BASE_URL':'https://openrouter.ai/api/v1'},clear=True),patch('agent.model.load_dotenv'):
            model=load_chat_model()
        # Only replace the transport in this offline test. No runtime opt-in exists.
        model.http_client._transport=httpx.MockTransport(inner)
        bound=model.bind_tools(SCHEMA,tool_choice='auto',parallel_tool_calls=False).bind(extra_body={'provider':TOOL_PROVIDER})
        first=bound.invoke([HumanMessage('Read status')])
        final=bound.invoke([HumanMessage('Read status'),first,ToolMessage('ready',tool_call_id='call_1')])
        self.assertEqual('Status read',final.content)
        self.assertEqual(2,len(seen))
        model.http_client.close()

    def test_invalid_types_fail_closed(self):
        for message in [{'role':[],'content':'x'},{'role':'tool','content':'x','tool_call_id':[]},{'role':'assistant','content':None,'tool_calls':[{'id':'call','type':'function','function':{'name':[],'arguments':'{}'}}]}]:
            b=self.body();b['messages'].append(message)
            with self.subTest(message=message),self.assertRaises(ValueError):self.check(b)

    def test_pending_and_duplicate_calls_fail(self):
        call={'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'workspace_status','arguments':'{}'}}]}
        b=self.body();b['messages'].append(call)
        with self.assertRaises(ValueError):self.check(b)
        b['messages'] += [{'role':'tool','content':'ok','tool_call_id':'call_1'},copy.deepcopy(call),{'role':'tool','content':'ok','tool_call_id':'call_1'}]
        with self.assertRaises(ValueError):self.check(b)

    def test_real_graph_executes_one_pinned_read_only_tool_offline(self):
        import asyncio
        import json
        from unittest.mock import patch
        from langchain_core.messages import HumanMessage
        from langchain_core.tools import StructuredTool
        from agent.model import load_chat_model
        from agent.tool_agent import build_tool_agent
        calls=[]
        tool_runs=[]
        from agent.mcp_server import workspace_status as actual_status
        def workspace_status():
            """Read workspace status."""
            tool_runs.append('read')
            return actual_status()
        tool=StructuredTool.from_function(workspace_status)
        # Pin the exact SDK serialization before constructing the mock boundary.
        from langchain_core.utils.function_calling import convert_to_openai_tool
        schema=[convert_to_openai_tool(tool)]
        contract=FreeToolContract(schema)
        def inner(request):
            contract.validate(request)
            calls.append(json.loads(request.content))
            if len(calls)==1:
                message={'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'workspace_status','arguments':'{}'}}]};finish='tool_calls'
            else:
                message={'role':'assistant','content':'Status read'};finish='stop'
            return httpx.Response(200,json={'id':'test','object':'chat.completion','created':0,'model':'openrouter/free','choices':[{'index':0,'finish_reason':finish,'message':message}]})
        with patch.dict('os.environ',{'MODEL_API_KEY':'test','MODEL_NAME':'openrouter/free','MODEL_BASE_URL':'https://openrouter.ai/api/v1'},clear=True),patch('agent.model.load_dotenv'):
            model=load_chat_model()
        model.http_client._transport=httpx.MockTransport(inner)
        # Inject candidate provider only into the test adapter, never production.
        model.extra_body={'provider':TOOL_PROVIDER}
        graph=build_tool_agent(model,[tool])
        result=asyncio.run(graph.ainvoke({'messages':[HumanMessage('Read status')]},config={'recursion_limit':5}))
        self.assertEqual('Status read',result['messages'][-1].content)
        self.assertEqual(['read'],tool_runs)
        self.assertEqual(2,len(calls))
        model.http_client.close()

    def test_status_arguments_are_not_arbitrary(self):
        for args in ['{"path":"/private"}', '{"x":1,"x":2}', '[]', 'null']:
            b=self.body();b['messages'] += [{'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'workspace_status','arguments':args}}]},{'role':'tool','content':'ok','tool_call_id':'call_1'}]
            with self.subTest(args=args),self.assertRaises(ValueError):self.check(b)

    def test_real_memory_tool_name_and_bounds(self):
        from agent.memory_mcp_server import recent_memories
        self.assertEqual('recent_memories',recent_memories.__name__)
        schema=copy.deepcopy(SCHEMA);schema[0]['function']['name']='recent_memories'
        contract=FreeToolContract(schema)
        for limit,valid in [(1,True),(20,True),(21,False),(0,False),(True,False),('5',False)]:
            import json
            b=self.body();b['tools']=schema;b['messages'] += [{'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'recent_memories','arguments':json.dumps({'limit':limit})}}]},{'role':'tool','content':'[]','tool_call_id':'call_1'}]
            if valid:contract.validate(httpx.Request('POST',URL,json=b))
            else:
                with self.assertRaises(ValueError):contract.validate(httpx.Request('POST',URL,json=b))
