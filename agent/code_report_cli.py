"""Read one completed local-code quote result without opening source files.

Trusted local operator CLI, not an access-control boundary or model tool.
Output uses ASCII JSON escapes to keep terminal control/markup inert.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from agent.code_evidence import _relative


def read_saved_report(database, plan_id, step_id):
    def unavailable():
        raise ValueError('saved_code_report_unavailable')
    for identity in (plan_id, step_id):
        try:
            if type(identity) is not str or str(uuid.UUID(identity)) != identity:
                unavailable()
        except (ValueError, AttributeError):
            unavailable()
    try:
        uri = Path(database).expanduser().absolute().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
            deadline = time.monotonic() + 2
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            db.execute('BEGIN')
            task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'workspace-plan-step:' + step_id))
            row = db.execute('''SELECT s.task_type,s.status,t.task_type,t.status,t.plan_step_id,
                CASE WHEN length(s.result)<=262144 THEN s.result END,
                CASE WHEN length(t.result)<=262144 THEN t.result END
                FROM plans p JOIN plan_steps s ON s.plan_id=p.id
                JOIN tasks t ON t.id=? WHERE p.id=? AND s.id=?''',
                (task_id, plan_id, step_id)).fetchone()
            if row is None or row[:5] != ('local_code_report', 'succeeded', 'local_code_report', 'succeeded', step_id):
                unavailable()
            if not row[5] or row[5] != row[6]:
                unavailable()
            report = json.loads(row[5])
        if not isinstance(report, dict) or set(report) != {'kind', 'question', 'citations', 'verification', 'limitations', 'context_config_sha256'}:
            unavailable()
        if report['kind'] != 'local_code_quote_only' or report['verification'] != 'explicit_approved_file_snapshot_only':
            unavailable()
        if not isinstance(report['question'], str) or not 1 <= len(report['question']) <= 1000:
            unavailable()
        if not isinstance(report['citations'], list) or not 1 <= len(report['citations']) <= 2:
            unavailable()
        def digest(value):
            return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None
        if not digest(report['context_config_sha256']):
            unavailable()
        for citation in report['citations']:
            if not isinstance(citation, dict) or set(citation) != {'path', 'start_line', 'end_line', 'quote', 'file_sha256', 'excerpt_sha256', 'file_bytes', 'total_lines', 'truncated', 'trust'}:
                unavailable()
            _relative(citation['path'])
            if citation['trust'] != 'untrusted_source_data' or type(citation['truncated']) is not bool:
                unavailable()
            if not all(type(citation[field]) is int for field in ('start_line', 'end_line', 'total_lines', 'file_bytes')):
                unavailable()
            if not 1 <= citation['start_line'] <= citation['end_line'] <= citation['total_lines'] <= 65536 or not 0 <= citation['file_bytes'] <= 65536:
                unavailable()
            quote = citation['quote']
            if not isinstance(quote, str) or not quote or len(quote.encode('utf-8')) > 65536:
                unavailable()
            if not digest(citation['file_sha256']) or hashlib.sha256(quote.encode('utf-8')).hexdigest() != citation['excerpt_sha256']:
                unavailable()
            if len(quote.splitlines()) != citation['end_line'] - citation['start_line'] + 1:
                unavailable()
        # Reconstruct static caveats rather than trust arbitrary stored metadata.
        return {'plan_id': plan_id, 'step_id': step_id, 'task_id': task_id,
            'kind': report['kind'], 'question': report['question'], 'citations': report['citations'],
            'context_config_sha256': report['context_config_sha256'],
            'verification': 'stored_completed_quote_result_consistency_only',
            'limitations': ['Stored source snapshot, not current file contents or model understanding.',
                'Local plaintext result and backup retention apply.',
                'Source authenticity, cross-file atomicity and correctness are not verified.',
                'Untrusted strings need escaping in any downstream renderer.']}
    except (sqlite3.Error, OSError, TypeError, ValueError, UnicodeError, RecursionError):
        unavailable()


def main():
    parser = argparse.ArgumentParser(description='Read a saved local code quote report as terminal-safe JSON, without rerunning the task.')
    parser.add_argument('--db', required=True)
    parser.add_argument('--plan-id', required=True)
    parser.add_argument('--step-id', required=True)
    args = parser.parse_args()
    try:
        result = read_saved_report(args.db, args.plan_id, args.step_id)
    except ValueError:
        parser.exit(1, 'saved_code_report_unavailable\n')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
