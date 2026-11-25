"""Real local HTTP plan monitoring, no-create and no-dispatch boundaries."""
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from agent.control_plane import Handler, allowed_authorities
from agent.plan_store import PlanStore
from agent.plan_monitoring import plan_snapshot


class PlanMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'plans.sqlite3'
        self.env = patch.dict(os.environ, {'WORKSPACE_PLAN_DB': str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_dashboard_panel_uses_safe_text_and_lock_clearing(self):
        from agent.control_plane import _page
        page = _page()
        self.assertIn('id="plan-list"', page)
        self.assertIn("renderPlans(p)", page)
        self.assertIn("panel.textContent=lines.join", page)
        self.assertIn("'memory-list','plan-list'", page)
        self.assertIn("'/api/plans'", page)
        self.assertNotIn('panel.innerHTML', page)

    def test_missing_database_is_not_created(self):
        self.assertEqual({'available': False, 'plans': []}, plan_snapshot())
        self.assertFalse(self.path.exists())

    def test_real_identity_and_result_redaction_without_mutation(self):
        store = PlanStore(self.path)
        plan = store.create_plan('private title', [{'task_type': 'workspace_status'}, {'task_type': 'memory_recent'}])
        first = store.run_next_step(plan['id'])
        before = self.path.read_bytes()
        with patch('agent.task_worker.execute_task', side_effect=AssertionError('no dispatch')):
            result = plan_snapshot()
        step = result['plans'][0]['steps'][0]
        self.assertEqual(first['task_id'], step['task_id'])
        self.assertEqual('succeeded', step['execution_status'])
        self.assertEqual(1, result['plans'][0]['completed_steps'])
        for secret in ('private title', str(Path.cwd()), 'payload', 'result', 'last_error'):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(before, self.path.read_bytes())

    def test_unknown_running_visible_not_replayed(self):
        store = PlanStore(self.path)
        plan = store.create_plan('legacy', [{'task_type': 'workspace_status'}])
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE plan_steps SET status='running'")
        result = plan_snapshot()['plans'][0]['steps'][0]
        self.assertTrue(result['outcome_reconciliation_required'])
        self.assertIsNone(result['task_id'])

    def test_limit_and_schema(self):
        store = PlanStore(self.path)
        for _ in range(3):
            store.create_plan('plan', [{'task_type': 'workspace_status'}])
        self.assertTrue(plan_snapshot(1)['has_more'])
        self.assertEqual(1, len(plan_snapshot(1)['plans']))
        for limit in (0, 51, True, '20'):
            with self.assertRaises(ValueError):
                plan_snapshot(limit)

    def test_http_auth_origin_host_and_strict_query(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.control_token = 'test-token'
        server.allowed_authorities = allowed_authorities(server.server_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(path='/api/plans', extra=None, auth=True):
            headers = {'Authorization': 'Bearer test-token'} if auth else {}
            headers.update(extra or {})
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            status = response.status
            data = json.loads(response.read())
            connection.close()
            return status, data
        self.assertEqual(401, request(auth=False)[0])
        self.assertEqual(403, request(extra={'Origin': 'https://untrusted.invalid'})[0])
        self.assertEqual(403, request(extra={'Host': 'untrusted.invalid'})[0])
        for query in ('?limit=1&limit=2', '?limit=', '?limit=51', '?unknown=1'):
            self.assertEqual(400, request('/api/plans' + query)[0])
        self.assertEqual((200, {'available': False, 'plans': []}), request())
        self.assertFalse(self.path.exists())
        PlanStore(self.path).create_plan('local', [{'task_type': 'workspace_status'}])
        status, result = request()
        self.assertEqual(200, status)
        self.assertEqual(1, len(result['plans']))


if __name__ == '__main__':
    unittest.main()
