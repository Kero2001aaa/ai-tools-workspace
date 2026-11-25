"""Standalone monitoring never opens a writable queue or migrates schemas."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from agent.task_queue import TaskQueue
from agent.task_monitoring import task_snapshot
from agent.control_plane import Handler, allowed_authorities, _safe_db_summary


class TaskMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'queue?#.sqlite3'
        setting=patch('agent.task_monitoring.DEFAULT_DB',self.path)
        setting.start();self.addCleanup(setting.stop)

    def test_missing_store_never_created_and_limits_strict(self):
        self.assertEqual([],task_snapshot())
        self.assertFalse(self.path.exists())
        for value in (True,0,101,1.0,'1'):
            with self.assertRaises(ValueError):task_snapshot(value)

    def test_actual_queue_contents_compatible_and_bytes_unchanged(self):
        queue=TaskQueue(self.path)
        queue.enqueue('workspace_status')
        expected=queue.list_tasks()
        before=(self.path.read_bytes(),self.path.stat().st_mtime_ns)
        with patch('agent.control_plane.TaskQueue',side_effect=AssertionError('no constructor')):
            self.assertEqual(expected,task_snapshot())
        self.assertEqual(before,(self.path.read_bytes(),self.path.stat().st_mtime_ns))
        self.assertEqual(1,_safe_db_summary(self.path,'tasks'))
        self.assertFalse((self.root/'queue').exists())

    def test_legacy_schema_read_without_column_migration(self):
        queue=TaskQueue(self.path)
        queue.enqueue('workspace_status')
        with sqlite3.connect(self.path) as db:
            db.execute('DROP INDEX idx_tasks_plan_step')
            db.execute('ALTER TABLE tasks DROP COLUMN plan_step_id')
        before=self.path.read_bytes()
        self.assertEqual(1,len(task_snapshot()))
        self.assertNotIn('plan_step_id',task_snapshot()[0])
        self.assertEqual(before,self.path.read_bytes())

    def test_wal_committed_rows_visible_and_no_dispatch(self):
        queue=TaskQueue(self.path)
        with sqlite3.connect(self.path) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            queue.enqueue('workspace_status')
            with patch('agent.task_worker.execute_task',side_effect=AssertionError('no dispatch')):
                self.assertEqual('pending',task_snapshot()[0]['status'])
            self.assertEqual(0,queue.list_tasks()[0]['attempts'])

    def test_symlink_nonfile_and_view_rejected(self):
        self.path.symlink_to(self.root/'missing')
        with self.assertRaises(ValueError):task_snapshot()
        self.path.unlink();self.path.mkdir()
        with self.assertRaises(ValueError):task_snapshot()
        self.path.rmdir()
        with sqlite3.connect(self.path) as db:db.execute('CREATE VIEW tasks AS SELECT 1 AS id')
        with self.assertRaises(ValueError):task_snapshot()

    def test_http_auth_missing_no_create_and_corrupt_fixed_error(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.control_token='test-token';server.allowed_authorities=allowed_authorities(server.server_port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            def get(auth):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                connection.request('GET','/api/tasks',headers={'Authorization':'Bearer test-token'} if auth else {})
                response=connection.getresponse();data=response.read();connection.close()
                return response.status,json.loads(data)
            self.assertEqual(401,get(False)[0]);self.assertFalse(self.path.exists())
            self.assertEqual((200,[]),get(True));self.assertFalse(self.path.exists())
            self.path.write_bytes(b'PRIVATE_CORRUPT_STORE')
            self.assertEqual((503,{'error':'task monitoring unavailable'}),get(True))
        finally:server.shutdown();server.server_close();thread.join(timeout=3)
