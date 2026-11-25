"""Private current-schema memory backup/restore, preserving fixed ACLs and audit.

Operator-only whole-database recovery. Not an agent export API, not a namespace
sharing operation, not connected to MCP or HTTP. Never emits stored content.
"""
import argparse
import json
import sqlite3
import time
from agent.plan_recovery import _copy_database
from agent.memory_store import _validate_current_schema, _decode_row

TABLES = {'memories', 'memory_schema_migrations', 'memory_namespace_acl', 'memory_access_audit'}


def _validate(db, deadline):
    db.row_factory = sqlite3.Row
    if [row[0] for row in db.execute('PRAGMA integrity_check')] != ['ok']:
        raise ValueError('Memory integrity check failed')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Memory references are invalid')
    if any(row[0] not in {'table', 'index'} for row in db.execute("SELECT type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")):
        raise ValueError('Memory snapshots cannot contain views or triggers')
    _validate_current_schema(db)
    expected = {
        'memories': {'id', 'content', 'tags', 'created_at', 'namespace', 'memory_type', 'visibility', 'sensitivity', 'task_id', 'content_hash', 'metadata_json', 'owner_agent'},
        'memory_schema_migrations': {'version', 'name', 'applied_at'},
        'memory_namespace_acl': {'principal', 'namespace', 'can_read', 'can_write', 'can_read_shareable'},
        'memory_access_audit': {'id', 'principal', 'operation', 'namespace', 'memory_id', 'created_at'},
    }
    for table, columns in expected.items():
        if {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')} != columns:
            raise ValueError('Unsupported memory snapshot columns')
    if [row[0] for row in db.execute('SELECT version FROM memory_schema_migrations ORDER BY version')] != [1]:
        raise ValueError('Unsupported memory migration version')
    for row in db.execute('SELECT * FROM memories'):
        if time.monotonic() > deadline:
            raise ValueError('Memory validation time limit exceeded')
        _decode_row(row)
    return {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in sorted(TABLES)}


def copy_memory_database(source, destination):
    """Copy a trusted whole memory DB to a new private path without live overwrite.

    Both namespaces and audit records are preserved, never merged or published.
    Receipts contain counts/digest only. Callers must protect the snapshot as
    sensitive, since file-level recovery necessarily contains private memories.
    """
    return _copy_database(source, destination, tables=TABLES, validate=_validate)


def main():
    parser = argparse.ArgumentParser(description='Operator-only whole memory database backup/restore to a NEW private path')
    parser.add_argument('operation', choices=('backup', 'restore'))
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    print(json.dumps(copy_memory_database(args.source, args.destination), sort_keys=True))


if __name__ == '__main__':
    main()
