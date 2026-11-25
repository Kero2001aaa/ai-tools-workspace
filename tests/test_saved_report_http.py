"""Actual authenticated local GET with stored temporary quote and no dispatch."""
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from agent.control_plane import Handler, allowed_authorities, _page
from agent.code_task import configured_context
from agent.plan_store import PlanStore


class SavedReportHTTPTests(unittest.TestCase):
    def test_actual_auth_host_origin_query_binding_and_no_reexecution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.py';source.write_text('def safe():\n    pass\n')
            with patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT':json.dumps({'root':str(root),'approved_files':['source.py']})}):
                payload={'question':'What function?', 'selections':[{'path':'source.py','start_line':1,'max_lines':1}], 'config_sha256':configured_context()[1]}
                store=PlanStore(root/'plans.sqlite3');plan=store.create_plan('Private title',[{'task_type':'local_code_report','payload':payload}]);store.run_next_step(plan['id'])
            before=store.get_plan(plan['id']);source.unlink()
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.control_token='synthetic';server.allowed_authorities=allowed_authorities(server.server_port)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def get(query,headers=None):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                connection.request('GET','/api/code-report'+query,headers=headers or {})
                response=connection.getresponse();status=response.status;body=response.read();connection.close();return status,json.loads(body)
            query='?plan_id='+plan['id']+'&step_id='+plan['steps'][0]['id'];auth={'Authorization':'Bearer synthetic'}
            try:
                with patch.dict(os.environ,{'WORKSPACE_PLAN_DB':str(store.db_path)}),patch('agent.code_task.execute_code_report',side_effect=AssertionError('no dispatch')):
                    self.assertEqual(401,get(query)[0])
                    self.assertEqual(403,get(query,{**auth,'Origin':'https://untrusted.invalid'})[0])
                    self.assertEqual(403,get(query,{**auth,'Host':'untrusted.invalid'})[0])
                    for bad in ('',query+'&extra=x',query+'&plan_id=x','?plan_id=x&step_id=x'):
                        self.assertEqual(404,get(bad,auth)[0])
                    status,result=get(query,auth);self.assertEqual(200,status)
                    self.assertEqual('def safe():\n',result['citations'][0]['quote'])
                    self.assertNotIn('Private title',str(result));self.assertNotIn(str(root),str(result))
                    self.assertEqual(before,store.get_plan(plan['id']))
            finally:server.shutdown();server.server_close()

    def test_ui_safe_text_clear_and_request_fence(self):
        page=_page()
        self.assertIn('id="saved-report"',page)
        self.assertIn("output.textContent=lines.join",page)
        self.assertIn('reportRequest===request',page)
        self.assertIn("document.getElementById('saved-report').textContent='';",page)
        self.assertNotIn('output.innerHTML',page)
        self.assertIn('لا تعرض أنواع مهام الخادم الأخرى',page)
        self.assertNotIn('two allowlisted task types',page)
