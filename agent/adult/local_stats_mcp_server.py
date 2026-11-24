"""Opt-in local counting MCP server. No import-time state or network activity.

This is deliberately not an AI provider or generic Adult execution endpoint.
Start explicitly with python -m agent.adult.local_stats_mcp_server.
It is not registered in the model agent or universal gateway allowlists.
"""
from __future__ import annotations

import json
from functools import wraps
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.adult_text_input import encode_text_input
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher
from agent.adult.text_stats_cli import open_stats_executor

mcp = FastMCP('workspace-durable-local-statistics-not-ai')


def _safe_tool(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            raise ValueError('local_stats_operation_failed') from None
    return call


def _root():
    configured = os.environ.get('LOCAL_STATS_STATE_DIR', '').strip()
    if not configured:
        raise ValueError('local_stats_state_not_configured')
    return Path(configured).expanduser()


def _status(task):
    if task is None:
        raise ValueError('local_stats_task_unavailable')
    return {'task_id': task.task_id, 'status': task.status, 'attempts': task.attempt_count,
            'has_result': task.latest_result_artifact_id is not None,
            'deadline_at': task.deadline_at, 'mode': 'local_text_statistics_not_ai'}


@mcp.tool()
@_safe_tool
def local_stats_stage(text: str) -> dict:
    """Explicitly encrypt bounded input and yield a local counting task, no AI call."""
    cipher = PayloadCipher.from_environment()
    data = encode_text_input('text.analyze', text)
    tasks, _, executor = open_stats_executor(_root(), cipher)
    staged = executor.stage_encrypted('text.analyze', data)
    return _status(tasks.get_task(staged.task_id))


@mcp.tool()
@_safe_tool
def local_stats_get_task(task_id: str) -> dict:
    """Read safe local task metadata only, without source text or stored secrets."""
    tasks = AdultTaskStore(_root() / 'tasks.sqlite3', read_only=True)
    return _status(tasks.get_task(task_id))


@mcp.tool()
@_safe_tool
def local_stats_run(task_id: str) -> dict:
    """Explicitly run one staged deterministic counting task; never AI or network."""
    root = _root(); cipher = PayloadCipher.from_environment()
    tasks = AdultTaskStore(root / 'tasks.sqlite3', read_only=True)
    task = tasks.get_task(task_id)
    _status(task)
    if task.status in {'completed', 'failed', 'cancelled'}:
        return _status(task)
    request = tasks.get_task_request(task_id)
    if request.payload_id is None:
        raise ValueError('local_stats_input_unavailable')
    artifacts = AdultArtifactStore(root / 'artifacts', cipher=cipher)
    raw = artifacts.load_input(request.payload_id, task_id)
    if cipher.fingerprint(raw) != request.input_fingerprint:
        raise ValueError('local_stats_input_mismatch')
    tasks, _, executor = open_stats_executor(root, cipher)
    executor.run(task_id, None, cipher.fingerprint(b'local-text-statistics-v1'))
    return _status(tasks.get_task(task_id))


@mcp.tool()
@_safe_tool
def local_stats_result(task_id: str) -> dict:
    """Read the counting JSON of a completed task, never its original input text."""
    root = _root(); cipher = PayloadCipher.from_environment()
    tasks = AdultTaskStore(root / 'tasks.sqlite3', read_only=True)
    task = tasks.get_task(task_id)
    status = _status(task)
    if task.status != 'completed' or task.latest_result_artifact_id is None:
        raise ValueError('local_stats_result_unavailable')
    artifacts = AdultArtifactStore(root / 'artifacts', cipher=cipher)
    statistics = json.loads(artifacts.read_chunk(task.latest_result_artifact_id, 0).data)
    if statistics.get('mode') != 'local_text_statistics_not_ai':
        raise ValueError('local_stats_result_invalid')
    return {**status, 'statistics': statistics}


@mcp.tool()
@_safe_tool
def local_stats_cancel(task_id: str) -> dict:
    """Explicitly cancel a local counting task without making any model call."""
    root = _root()
    # Refuse missing state, then use the existing cancellation transaction.
    readonly = AdultTaskStore(root / 'tasks.sqlite3', read_only=True)
    _status(readonly.get_task(task_id))
    tasks = AdultTaskStore(root / 'tasks.sqlite3')
    return _status(tasks.request_cancel(task_id))


if __name__ == '__main__':
    mcp.run(transport='stdio')
