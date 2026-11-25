"""Offline project-only acceptance run, retaining one approved utility-file quote.

Writes new private acceptance databases/report under an explicit empty directory.
No overwrite, source changes, model, provider or network calls.
"""
import argparse
import json
import os
from pathlib import Path
import stat
from agent.code_task import configured_context
from agent.code_report_cli import read_saved_report
from agent.plan_recovery import copy_database
from agent.plan_store import PlanStore


def run_journey(destination):
    destination = Path(destination).absolute()
    if destination.exists():
        raise ValueError('journey_destination_exists')
    repository = Path(__file__).resolve().parents[1]
    relative = 'agent/mcp_server.py'
    # Fixed project utility source, never a caller-supplied file or filesystem root.
    text = (repository / relative).read_text(encoding='utf-8')
    line = next(i for i, value in enumerate(text.splitlines(), 1) if value.startswith('def text_stats('))
    destination.mkdir(mode=0o700)
    old = os.environ.get('WORKSPACE_CODE_CONTEXT')
    try:
        os.environ['WORKSPACE_CODE_CONTEXT'] = json.dumps({'root': str(repository), 'approved_files': [relative]})
        context_hash = configured_context()[1]
        store = PlanStore(destination / 'plans.sqlite3')
        os.chmod(store.db_path, stat.S_IRUSR | stat.S_IWUSR)
        plan = store.create_plan('Offline known-code quote journey', [
            {'task_type': 'local_code_report', 'payload': {
                'question': 'Which function returns text statistics in the approved MCP utility file?',
                'selections': [{'path': relative, 'start_line': line, 'max_lines': 7}],
                'config_sha256': context_hash}},
            {'task_type': 'workspace_status'}])
        first = store.run_next_step(plan['id'])
        if first['status'] != 'step_succeeded':
            raise ValueError('journey_step_failed')
        before = store.get_plan(plan['id'])
        copy_database(store.db_path, destination / 'restored.sqlite3')
        os.environ.pop('WORKSPACE_CODE_CONTEXT', None)
        report = read_saved_report(destination / 'restored.sqlite3', plan['id'], plan['steps'][0]['id'])
        restored = PlanStore(destination / 'restored.sqlite3')
        finished = restored.run_until_done(plan['id'])
        same_step = before['steps'][0] == restored.get_plan(plan['id'])['steps'][0]
        attempts = restored.queue.get(first['task_id'])['attempts']
        if finished['status'] != 'completed' or not same_step or attempts != 1:
            raise ValueError('journey_resume_failed')
        outcome = {'kind': 'offline_local_quote_acceptance', 'plan_id': plan['id'],
            'step_id': plan['steps'][0]['id'], 'task_id': first['task_id'],
            'stored_report_retrieved': True, 'restored_plan_completed': True,
            'completed_step_unchanged': same_step, 'completed_task_attempts': attempts,
            'model_called': False, 'provider_called': False, 'stage_accepted': False,
            'report': report}
        target = destination / 'report.json'
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            json.dump(outcome, output, ensure_ascii=True, indent=2)
        return outcome
    finally:
        if old is None:
            os.environ.pop('WORKSPACE_CODE_CONTEXT', None)
        else:
            os.environ['WORKSPACE_CODE_CONTEXT'] = old


def main():
    parser = argparse.ArgumentParser(description='Run project-only offline quote report acceptance into a NEW private directory. Retains approved source excerpt in local plaintext DB/backups.')
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        result = run_journey(args.output_dir)
    except (ValueError, OSError, StopIteration):
        parser.exit(1, 'local_code_journey_failed\n')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
