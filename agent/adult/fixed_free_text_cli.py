"""Explicit fixed-free operator CLI. No discovery, certification or background work."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.adult_text_input import encode_text_input, _unique_object, _reject_constant
from agent.adult.fixed_free_text_service import FixedFreeTextService
from agent.adult.fixed_free_text_application import FixedFreeTextApplication
from agent.adult.integrations.omniroute_chat import AdultOmniRouteConfig
from agent.adult.models import TaskBudget
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher


def _status(task):
    if task is None or task.task_type not in {'text.analyze', 'text.generate', 'text.rewrite'}:
        raise ValueError('text_task_unavailable')
    return {'task_id': task.task_id, 'status': task.status, 'attempts': task.attempt_count,
            'provider_calls': task.provider_calls, 'deadline_at': task.deadline_at,
            'has_result': task.latest_result_artifact_id is not None,
            'mode': 'fixed_free_text', 'user_action_required': task.user_action_required}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit fixed-free text task operator. No automatic route certification.')
    parser.add_argument('--state-dir', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    stage = sub.add_parser('stage', help='Encrypt UTF-8 JSON stdin containing text and optional instruction. Zero calls.')
    stage.add_argument('--task-type', choices=('text.analyze', 'text.generate', 'text.rewrite'), required=True)
    stage.add_argument('--provider-call-budget', type=int, required=True, choices=range(1, 21))
    for name in ('run', 'status', 'result', 'cancel'):
        command = sub.add_parser(name)
        command.add_argument('task_id')
        if name == 'result':
            command.add_argument('--offset', type=int, default=0)
            command.add_argument('--max-bytes', type=int, default=262144)
    args = parser.parse_args(argv)
    try:
        root = Path(args.state_dir).expanduser()
        if args.command == 'stage':
            # All input/config validation precedes persistent state creation.
            cipher = PayloadCipher.from_environment()
            config = AdultOmniRouteConfig.from_env()
            if config is None or config.base_url.rstrip('/') != 'https://openrouter.ai/api/v1':
                raise ValueError('fixed_free_config_required')
            raw = sys.stdin.buffer.read(160 * 1024 + 1)
            if len(raw) > 160 * 1024:
                raise ValueError('input_too_large')
            envelope = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if not isinstance(envelope, dict) or not set(envelope) <= {'text', 'instruction'}:
                raise ValueError('input_invalid')
            text, instruction = envelope.get('text'), envelope.get('instruction', '')
            encode_text_input(args.task_type, text, instruction)
            budget = TaskBudget(max_provider_calls=args.provider_call_budget)
            tasks = AdultTaskStore(root / 'tasks.sqlite3')
            artifacts = AdultArtifactStore(root / 'artifacts', cipher=cipher)
            service = FixedFreeTextService(tasks, artifacts, cipher, config)
            application = FixedFreeTextApplication(service, budget=budget)
            # The stage command explicitly requests encrypted durable retention.
            staged = application.submit_text_task(args.task_type, text, instruction, retain_payload=True)
            result = _status(tasks.get_task(staged['task_id']))
        else:
            # A read must not create/migrate a replacement database.
            tasks = AdultTaskStore(root / 'tasks.sqlite3', read_only=True)
            task = tasks.get_task(args.task_id)
            result = _status(task)
            if args.command == 'cancel':
                result = _status(AdultTaskStore(root / 'tasks.sqlite3').request_cancel(args.task_id))
            elif args.command == 'run':
                cipher = PayloadCipher.from_environment()
                config = AdultOmniRouteConfig.from_env()
                if config is None:
                    raise ValueError('fixed_free_config_required')
                artifacts = AdultArtifactStore(root / 'artifacts', cipher=cipher)
                # Validate configuration and retained input before opening writable task store.
                service = FixedFreeTextService(tasks, artifacts, cipher, config)
                if task.status not in {'completed', 'failed', 'cancelled'}:
                    request = tasks.get_task_request(args.task_id)
                    data = artifacts.load_input(request.payload_id, args.task_id)
                    if cipher.fingerprint(data) != request.input_fingerprint:
                        raise ValueError('input_key_mismatch')
                service = FixedFreeTextService(AdultTaskStore(root / 'tasks.sqlite3'), artifacts, cipher, config)
                application = FixedFreeTextApplication(service, budget=TaskBudget(
                    max_provider_calls=task.max_provider_calls))
                outcome = application.run_task(args.task_id)
                result = _status(service.tasks.get_task(args.task_id))
                result['error_code'] = outcome['error_code']
            elif args.command == 'result':
                if task.status != 'completed' or task.latest_result_artifact_id is None:
                    raise ValueError('result_unavailable')
                cipher = PayloadCipher.from_environment()
                artifacts = AdultArtifactStore(root / 'artifacts', cipher=cipher)
                from agent.adult.fixed_free_text_application import task_result_chunk
                chunk = task_result_chunk(task, artifacts, args.offset, args.max_bytes)
                result['artifact'] = {key:value for key,value in chunk.items() if key != 'task_id'}
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'status': 'failed', 'error_code': 'fixed_free_text_operation_failed'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
