"""General read-only task attempt evidence. Hashes are identity, not secrecy."""
import hashlib
import json
import sqlite3

COLUMNS = {'task_id','attempt_no','plan_step_id','method_fingerprint','input_fingerprint',
           'claim_stamp','started_at','finished_at','status','failure_class','retryable',
           'error_code','result_sha256','retry_reason'}


class RetryBlockedError(ValueError):
    """A nonretryable attempt needs an explicit, unverified repair assertion."""


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS task_attempts (
        task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, plan_step_id TEXT NOT NULL,
        method_fingerprint TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
        claim_stamp TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        status TEXT NOT NULL, failure_class TEXT, retryable INTEGER NOT NULL DEFAULT 0,
        error_code TEXT, result_sha256 TEXT, retry_reason TEXT,
        PRIMARY KEY(task_id,attempt_no))''')


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def fingerprints(task_type, payload):
    # Explicit method revision. This does not fingerprint the source files read,
    # current memory contents, credentials, or the machine environment.
    return digest('workspace-readonly-v1:' + task_type), digest(json.dumps(
        payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True))


def classify(error):
    if isinstance(error, PermissionError):
        return 'permission', False, 'PERMISSION_DENIED'
    if isinstance(error, ValueError) and str(error) in {
            'code_task_not_configured','code_task_config_invalid',
            'code_task_config_changed','code_source_not_approved'}:
        return 'permission', False, 'SOURCE_SCOPE_UNAVAILABLE'
    if isinstance(error, (TimeoutError, ConnectionError)):
        return 'transient', True, 'TRANSIENT_IO'
    if isinstance(error, sqlite3.OperationalError):
        return 'storage', False, 'STORAGE_REPAIR_REQUIRED'
    if isinstance(error, (ValueError, TypeError, FileNotFoundError, UnicodeError)):
        return 'input_or_state', False, 'INPUT_OR_STATE_REPAIR_REQUIRED'
    return 'unknown', False, 'UNCLASSIFIED_REPAIR_REQUIRED'
