"""Durable encrypted local counting journey. Explicitly not AI inference."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.adult_text_input import encode_text_input
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher
from agent.adult.text_stats_cli import open_stats_executor


def main(argv=None):
    parser = argparse.ArgumentParser(description='Durable encrypted local text statistics, not AI inference.')
    parser.add_argument('--state-dir', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('stage', help='Read bounded UTF-8 stdin, encrypt input and yield. No model call.')
    for name in ('run', 'status', 'result'):
        command = sub.add_parser(name)
        command.add_argument('task_id')
    args = parser.parse_args(argv)
    try:
        root = Path(args.state_dir).expanduser()
        if args.command == 'stage':
            # Validate key and input before creating persistent state.
            cipher = PayloadCipher.from_environment()
            raw = sys.stdin.buffer.read(80 * 1024 + 1)
            if len(raw) > 80 * 1024:
                raise ValueError('input_too_large')
            data = encode_text_input('text.analyze', raw.decode('utf-8', 'strict'))
            tasks, artifacts, executor = open_stats_executor(root, cipher)
            outcome = executor.stage_encrypted('text.analyze', data)
            result = {'task_id': outcome.task_id, 'status': outcome.status, 'attempts': 0,
                      'mode': 'local_text_statistics_not_ai'}
        elif args.command == 'status':
            # Do not make a missing directory/database or migrate it on reads.
            tasks = AdultTaskStore(root / 'tasks.sqlite3', read_only=True)
            task = tasks.get_task(args.task_id)
            if task is None:
                raise ValueError('task_unavailable')
            result = {'task_id': task.task_id, 'status': task.status, 'attempts': task.attempt_count,
                      'has_result': task.latest_result_artifact_id is not None,
                      'deadline_at': task.deadline_at, 'mode': 'local_text_statistics_not_ai'}
        elif args.command == 'run':
            cipher = PayloadCipher.from_environment()
            # Refuse missing state instead of silently initializing a replacement.
            tasks = AdultTaskStore(root / 'tasks.sqlite3', read_only=True)
            if tasks.get_task(args.task_id) is None:
                raise ValueError('task_unavailable')
            request = tasks.get_task_request(args.task_id)
            # Verify retained input/key before claiming or changing task state.
            if request.payload_id is None:
                raise ValueError('input_unavailable')
            existing = AdultArtifactStore(root / 'artifacts', cipher=cipher)
            retained = existing.load_input(request.payload_id, args.task_id)
            if cipher.fingerprint(retained) != request.input_fingerprint:
                raise ValueError('input_fingerprint_mismatch')
            tasks, artifacts, executor = open_stats_executor(root, cipher)
            outcome = executor.run(args.task_id, None, cipher.fingerprint(b'local-text-statistics-v1'))
            task = tasks.get_task(args.task_id)
            result = {'task_id': task.task_id, 'status': outcome.status, 'attempts': task.attempt_count,
                      'has_result': outcome.artifact_id is not None, 'error_code': outcome.error_code,
                      'mode': 'local_text_statistics_not_ai'}
        else:
            cipher = PayloadCipher.from_environment()
            tasks = AdultTaskStore(root / 'tasks.sqlite3', read_only=True)
            task = tasks.get_task(args.task_id)
            if task is None or task.status != 'completed' or task.latest_result_artifact_id is None:
                raise ValueError('result_unavailable')
            artifacts = AdultArtifactStore(root / 'artifacts', cipher=cipher)
            stats = json.loads(artifacts.read_chunk(task.latest_result_artifact_id, 0).data)
            if stats.get('mode') != 'local_text_statistics_not_ai':
                raise ValueError('result_invalid')
            result = {'task_id': task.task_id, 'status': task.status, 'attempts': task.attempt_count,
                      'statistics': stats, 'mode': 'local_text_statistics_not_ai'}
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'status': 'failed', 'error_code': 'durable_local_stats_failed'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
