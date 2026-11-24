"""Actual separate-process durable local CLI roundtrip, no network."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest


class DurableStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.env = dict(os.environ, ADULT_PAYLOAD_ENCRYPTION_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())

    def call(self, command, *args, data=b'', env=None):
        return subprocess.run([sys.executable, '-m', 'agent.adult.durable_stats_cli',
            '--state-dir', str(self.root), command, *args], input=data,
            capture_output=True, env=self.env if env is None else env, timeout=15)

    def test_stage_restart_run_status_result_reopen_no_duplicate(self):
        text = 'one two\nthree'
        stage = self.call('stage', data=text.encode())
        self.assertEqual(0, stage.returncode, stage.stderr)
        task = json.loads(stage.stdout)['task_id']
        status = self.call('status', task, env={k:v for k,v in self.env.items() if k != 'ADULT_PAYLOAD_ENCRYPTION_KEY'})
        staged = json.loads(status.stdout)
        self.assertEqual(('checkpointed', 0, False), (staged['status'], staged['attempts'], staged['has_result']))
        run = self.call('run', task); self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(('completed', 1), (json.loads(run.stdout)['status'], json.loads(run.stdout)['attempts']))
        result = self.call('result', task); self.assertEqual(0, result.returncode, result.stderr)
        stats = json.loads(result.stdout)['statistics']
        self.assertEqual((13,3,2), (stats['characters'],stats['words_whitespace'],stats['lines']))
        self.assertEqual(1, json.loads(self.call('run',task).stdout)['attempts'])
        final = json.loads(self.call('status', task).stdout)
        self.assertEqual(staged['deadline_at'], final['deadline_at'])
        for file in self.root.rglob('*'):
            if file.is_file(): self.assertNotIn(text.encode(), file.read_bytes())
        for output in (stage, status, run, result):
            self.assertNotIn(text.encode(), output.stdout + output.stderr)

    def test_missing_key_or_invalid_input_creates_no_state(self):
        missing = dict(self.env); missing.pop('ADULT_PAYLOAD_ENCRYPTION_KEY')
        for data, env in ((b'valid input', missing),(b'\xff', self.env),(b'',self.env),(b'x'*81921,self.env)):
            with self.subTest(size=len(data)):
                result = self.call('stage', data=data, env=env)
                self.assertEqual(1, result.returncode)
                self.assertFalse(self.root.exists())

    def test_status_missing_state_does_not_create_and_result_requires_completed(self):
        result = self.call('status','missing-task')
        self.assertEqual(1,result.returncode); self.assertFalse(self.root.exists())
        task = json.loads(self.call('stage',data=b'private sample').stdout)['task_id']
        result = self.call('result',task)
        self.assertEqual(1,result.returncode)
        self.assertEqual(0,json.loads(self.call('status',task).stdout)['attempts'])

    def test_wrong_key_cannot_read_result(self):
        task = json.loads(self.call('stage',data=b'private sample').stdout)['task_id']
        self.assertEqual(0,self.call('run',task).returncode)
        wrong = dict(self.env, ADULT_PAYLOAD_ENCRYPTION_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        result = self.call('result',task,env=wrong)
        self.assertEqual(1,result.returncode)
        self.assertNotIn(b'private sample',result.stdout+result.stderr)

    def test_wrong_key_run_preserves_checkpointed_input_and_can_run_with_right_key(self):
        task = json.loads(self.call('stage',data=b'private sample').stdout)['task_id']
        wrong = dict(self.env, ADULT_PAYLOAD_ENCRYPTION_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        self.assertEqual(1,self.call('run',task,env=wrong).returncode)
        status = json.loads(self.call('status',task).stdout)
        self.assertEqual(('checkpointed',0),(status['status'],status['attempts']))
        self.assertEqual(0,self.call('run',task).returncode)

    def test_readonly_status_does_not_change_database(self):
        task = json.loads(self.call('stage',data=b'private sample').stdout)['task_id']
        db = self.root/'tasks.sqlite3'
        before = (db.read_bytes(), db.stat().st_mtime_ns)
        self.assertEqual(0,self.call('status',task).returncode)
        self.assertEqual(before,(db.read_bytes(),db.stat().st_mtime_ns))

    def test_cancelled_task_never_runs(self):
        from agent.adult.adult_task_store import AdultTaskStore
        task = json.loads(self.call('stage',data=b'private sample').stdout)['task_id']
        AdultTaskStore(self.root/'tasks.sqlite3').request_cancel(task)
        result = self.call('run',task)
        self.assertEqual(0,result.returncode)
        self.assertEqual(('cancelled',0),(json.loads(result.stdout)['status'],json.loads(result.stdout)['attempts']))
