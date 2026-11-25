"""Bounded read-only retained ciphertext audit. No decryption, export or cleanup."""
import hashlib
import hmac
import sqlite3
import time
from contextlib import closing

from agent.adult.payload_store import (
    AdultArtifactStore, PAYLOAD_KEY_VERSION, PAYLOAD_SCHEMA_VERSION,
    PayloadStoreError, PayloadUnavailable, _parse_utc,
)


_CODES = ('expired', 'missing', 'unsafe_file', 'ciphertext_mismatch',
          'unsupported_version',
          'invalid_metadata', 'ciphertext_verified_not_decrypted')


def _audit_retained(store, *, limit=100):
    """Inspect up to100existing retained records, with counts only in the result.

    A ciphertext hash match is NOT AEAD authentication or proof of recoverability.
    No plaintext/key bytes/paths/IDs returned. No expiry cleanup or TTL extension.
    Only indexes that already exist can be read, no storage initialization here.
    """
    if not isinstance(store, AdultArtifactStore):
        raise TypeError('artifact_store_required')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('audit_limit_invalid')
    deadline = time.monotonic() + 5
    counts = {code: 0 for code in _CODES}
    key_checks = {'key_unavailable': 0, 'key_mismatch': 0, 'key_matches_metadata': 0}
    inspected = 0
    bytes_checked = 0
    complete = True
    with store._lock:
        if store._index_path.is_symlink() or store.root.is_symlink():
            raise ValueError('audit_storage_unsafe')
        with closing(sqlite3.connect(store._index_path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA trusted_schema=OFF')
            records = db.execute('SELECT * FROM adult_payload_records ORDER BY payload_id LIMIT ?', (limit + 1,)).fetchall()
        if len(records) > limit:
            complete = False
        for row in records[:limit]:
            if time.monotonic() > deadline:
                complete = False
                break
            inspected += 1
            record = dict(row)
            try:
                if record['kind'] not in {'input', 'output'} or type(record['plaintext_size']) is not int or record['plaintext_size'] < 0:
                    counts['invalid_metadata'] += 1
                    continue
                if _parse_utc(record['expires_at']) <= store._now():
                    counts['expired'] += 1
                    continue
                if record['key_version'] != PAYLOAD_KEY_VERSION or record['schema_version'] != PAYLOAD_SCHEMA_VERSION:
                    counts['unsupported_version'] += 1
                    continue
                maximum = store.max_output_bytes if record['kind'] == 'output' else store.max_input_bytes
                if record['plaintext_size'] > maximum:
                    counts['invalid_metadata'] += 1
                    continue
                if bytes_checked + record['plaintext_size'] + 65 > 64 * 1024 * 1024:
                    inspected -= 1
                    complete = False
                    break
                if store.cipher is None:
                    key_checks['key_unavailable'] += 1
                elif not hmac.compare_digest(str(record['key_id']), store.cipher.key_id):
                    key_checks['key_mismatch'] += 1
                else:
                    key_checks['key_matches_metadata'] += 1
                data = store._read_encrypted_file(record['payload_id'], maximum)
                bytes_checked += len(data)
                if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), str(record['ciphertext_sha256'])):
                    counts['ciphertext_mismatch'] += 1
                else:
                    counts['ciphertext_verified_not_decrypted'] += 1
            except PayloadUnavailable:
                counts['missing'] += 1
            except PayloadStoreError:
                counts['unsafe_file'] += 1
            except (TypeError, ValueError, OverflowError):
                counts['invalid_metadata'] += 1
    return {'status': 'inspected', 'inspected': inspected, 'complete': complete, 'counts': counts,
            'key_checks': key_checks,
            'plaintext_authenticated': False, 'recoverability_proven': False,
            'data_modified': False, 'transient_payloads_included': False}


def audit_retained(store, *, limit=100):
    """Count-only audit with separate exclusive file and key-check dimensions.

    Index read errors are a stable unavailable result, never a raw path-bearing
    exception. Configuration/type validation remains an explicit caller error.
    """
    try:
        return _audit_retained(store, limit=limit)
    except (sqlite3.Error, OSError):
        return {'status': 'index_unavailable', 'inspected': 0, 'complete': False,
                'counts': {code: 0 for code in _CODES},
                'key_checks': {'key_unavailable': 0, 'key_mismatch': 0, 'key_matches_metadata': 0},
                'plaintext_authenticated': False, 'recoverability_proven': False,
                'data_modified': False, 'transient_payloads_included': False}
