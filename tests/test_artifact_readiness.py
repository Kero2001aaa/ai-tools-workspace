"""Retained ciphertext audit is read-only, bounded and never decrypts."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent.adult.artifact_readiness import audit_retained
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher


class ArtifactReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'artifacts'
        self.now = datetime(2026, 11, 25, tzinfo=timezone.utc)
        self.cipher = PayloadCipher(b'A' * 32)
        self.store = AdultArtifactStore(self.root, cipher=self.cipher, clock=lambda: self.now)

    def output(self):
        return self.store.save_output('task-private-id', 'attempt-private-id', b'private text', 'text/plain')

    def test_hash_verification_no_decryption_no_ids_no_mutation(self):
        ref = self.output()
        before = {file.name: file.read_bytes() for file in self.root.iterdir()}
        with patch.object(self.cipher, 'decrypt', side_effect=AssertionError('no decryption')):
            result = audit_retained(self.store)
        self.assertEqual(1, result['counts']['ciphertext_verified_not_decrypted'])
        for field in ('private text', 'task-private-id', ref.artifact_id, str(self.root)):
            self.assertNotIn(field, json.dumps(result))
        self.assertFalse(result['recoverability_proven'])
        self.assertEqual(before, {file.name: file.read_bytes() for file in self.root.iterdir()})

    def test_missing_and_tampered_files_counted_without_content(self):
        first = self.output()
        second = self.output()
        self.store._payload_path(first.artifact_id).unlink()
        self.store._payload_path(second.artifact_id).write_bytes(b'tampered')
        result = audit_retained(self.store)
        self.assertEqual(1, result['counts']['missing'])
        self.assertEqual(1, result['counts']['ciphertext_mismatch'])

    def test_expired_not_read_or_deleted(self):
        ref = self.output()
        self.now += timedelta(days=2)
        with patch.object(self.store, '_read_encrypted_file', side_effect=AssertionError('expired must not read')):
            result = audit_retained(self.store)
        self.assertEqual(1, result['counts']['expired'])
        self.assertTrue(self.store._payload_path(ref.artifact_id).exists())

    def test_absent_and_wrong_keys_are_not_reported_as_recoverable(self):
        self.output()
        self.store.cipher = None
        result = audit_retained(self.store)
        self.assertEqual(1, result['key_checks']['key_unavailable'])
        self.assertFalse(result['plaintext_authenticated'])
        self.store.cipher = PayloadCipher(b'B' * 32)
        self.assertEqual(1, audit_retained(self.store)['key_checks']['key_mismatch'])

    def test_symlink_file_not_followed(self):
        ref = self.output()
        file = self.store._payload_path(ref.artifact_id)
        file.unlink()
        target = Path(self.temp.name) / 'outside'
        target.write_bytes(b'do not read')
        file.symlink_to(target)
        self.assertEqual(1, audit_retained(self.store)['counts']['unsafe_file'])

    def test_limit_and_ephemeral_exclusion(self):
        self.output()
        self.output()
        result = audit_retained(self.store, limit=1)
        self.assertEqual(1, result['inspected'])
        self.assertFalse(result['complete'])
        transient = AdultArtifactStore(Path(self.temp.name) / 'transient', clock=lambda: self.now)
        transient.save_output('task', 'attempt', b'ephemeral', 'text/plain')
        self.assertEqual(0, audit_retained(transient)['inspected'])
        for limit in (True, 0, 101, '10'):
            with self.assertRaises(ValueError):
                audit_retained(self.store, limit=limit)

    def test_file_and_key_counts_are_separate_exclusive_dimensions(self):
        self.output()
        self.store.cipher = None
        result = audit_retained(self.store)
        self.assertEqual(1, sum(result['counts'].values()))
        self.assertEqual(1, sum(result['key_checks'].values()))
        self.assertEqual(1, result['counts']['ciphertext_verified_not_decrypted'])
        self.assertEqual(1, result['key_checks']['key_unavailable'])

    def test_unreadable_index_stable_unavailable(self):
        with patch('agent.adult.artifact_readiness.sqlite3.connect', side_effect=OSError('private path marker')):
            result = audit_retained(self.store)
        self.assertEqual('index_unavailable', result['status'])
        self.assertNotIn('private path marker', json.dumps(result))

    def test_missing_malformed_or_empty_index_stable_unavailable(self):
        index = self.store._index_path
        for kind in ('missing', 'malformed', 'empty'):
            if index.exists():
                index.unlink()
            if kind == 'malformed':
                index.write_bytes(b'not sqlite private marker')
            elif kind == 'empty':
                index.touch()
            result = audit_retained(self.store)
            self.assertEqual('index_unavailable', result['status'])
            self.assertFalse(result['complete'])
            self.assertNotIn(str(self.root), json.dumps(result))
            self.assertNotIn('private marker', json.dumps(result))
            if kind == 'missing':
                self.assertFalse(index.exists())


if __name__ == '__main__':
    unittest.main()
