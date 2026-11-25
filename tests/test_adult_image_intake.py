"""Leased image intake over real stores, no adapter or network."""
import base64
import secrets
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from agent.adult.adult_image_input import decode_image_input
from agent.adult.adult_image_intake import (
    AdultImageIntake, ImageIntakeError, canonical_image_input,
)
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.models import AdultTaskRequest, TaskBudget
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher


def png_bytes(width=2, height=2):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class CanonicalImageInputTests(unittest.TestCase):
    def test_deterministic_for_equal_validated_content(self):
        self.assertEqual(canonical_image_input("image.generate", None, "a red square"),
                         canonical_image_input("image.generate", None, "a red square"))
        self.assertEqual(canonical_image_input("image.edit", png_bytes(), "make it blue"),
                         canonical_image_input("image.edit", png_bytes(), "make it blue"))

    def test_distinct_content_binds_distinct_canonical_bytes(self):
        self.assertNotEqual(canonical_image_input("image.generate", None, "a red square"),
                            canonical_image_input("image.generate", None, "a blue square"))
        self.assertNotEqual(canonical_image_input("image.analyze", png_bytes(2, 2), None),
                            canonical_image_input("image.analyze", png_bytes(3, 2), None))

    def test_canonical_form_is_strictly_decodable(self):
        for task_type, image, prompt in (("image.analyze", png_bytes(), None),
                                         ("image.analyze", png_bytes(), "what is shown?"),
                                         ("image.generate", None, "a red square"),
                                         ("image.edit", png_bytes(), "make it blue")):
            envelope = decode_image_input(canonical_image_input(task_type, image, prompt), task_type)
            self.assertEqual(task_type, envelope.task_type)

    def test_optional_analysis_prompt_binds_distinct_canonical_bytes(self):
        promptless = canonical_image_input("image.analyze", png_bytes(), None)
        prompted = canonical_image_input("image.analyze", png_bytes(), "what is shown?")
        self.assertNotEqual(promptless, prompted)
        self.assertEqual(prompted, canonical_image_input("image.analyze", png_bytes(), "what is shown?"))

    def test_shape_and_content_rejections(self):
        for task_type, image, prompt in (
            ("image.analyze", None, None), ("image.analyze", None, "orphan prompt"),
            ("image.generate", png_bytes(), None), ("image.generate", None, None),
            ("image.edit", png_bytes(), None), ("image.edit", None, "make it blue"),
            ("image.analyze", b"not-an-image", None), ("text.analyze", None, None),
        ):
            with self.subTest(task_type=task_type, image=image, prompt=prompt):
                with self.assertRaises(Exception):
                    canonical_image_input(task_type, image, prompt)


class AdultImageIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tasks = AdultTaskStore(self.root / "tasks.sqlite3")
        self.key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        self.cipher = PayloadCipher(self.key)
        self.artifacts = AdultArtifactStore(self.root / "artifacts", cipher=self.cipher)
        self.intake = AdultImageIntake(self.tasks, self.artifacts, self.cipher)
        self.image = png_bytes()

    def make_task(self, task_type, image, prompt, retention_policy="ephemeral"):
        canonical = canonical_image_input(task_type, image, prompt)
        task = self.tasks.create_task(
            AdultTaskRequest(task_type, self.cipher.fingerprint(canonical),
                             retention_policy=retention_policy), TaskBudget())
        lease = self.tasks.claim_task(task.task_id, "image-intake-test", lease_seconds=60)
        task = self.tasks.get_task(task.task_id)
        return task, lease

    def attach(self, task, lease, image, prompt):
        return self.intake.attach(task.task_id, image=image, prompt=prompt,
                                  lease=lease, expected_state_version=task.state_version)

    def envelope(self, task_id):
        request = self.tasks.get_task_request(task_id)
        return decode_image_input(self.artifacts.load_input(request.payload_id, task_id),
                                  request.task_type)

    def test_attach_stores_envelope_and_parts_readably(self):
        task, lease = self.make_task("image.edit", self.image, "make it blue")
        self.attach(task, lease, self.image, "make it blue")
        request = self.tasks.get_task_request(task.task_id)
        self.assertIsNotNone(request.payload_id)
        envelope = self.envelope(task.task_id)
        self.assertEqual(self.image, self.artifacts.load_input(envelope.image_payload_id, task.task_id))
        self.assertEqual("make it blue",
                         self.artifacts.load_input(envelope.prompt_payload_id, task.task_id).decode())

    def test_generate_and_analyze_shapes(self):
        generate, lease = self.make_task("image.generate", None, "a red square")
        attached = self.attach(generate, lease, None, "a red square")
        envelope = self.envelope(generate.task_id)
        self.assertIsNone(envelope.image_payload_id)
        self.assertIsNotNone(envelope.prompt_payload_id)
        # Only one task may hold the active lease store-wide.
        self.tasks.release_lease(lease, attached.state_version, "checkpointed", "yielded")
        analyze, lease = self.make_task("image.analyze", self.image, None)
        self.attach(analyze, lease, self.image, None)
        envelope = self.envelope(analyze.task_id)
        self.assertIsNotNone(envelope.image_payload_id)
        self.assertIsNone(envelope.prompt_payload_id)

    def test_changed_content_fails_before_any_storage(self):
        task, lease = self.make_task("image.generate", None, "a red square")
        with self.assertRaises(ImageIntakeError) as raised:
            self.attach(task, lease, None, "a blue square")
        self.assertEqual("input_invalid", str(raised.exception))
        self.assertIsNone(self.tasks.get_task_request(task.task_id).payload_id)
        self.assertEqual(0, self.artifacts.delete_task_inputs(task.task_id))

    def test_invalid_content_fails_before_any_storage(self):
        task, lease = self.make_task("image.analyze", self.image, None)
        for image, prompt in ((b"not-an-image", None), (self.image, "unexpected"), (None, None)):
            with self.subTest(image=image, prompt=prompt):
                with self.assertRaises(ImageIntakeError):
                    self.attach(task, lease, image, prompt)
        self.assertIsNone(self.tasks.get_task_request(task.task_id).payload_id)
        self.assertEqual(0, self.artifacts.delete_task_inputs(task.task_id))

    def test_reattachment_is_denied(self):
        task, lease = self.make_task("image.generate", None, "a red square")
        self.attach(task, lease, None, "a red square")
        with self.assertRaises(ImageIntakeError) as raised:
            self.attach(self.tasks.get_task(task.task_id), lease, None, "a red square")
        self.assertEqual("intake_unavailable", str(raised.exception))

    def test_commit_failure_discards_only_created_payloads(self):
        task, lease = self.make_task("image.edit", self.image, "make it blue")
        with patch.object(self.tasks, "attach_input", side_effect=OSError("store unavailable")):
            with self.assertRaises(ImageIntakeError) as raised:
                self.attach(task, lease, self.image, "make it blue")
        self.assertEqual("intake_unavailable", str(raised.exception))
        self.assertIsNone(self.tasks.get_task_request(task.task_id).payload_id)
        self.assertEqual(0, self.artifacts.delete_task_inputs(task.task_id))

    def test_committed_payload_is_never_deleted_after_readback_failure(self):
        task, lease = self.make_task("image.generate", None, "a red square")
        real_attach = self.tasks.attach_input

        def commit_then_lose(*args, **kwargs):
            real_attach(*args, **kwargs)
            raise OSError("response lost")

        with patch.object(self.tasks, "attach_input", side_effect=commit_then_lose):
            with self.assertRaises(ImageIntakeError) as raised:
                self.attach(task, lease, None, "a red square")
        self.assertEqual("intake_committed_readback_failed", str(raised.exception))
        request = self.tasks.get_task_request(task.task_id)
        self.assertIsNotNone(request.payload_id)
        decode_image_input(self.artifacts.load_input(request.payload_id, task.task_id),
                           request.task_type)

    def test_cleanup_failure_is_surfaced(self):
        task, lease = self.make_task("image.generate", None, "a red square")
        with patch.object(self.tasks, "attach_input", side_effect=OSError("store unavailable")), \
                patch.object(self.artifacts, "discard_input", side_effect=OSError("disk stuck")):
            with self.assertRaises(ImageIntakeError) as raised:
                self.attach(task, lease, None, "a red square")
        self.assertEqual("input_cleanup_failed", str(raised.exception))

    def test_encrypted_retention_requires_matching_artifact_key(self):
        keyless = AdultArtifactStore(self.root / "keyless")
        intake = AdultImageIntake(self.tasks, keyless, self.cipher)
        canonical = canonical_image_input("image.generate", None, "a red square")
        with patch.dict("os.environ", {"ADULT_PAYLOAD_ENCRYPTION_KEY": self.key}):
            task = self.tasks.create_task(
                AdultTaskRequest("image.generate", self.cipher.fingerprint(canonical),
                                 retention_policy="encrypted"), TaskBudget())
        lease = self.tasks.claim_task(task.task_id, "image-intake-test", lease_seconds=60)
        task = self.tasks.get_task(task.task_id)
        with self.assertRaises(ImageIntakeError) as raised:
            intake.attach(task.task_id, image=None, prompt="a red square",
                          lease=lease, expected_state_version=task.state_version)
        self.assertEqual("input_invalid", str(raised.exception))

    def test_encrypted_retention_roundtrips_all_parts(self):
        canonical = canonical_image_input("image.edit", self.image, "make it blue")
        with patch.dict("os.environ", {"ADULT_PAYLOAD_ENCRYPTION_KEY": self.key}):
            task = self.tasks.create_task(
                AdultTaskRequest("image.edit", self.cipher.fingerprint(canonical),
                                 retention_policy="encrypted"), TaskBudget())
        lease = self.tasks.claim_task(task.task_id, "image-intake-test", lease_seconds=60)
        task = self.tasks.get_task(task.task_id)
        self.attach(task, lease, self.image, "make it blue")
        reopened = AdultArtifactStore(self.root / "artifacts", cipher=self.cipher)
        envelope = decode_image_input(
            reopened.load_input(self.tasks.get_task_request(task.task_id).payload_id, task.task_id),
            "image.edit")
        self.assertEqual(self.image, reopened.load_input(envelope.image_payload_id, task.task_id))
        self.assertEqual("make it blue",
                         reopened.load_input(envelope.prompt_payload_id, task.task_id).decode())

    def test_prompt_limits_hold(self):
        with self.assertRaises(Exception):
            canonical_image_input("image.generate", None, "x" * 4001)
        with self.assertRaises(Exception):
            canonical_image_input("image.generate", None, "  ")


if __name__ == "__main__":
    unittest.main()
