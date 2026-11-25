"""Explicitly configured local-code report task. No default filesystem grant."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
import tempfile
from agent.code_evidence import _relative


def validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {'question', 'selections', 'config_sha256'}:
        raise ValueError('code_task_payload_invalid')
    if not isinstance(payload['question'], str) or not 1 <= len(payload['question'].strip()) <= 1000:
        raise ValueError('code_task_payload_invalid')
    if not isinstance(payload['config_sha256'], str) or len(payload['config_sha256']) != 64 or any(char not in '0123456789abcdef' for char in payload['config_sha256']):
        raise ValueError('code_task_payload_invalid')
    selections = payload['selections']
    if not isinstance(selections, list) or not 1 <= len(selections) <= 2:
        raise ValueError('code_task_payload_invalid')
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {'path', 'start_line', 'max_lines'}:
            raise ValueError('code_task_payload_invalid')
        _relative(selection['path'])
        if type(selection['start_line']) is not int or not 1 <= selection['start_line'] <= 65537 or type(selection['max_lines']) is not int or not 1 <= selection['max_lines'] <= 50:
            raise ValueError('code_task_payload_invalid')
    try:
        payload['question'].encode('utf-8')
    except UnicodeError:
        raise ValueError('code_task_payload_invalid') from None


def configured_context():
    raw = os.environ.get('WORKSPACE_CODE_CONTEXT', '')
    if not raw or len(raw) > 8192:
        raise ValueError('code_task_not_configured')
    try:
        config = json.loads(raw)
        if not isinstance(config, dict) or set(config) != {'root', 'approved_files'}:
            raise ValueError()
        if not isinstance(config['root'], str) or not Path(config['root']).is_absolute():
            raise ValueError()
        approved = config['approved_files']
        if not isinstance(approved, list) or not 1 <= len(approved) <= 20:
            raise ValueError()
        for path in approved:
            _relative(path)
        digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('code_task_config_invalid') from None
    return config, digest


def _close_input(file):
    if file is not None:
        try:
            file.close()
        except OSError:
            pass


def execute_code_report(payload):
    validate_payload(payload)
    config, digest = configured_context()
    if payload['config_sha256'] != digest:
        raise ValueError('code_task_config_changed')
    if any(selection['path'] not in config['approved_files'] for selection in payload['selections']):
        raise ValueError('code_source_not_approved')
    # Source reads happen only in this bounded child. No source code is executed.
    context = {**config, 'question': payload['question'], 'selections': payload['selections']}
    program = 'import json,sys;from agent.code_report import build_code_quote_report;print(json.dumps(build_code_quote_report(**json.load(sys.stdin))))'
    package = str(Path(__file__).resolve().parents[1])
    request = json.dumps(context, ensure_ascii=False).encode('utf-8')
    if len(request) > 32768:
        raise ValueError('code_task_config_invalid')
    input_file = None
    try:
        input_file = tempfile.TemporaryFile()
        input_file.write(request)
        input_file.seek(0)
    except OSError:
        if input_file is not None:
            _close_input(input_file)
        raise ValueError('code_task_read_failed') from None
    try:
        process = subprocess.Popen([sys.executable, '-c', program], stdin=input_file,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=package,
            env={'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': package, 'PYTHONIOENCODING': 'utf-8'})
    except OSError:
        _close_input(input_file)
        raise ValueError('code_task_read_failed') from None
    output = bytearray();deadline = time.monotonic() + 5
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('code_task_timeout')
                events = selector.select(remaining)
                if not events:
                    raise ValueError('code_task_timeout')
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 131072:
                    raise ValueError('code_task_result_too_large')
        if process.wait(timeout=max(0.01, deadline-time.monotonic())) != 0:
            raise ValueError('code_task_read_failed')
        result = json.loads(output)
        if not isinstance(result, dict) or result.get('kind') != 'local_code_quote_only':
            raise ValueError('code_task_result_invalid')
        result['context_config_sha256'] = digest
        return result
    except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError):
        raise ValueError('code_task_read_failed') from None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        _close_input(input_file)
