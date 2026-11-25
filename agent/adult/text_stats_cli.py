"""Explicitly offline text-statistics journey. No AI or network inference.

Example: python -m agent.adult.text_stats_cli < safe-input.txt
An isolated temporary store is removed at exit, output is written only to stdout.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sys
import tempfile

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_evaluator import AdultEvaluator
from agent.adult.adult_policies import AdultPolicies
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.adult_text_executor import AdultTextExecutor
from agent.adult.adult_text_input import decode_text_input, encode_text_input
from agent.adult.adult_text_intake import AdultTextIntake
from agent.adult.adult_text_pipeline import AdultTextPipeline
from agent.adult.capability_registry import ModelCapabilityRegistry
from agent.adult.integrations.protocols import TextAdapterResult
from agent.adult.models import CapabilityEvidence, ModelCapabilitySpec
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher


class LocalTextStatsAdapter:
    """Deterministic counting, deliberately not named or certified as an AI model."""
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def invoke(self, request):
        data = self.artifacts.load_input(request.input_payload_id, request.task_id)
        text = decode_text_input(data, 'text.analyze').text
        result = {
            'mode': 'local_text_statistics_not_ai',
            'characters': len(text), 'words_whitespace': len(text.split()),
            'lines': len(text.splitlines()), 'utf8_bytes': len(text.encode('utf-8')),
        }
        return TextAdapterResult(json.dumps(result, sort_keys=True), {}, 0)


def open_stats_executor(root: Path, cipher: PayloadCipher):
    """Reopen only the deterministic counting contract, never production inference."""
    tasks = AdultTaskStore(root / 'tasks.sqlite3')
    artifacts = AdultArtifactStore(root / 'artifacts', cipher=cipher)
    registry = ModelCapabilityRegistry(tasks)
    spec = ModelCapabilitySpec(
        'local-text-statistics', '1', 'text.analyze', ('text',), ('text',), 'local',
    )
    # Reuse the exact existing local contract on process restart. Never silently
    # replace a different or revoked record under the same adapter identity.
    with tasks._connect() as db:
        prior = db.execute("SELECT capability_id FROM adult_model_capabilities "
                           "WHERE adapter_id=? AND adapter_version=? AND task_type=?",
                           (spec.adapter_id, spec.adapter_version, spec.task_type)).fetchone()
    record = registry.get(prior['capability_id']) if prior else registry.register(spec)
    if record.spec != spec or record.verification_status == 'revoked':
        raise RuntimeError('local_capability_conflict')
    # A local, deterministic smoke check establishes only this counting contract.
    sample = encode_text_input('text.analyze', 'one two\nthree')
    ref = artifacts.save_input('stats-smoke', sample, 'application/json', retain_encrypted=False)
    from agent.adult.integrations.protocols import TextRequest
    adapter = LocalTextStatsAdapter(artifacts)
    smoke = json.loads(adapter.invoke(TextRequest('stats-smoke','stats-smoke-step','text.analyze',ref.payload_id)).text)
    if smoke.get('words_whitespace') != 3 or smoke.get('lines') != 2:
        raise RuntimeError('local_smoke_failed')
    artifacts.discard_input(ref.payload_id, 'stats-smoke')
    now = datetime.now(timezone.utc)
    evidence = CapabilityEvidence(
        capability_source='local_adapter_smoke_test', capability_reference='local:counting-smoke-v1',
        verified_at=now.isoformat().replace('+00:00', 'Z'),
        expires_at=(now+timedelta(minutes=10)).isoformat().replace('+00:00', 'Z'),
        adapter_id='local-text-statistics', adapter_version='1', task_type='text.analyze',
        input_modalities=('text',), output_modalities=('text',),
    )
    record = registry.verify(record.capability_id, evidence, record.state_version)
    registry.enable(record.capability_id, record.state_version)
    tracker = AdultAttemptTracker(tasks)
    router = AdultModelRouter(registry, approach_key_builder=tracker.approach_keys,
                             local_adapter_available=lambda value: value == 'local-text-statistics')
    policy = AdultPolicies()
    pipeline = AdultTextPipeline(artifacts, tasks, router, tracker, policy, AdultEvaluator(policy),
                                 {('local-text-statistics', '1'): adapter})
    executor = AdultTextExecutor(tasks, AdultTextIntake(tasks, artifacts, cipher), pipeline, cipher)
    return tasks, artifacts, executor


def run_stats(text: str) -> dict:
    """One real task to an evaluated artifact; no source content in returned stats."""
    with tempfile.TemporaryDirectory(prefix='workspace-text-stats-') as directory:
        root = Path(directory)
        cipher = PayloadCipher(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        tasks, artifacts, executor = open_stats_executor(root, cipher)
        data = encode_text_input('text.analyze', text)
        task = executor.create('text.analyze', data)
        result = executor.run(task.task_id, data, cipher.fingerprint(b'local-text-statistics-v1'))
        if result.status != 'completed' or result.artifact_id is None:
            raise RuntimeError('stats_task_incomplete')
        stats = json.loads(artifacts.read_chunk(result.artifact_id, 0).data)
        return {'task_id': task.task_id, 'status': result.status, 'attempts': tasks.get_task(task.task_id).attempt_count,
                'checkpoint': tasks.get_latest_checkpoint(task.task_id).reason, 'statistics': stats}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Offline local text statistics through the bounded task executor. Not AI inference.')
    parser.parse_args(argv)
    try:
        # Bound the read before decoding or envelope allocation. No input in CLI args/logs.
        raw = sys.stdin.buffer.read(80 * 1024 + 1)
        if len(raw) > 80 * 1024:
            raise ValueError('input_too_large')
        value = run_stats(raw.decode('utf-8', 'strict'))
        print(json.dumps(value, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'status': 'failed', 'error_code': 'local_text_stats_failed'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
