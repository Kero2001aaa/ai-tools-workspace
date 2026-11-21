"""Security and lifecycle tests for Adult Stage 4 encrypted payload storage."""
import base64
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.adult.payload_store import (
    AdultArtifactStore,
    PayloadAuthenticationError,
    PayloadCipher,
    PayloadKeyInvalid,
    PayloadKeyMismatch,
    PayloadKeyUnavailable,
    PayloadNotFound,
    PayloadStorageError,
    PayloadTooLarge,
    PayloadUnavailable,
    TransientPayloadCache,
)


KEY_ONE = base64.urlsafe_b64encode(b"A" * 32).decode("ascii")
KEY_TWO = base64.urlsafe_b64encode(b"B" * 32).decode("ascii")
STAMP = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)


class PayloadCipherTests(unittest.TestCase):
    def setUp(self):
        self.cipher = PayloadCipher(KEY_ONE)

    def test_aes_gcm_round_trip_random_nonce_and_key_separation(self):
        payload_id = "a" * 32
        first = self.cipher.encrypt("task-1", payload_id, b"private payload")
        second = self.cipher.encrypt("task-1", payload_id, b"private payload")
        self.assertEqual(b"private payload", self.cipher.decrypt("task-1", payload_id, first.ciphertext))
        self.assertNotEqual(first.ciphertext, second.ciphertext)
        self.assertNotEqual(self.cipher._encryption_key, self.cipher._fingerprint_key)
        self.assertTrue(self.cipher.fingerprint(b"private payload").startswith("hmac-sha256:"))
        self.assertNotIn(b"private payload", first.ciphertext)

    def test_authenticated_data_binds_task_and_payload_ids(self):
        payload_id = "b" * 32
        encrypted = self.cipher.encrypt("task-1", payload_id, b"bound data")
        with self.assertRaises(PayloadAuthenticationError):
            self.cipher.decrypt("task-2", payload_id, encrypted.ciphertext)
        with self.assertRaises(PayloadAuthenticationError):
            self.cipher.decrypt("task-1", "c" * 32, encrypted.ciphertext)

    def test_cipher_rejects_tampering_and_wrong_key(self):
        payload_id = "d" * 32
        encrypted = self.cipher.encrypt("task-1", payload_id, b"authenticated")
        tampered = encrypted.ciphertext[:-1] + bytes((encrypted.ciphertext[-1] ^ 1,))
        with self.assertRaises(PayloadAuthenticationError):
            self.cipher.decrypt("task-1", payload_id, tampered)
        with self.assertRaises(PayloadKeyMismatch):
            PayloadCipher(KEY_TWO).decrypt("task-1", payload_id, encrypted.ciphertext)

    def test_invalid_or_missing_master_key_fails_closed(self):
        with self.assertRaises(PayloadKeyInvalid):
            PayloadCipher("not-base64")
        with self.assertRaises(PayloadKeyInvalid):
            PayloadCipher(b"short")
        with self.assertRaises(PayloadKeyUnavailable):
            PayloadCipher.from_environment({})


class AdultArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "payloads"
        self.now = [STAMP]
        self.cipher = PayloadCipher(KEY_ONE)
        self.store = AdultArtifactStore(
            self.root, cipher=self.cipher, clock=lambda: self.now[0]
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_retained_input_is_encrypted_and_metadata_contains_no_plaintext_or_key(self):
        secret = b"distinctive-private-input-9b3d2e"
        reference = self.store.save_input("task-1", secret, "text/plain", retain_encrypted=True)
        self.assertTrue(reference.retained)
        encrypted_path = self.root / (reference.payload_id + ".aap")
        encrypted = encrypted_path.read_bytes()
        database = self.store._index_path.read_bytes()
        self.assertNotIn(secret, encrypted)
        self.assertNotIn(secret, database)
        self.assertNotIn(KEY_ONE.encode(), database)
        self.assertEqual(secret, self.store.load_input(reference.payload_id, "task-1"))
        with sqlite3.connect(self.store._index_path) as connection:
            row = connection.execute(
                "SELECT key_id,key_version,schema_version,kind FROM adult_payload_records WHERE payload_id=?",
                (reference.payload_id,),
            ).fetchone()
        self.assertEqual((self.cipher.key_id, 1, 1, "input"), row)

    def test_wrong_task_cannot_load_retained_input(self):
        reference = self.store.save_input("task-1", b"secret", "text/plain", retain_encrypted=True)
        with self.assertRaises(PayloadNotFound):
            self.store.load_input(reference.payload_id, "task-2")

    def test_wrong_key_cannot_decrypt_after_store_restart(self):
        reference = self.store.save_input("task-1", b"restart-safe secret", "text/plain", retain_encrypted=True)
        restarted = AdultArtifactStore(self.root, cipher=PayloadCipher(KEY_TWO))
        with self.assertRaises(PayloadKeyMismatch):
            restarted.load_input(reference.payload_id, "task-1")

    def test_file_tampering_is_detected_before_plaintext_is_returned(self):
        reference = self.store.save_input("task-1", b"tamper target", "text/plain", retain_encrypted=True)
        path = self.root / (reference.payload_id + ".aap")
        content = path.read_bytes()
        path.write_bytes(content[:-1] + bytes((content[-1] ^ 1,)))
        with self.assertRaises(PayloadAuthenticationError):
            self.store.load_input(reference.payload_id, "task-1")

    def test_missing_key_rejects_retained_input_before_any_file_write(self):
        store = AdultArtifactStore(self.root / "no-key")
        with self.assertRaises(PayloadKeyUnavailable):
            store.save_input("task-1", b"must not persist", "text/plain", retain_encrypted=True)
        self.assertEqual([], list(store.root.glob("*.aap")))
        with sqlite3.connect(store._index_path) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM adult_payload_records").fetchone()[0])

    def test_missing_key_uses_bounded_memory_only_output_fallback(self):
        root = self.root / "memory-only"
        store = AdultArtifactStore(root)
        output = store.save_output("task-1", "attempt-1", b"generated bytes", "image/png")
        self.assertFalse(output.persisted)
        chunk = store.read_chunk(output.artifact_id, 0, 8)
        self.assertEqual(b"generate", chunk.data)
        self.assertEqual("image/png", chunk.mime_type)
        self.assertFalse(chunk.eof)
        restarted = AdultArtifactStore(root)
        with self.assertRaises(PayloadNotFound):
            restarted.read_chunk(output.artifact_id, 0)

    def test_ephemeral_input_is_process_local_and_missing_after_restart(self):
        reference = self.store.save_input("task-1", b"ephemeral input", "text/plain", retain_encrypted=False)
        self.assertFalse(reference.retained)
        self.assertEqual(b"ephemeral input", self.store.load_input(reference.payload_id, "task-1"))
        restarted = AdultArtifactStore(self.root, cipher=self.cipher)
        with self.assertRaises(PayloadUnavailable):
            restarted.load_input(reference.payload_id, "task-1")

    def test_atomic_write_failure_leaves_no_partial_or_metadata_record(self):
        with patch("agent.adult.payload_store.os.replace", side_effect=OSError("simulated")):
            with self.assertRaises(PayloadStorageError):
                self.store.save_input("task-1", b"must not be partially written", "text/plain", retain_encrypted=True)
        self.assertEqual([], list(self.root.glob("*.aap")))
        self.assertEqual([], list(self.root.glob("*.partial")))
        with sqlite3.connect(self.store._index_path) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM adult_payload_records").fetchone()[0])

    def test_path_traversal_references_are_rejected(self):
        with self.assertRaises(Exception):
            self.store.load_input("../../outside", "task-1")
        with self.assertRaises(Exception):
            self.store.read_chunk("../outside", 0)

    def test_output_artifact_is_encrypted_and_chunked(self):
        raw = b"0123456789abcdef"
        artifact = self.store.save_output("task-1", "attempt-1", raw, "application/octet-stream")
        self.assertTrue(artifact.persisted)
        path = self.root / (artifact.artifact_id + ".aap")
        self.assertNotIn(raw, path.read_bytes())
        first = self.store.read_chunk(artifact.artifact_id, 0, 5)
        second = self.store.read_chunk(artifact.artifact_id, first.next_offset, 5)
        third = self.store.read_chunk(artifact.artifact_id, second.next_offset, 6)
        self.assertEqual(b"01234", first.data)
        self.assertEqual(b"56789", second.data)
        self.assertEqual(b"abcdef", third.data)
        self.assertTrue(third.eof)
        self.assertEqual("application/octet-stream", third.mime_type)

    def test_chunk_bounds_and_offsets_are_validated(self):
        artifact = self.store.save_output("task-1", "attempt-1", b"123456", "text/plain")
        with self.assertRaises(ValueError):
            self.store.read_chunk(artifact.artifact_id, -1)
        with self.assertRaises(ValueError):
            self.store.read_chunk(artifact.artifact_id, 0, 262145)
        with self.assertRaises(ValueError):
            self.store.read_chunk(artifact.artifact_id, 7)

    def test_expiry_deletes_encrypted_files_and_metadata(self):
        reference = self.store.save_input("task-1", b"expires", "text/plain", retain_encrypted=True)
        self.now[0] = self.now[0] + timedelta(hours=25)
        self.assertEqual(1, self.store.delete_expired())
        self.assertFalse((self.root / (reference.payload_id + ".aap")).exists())
        with self.assertRaises(PayloadUnavailable):
            self.store.load_input(reference.payload_id, "task-1")

    def test_task_completion_deletes_persistent_and_ephemeral_inputs_only(self):
        retained = self.store.save_input("task-1", b"retained", "text/plain", retain_encrypted=True)
        ephemeral = self.store.save_input("task-1", b"temporary", "text/plain", retain_encrypted=False)
        output = self.store.save_output("task-1", "attempt-1", b"keep output", "text/plain")
        self.assertEqual(2, self.store.delete_task_inputs("task-1"))
        with self.assertRaises(PayloadUnavailable):
            self.store.load_input(retained.payload_id, "task-1")
        with self.assertRaises(PayloadUnavailable):
            self.store.load_input(ephemeral.payload_id, "task-1")
        self.assertEqual(b"keep output", self.store.read_chunk(output.artifact_id, 0).data)

    def test_transient_cache_enforces_entry_and_byte_bounds(self):
        cache = TransientPayloadCache(max_entries=1, max_bytes=8, ttl_seconds=30)
        first = cache.put("task-1", b"1234")
        second = cache.put("task-1", b"5678")
        self.assertIsNone(cache.get("task-1", first))
        self.assertEqual(b"5678", cache.get("task-1", second))

    def test_transient_cache_expiry_is_enforced(self):
        ticks = [10.0]
        cache = TransientPayloadCache(max_entries=2, max_bytes=32, ttl_seconds=5, clock=lambda: ticks[0])
        reference = cache.put("task-1", b"short-lived")
        ticks[0] = 16.0
        self.assertIsNone(cache.get("task-1", reference))

    def test_input_and_output_size_limits_are_enforced(self):
        store = AdultArtifactStore(self.root / "limited", cipher=self.cipher, max_input_bytes=4, max_output_bytes=4)
        with self.assertRaises(PayloadTooLarge):
            store.save_input("task-1", b"12345", "text/plain", retain_encrypted=False)
        with self.assertRaises(PayloadTooLarge):
            store.save_output("task-1", "attempt-1", b"12345", "text/plain")

    def test_permissions_are_restricted_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX permission bits are not available.")
        reference = self.store.save_input("task-1", b"permission check", "text/plain", retain_encrypted=True)
        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.store._index_path.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE((self.root / (reference.payload_id + ".aap")).stat().st_mode))

    def test_store_rejects_symlink_root(self):
        target = self.root / "real"
        target.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Creating a symbolic link is not permitted.")
        with self.assertRaises(PayloadStorageError):
            AdultArtifactStore(link, cipher=self.cipher)

    def test_metadata_schema_rejects_unexpected_tables(self):
        with sqlite3.connect(self.store._index_path) as connection:
            connection.execute("CREATE TABLE unexpected(value TEXT)")
        with self.assertRaises(PayloadStorageError):
            AdultArtifactStore(self.root, cipher=self.cipher)


if __name__ == "__main__":
    unittest.main()
