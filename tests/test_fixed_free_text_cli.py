"""Actual process restart tests. No provider routes or network are configured."""
import base64
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent.adult.fixed_free_text_cli import main
from tests import test_fixed_free_text_service as service_fixtures


class FixedFreeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.env = dict(os.environ, ADULT_PAYLOAD_ENCRYPTION_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            ADULT_OMNIROUTE_BASE_URL='https://openrouter.ai/api/v1', ADULT_OMNIROUTE_API_KEY='synthetic-unit-test-value')

    def call(self, command, *args, envelope=None, env=None, data=None):
        return subprocess.run([sys.executable, '-m', 'agent.adult.fixed_free_text_cli',
            '--state-dir', str(self.root), command, *args],
            input=data if data is not None else json.dumps(envelope or {'text':'private sample'}).encode(),
            capture_output=True, env=self.env if env is None else env, timeout=15)

    def stage(self):
        result = self.call('stage', '--task-type', 'text.analyze', '--provider-call-budget', '1')
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)['task_id']

    def test_stage_restart_keyless_status_no_route_pause_and_cancel(self):
        task = self.stage()
        minimal = {k:v for k,v in self.env.items() if not k.startswith('ADULT_')}
        status = self.call('status',task,env=minimal)
        self.assertEqual(('checkpointed',0),tuple(json.loads(status.stdout)[k] for k in ('status','attempts')))
        run = self.call('run',task)
        self.assertEqual(0,run.returncode,run.stderr)
        paused = json.loads(run.stdout)
        self.assertEqual(('paused',0,0),(paused['status'],paused['attempts'],paused['provider_calls']))
        self.assertEqual(1,self.call('result',task).returncode)
        cancelled = self.call('cancel',task,env=minimal)
        self.assertEqual('cancelled',json.loads(cancelled.stdout)['status'])
        self.assertEqual(0,json.loads(self.call('run',task).stdout)['provider_calls'])
        for path in self.root.rglob('*'):
            if path.is_file(): self.assertNotIn(b'private sample',path.read_bytes())
        for result in (status,run,cancelled):
            self.assertNotIn(b'private sample',result.stdout+result.stderr)

    def test_missing_config_or_invalid_input_creates_no_state(self):
        for key in ('ADULT_PAYLOAD_ENCRYPTION_KEY','ADULT_OMNIROUTE_BASE_URL','ADULT_OMNIROUTE_API_KEY'):
            env=dict(self.env);env.pop(key)
            result=self.call('stage','--task-type','text.analyze','--provider-call-budget','1',env=env)
            self.assertEqual(1,result.returncode);self.assertFalse(self.root.exists())
        for data in (b'',b'\xff',b'[]',b'{"text":"x","endpoint":"bad"}',b'{"text":null}',b'{"text":"first","text":"second"}',b'{"text":NaN}',b'x'*163841):
            result=self.call('stage','--task-type','text.analyze','--provider-call-budget','1',data=data)
            self.assertEqual(1,result.returncode);self.assertFalse(self.root.exists())

    def test_wrong_key_or_endpoint_run_preserves_checkpoint(self):
        task=self.stage();db=self.root/'tasks.sqlite3';before=db.read_bytes()
        for env in (dict(self.env,ADULT_PAYLOAD_ENCRYPTION_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()),
                    dict(self.env,ADULT_OMNIROUTE_BASE_URL='https://example.com/v1')):
            result=self.call('run',task,env=env)
            self.assertEqual(1,result.returncode);self.assertEqual(before,db.read_bytes())
        self.assertEqual('checkpointed',json.loads(self.call('status',task).stdout)['status'])

    def test_missing_state_and_status_reads_do_not_write(self):
        self.assertEqual(1,self.call('status','missing').returncode);self.assertFalse(self.root.exists())
        task=self.stage();db=self.root/'tasks.sqlite3';before=(db.read_bytes(),db.stat().st_mtime_ns)
        self.assertEqual(0,self.call('status',task).returncode)
        self.assertEqual(before,(db.read_bytes(),db.stat().st_mtime_ns))

    def test_budget_required_and_payload_not_an_argument(self):
        result=self.call('stage','--task-type','text.analyze')
        self.assertEqual(2,result.returncode);self.assertFalse(self.root.exists())
        result=self.call('stage','--task-type','text.analyze','--provider-call-budget','0')
        self.assertEqual(2,result.returncode);self.assertFalse(self.root.exists())

    def test_explicit_result_chunk_uses_real_encrypted_artifact(self):
        from agent.adult.adult_task_store import AdultTaskStore
        from agent.adult.payload_store import AdultArtifactStore, PayloadCipher
        task=self.stage();cipher=PayloadCipher(self.env['ADULT_PAYLOAD_ENCRYPTION_KEY'])
        tasks=AdultTaskStore(self.root/'tasks.sqlite3')
        # Reuse the already-tested fake-client service fixture for a real completion.
        fixture=service_fixtures.FixedServiceTests();fixture.tasks=tasks;fixture.root=self.root
        from agent.adult.fixed_free_text_service import FixedFreeTextService
        from agent.adult.integrations.omniroute_chat import AdultOmniRouteConfig
        from types import SimpleNamespace
        from unittest.mock import Mock
        service=FixedFreeTextService(tasks,AdultArtifactStore(self.root/'artifacts',cipher=cipher),cipher,
            AdultOmniRouteConfig(self.env['ADULT_OMNIROUTE_BASE_URL'],self.env['ADULT_OMNIROUTE_API_KEY']))
        fixture.enable_route(service)
        client=Mock();client.invoke.return_value=SimpleNamespace(content='Safe fake output.',usage_metadata={},response_metadata={})
        with patch.dict(os.environ,self.env),patch('agent.adult.integrations.omniroute_chat.build_chat_model',return_value=client):
            self.assertEqual('completed',service.run(task).status)
        result=self.call('result',task,'--max-bytes','4')
        self.assertEqual(0,result.returncode,result.stderr)
        artifact=json.loads(result.stdout)['artifact']
        self.assertEqual(b'Safe',base64.b64decode(artifact['data_base64']))
        self.assertFalse(artifact['complete']);self.assertEqual(4,artifact['next_offset'])
        self.assertEqual(1,self.call('result',task,'--offset','-1').returncode)
        # Completed output is independent of provider credentials/availability.
        key_only={k:v for k,v in self.env.items() if k not in {'ADULT_OMNIROUTE_BASE_URL','ADULT_OMNIROUTE_API_KEY'}}
        self.assertEqual(0,self.call('result',task,env=key_only).returncode)
