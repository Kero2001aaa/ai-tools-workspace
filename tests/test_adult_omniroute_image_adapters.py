"""Mocked-only tests for the Adult OmniRoute image adapters."""
from __future__ import annotations

import base64
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import httpx

from agent.adult.integrations.omniroute_chat import AdultOmniRouteConfig
from agent.adult.integrations.omniroute_image import (
    OmniRouteImageAdapterError,
    OmniRouteImageEditingAdapter,
    OmniRouteImageGenerationAdapter,
    OmniRouteImageUnderstandingAdapter,
)
from agent.adult.integrations.protocols import (
    ImageAnalysisRequest,
    ImageEditRequest,
    ImageGenerationRequest,
)
from agent.adult.models import RouteCandidate
from agent.adult.payload_store import AdultArtifactStore


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(width: int = 1, height: int = 1) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanline = b"\x00" + (b"\x00" * width * 4)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanline))
        + _png_chunk(b"IEND", b"")
    )


class FakeResponse:
    def __init__(self, payload=None, *, raw=None, headers=None, status_code=200):
        self.status_code = status_code
        self.headers = {
            "content-type": "application/json",
            **(headers or {}),
        }
        self.raw = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def iter_bytes(self, chunk_size=65536):
        for offset in range(0, len(self.raw), chunk_size):
            yield self.raw[offset:offset + chunk_size]


class FakeResponseContext:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def stream(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        if self.error is not None:
            raise self.error
        return FakeResponseContext(self.response)


class AdultOmniRouteImageAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AdultArtifactStore(Path(self.temp.name) / "payloads")
        self.task_id = "task-123456789012"
        self.step_id = "step-123456789012"
        self.config = AdultOmniRouteConfig("https://adult.example/v1", "adult-secret")

    def tearDown(self):
        self.temp.cleanup()

    def _route(self, task_type="image.generate", **overrides):
        values = {
            "adapter_id": "omniroute",
            "adapter_version": "1",
            "provider_id": "provider",
            "model_id": "verified-model",
            "model_version": "1",
            "tool_id": None,
            "tool_version": None,
            "task_type": task_type,
            "cost_class": "free_verified",
            "capability_version": "a" * 64,
            "verified_at": "2026-11-22T12:00:00.000000Z",
        }
        values.update(overrides)
        return RouteCandidate(**values)

    def _save_input(self, payload, mime_type):
        return self.store.save_input(
            self.task_id, payload, mime_type, retain_encrypted=False,
        ).payload_id

    def _image_request(self, task_id=None, mime_type="image/png"):
        payload_id = self._save_input(_png(), mime_type)
        return ImageAnalysisRequest(
            task_id=task_id or self.task_id,
            step_id=self.step_id,
            input_payload_id=payload_id,
            mime_type=mime_type,
        )

    def _prompt_request(self, task_type="image.generate", prompt="A red car"):
        prompt_id = self._save_input(prompt.encode("utf-8"), "text/plain")
        return ImageGenerationRequest(
            task_id=self.task_id,
            step_id=self.step_id,
            prompt_payload_id=prompt_id,
        )

    def test_analysis_uses_fixed_instruction_inline_image_and_bounded_metadata(self):
        image_bytes = _png()
        payload_id = self._save_input(image_bytes, "image/png")
        request = ImageAnalysisRequest(
            task_id=self.task_id, step_id=self.step_id,
            input_payload_id=payload_id, mime_type="image/png",
        )
        fake = FakeClient(FakeResponse(
            {
                "choices": [{"message": {"content": "A small red car."}}],
                "usage": {"input_tokens": 11, "output_tokens": 5, "ignored": "drop"},
            },
            headers={
                "x-omniroute-response-cost": "0.0000000000",
                "x-omniroute-tokens-in": "11",
                "x-omniroute-tokens-out": "5",
            },
        ))
        adapter = OmniRouteImageUnderstandingAdapter(
            self.config, self._route("image.analyze"), self.store,
            client_factory=lambda: fake,
        )

        result = adapter.analyze(request)

        self.assertEqual("A small red car.", result.text)
        self.assertEqual({"input_tokens": 11, "output_tokens": 5}, result.usage)
        self.assertEqual(0, result.cost_microusd)
        self.assertNotIn("A small red car.", repr(result))
        self.assertEqual(1, len(fake.calls))
        method, endpoint, kwargs = fake.calls[0]
        self.assertEqual(("POST", "chat/completions"), (method, endpoint))
        body = kwargs["json"]
        self.assertEqual("verified-model", body["model"])
        self.assertFalse(body["stream"])
        self.assertEqual("system", body["messages"][0]["role"])
        self.assertIn("untrusted data", body["messages"][0]["content"])
        content = body["messages"][1]["content"]
        data_url = content[1]["image_url"]["url"]
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        encoded = data_url.split(",", 1)[1]
        self.assertEqual(image_bytes, base64.b64decode(encoded, validate=True))
        self.assertNotIn("http://", data_url)
        self.assertNotIn("https://", data_url)

    def test_generate_uses_exact_model_one_image_and_inline_base64(self):
        image_bytes = _png()
        prompt_id = self._save_input(b"A red car", "text/plain")
        request = ImageGenerationRequest(
            task_id=self.task_id, step_id=self.step_id,
            prompt_payload_id=prompt_id,
        )
        fake = FakeClient(FakeResponse(
            {
                "data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii")}],
                "usage": {"image_tokens": 9, "total_tokens": 9, "unsafe": "ignore"},
            },
            headers={
                "x-omniroute-response-cost": "0.0000010000",
                "x-omniroute-tokens-in": "4",
            },
        ))
        adapter = OmniRouteImageGenerationAdapter(
            self.config, self._route("image.generate"), self.store,
            client_factory=lambda: fake,
        )

        result = adapter.generate(request)

        self.assertEqual(image_bytes, result.image_bytes)
        self.assertEqual("image/png", result.mime_type)
        self.assertEqual({"image_tokens": 9, "total_tokens": 9, "input_tokens": 4}, result.usage)
        self.assertEqual(1, result.cost_microusd)
        self.assertNotIn(image_bytes.decode("latin1"), repr(result))
        self.assertEqual(1, len(fake.calls))
        method, endpoint, kwargs = fake.calls[0]
        self.assertEqual(("POST", "images/generations"), (method, endpoint))
        self.assertEqual(
            {
                "model": "verified-model",
                "prompt": "A red car",
                "n": 1,
                "output_format": "png",
            },
            kwargs["json"],
        )

    def test_edit_uses_task_owned_image_prompt_and_multipart(self):
        image_bytes = _png()
        image_id = self._save_input(image_bytes, "image/png")
        prompt_id = self._save_input(b"Make the car blue", "text/plain")
        request = ImageEditRequest(
            task_id=self.task_id, step_id=self.step_id,
            image_payload_id=image_id, prompt_payload_id=prompt_id,
            mime_type="image/png",
        )
        output = _png(width=2, height=1)
        fake = FakeClient(FakeResponse({
            "data": [{"b64_json": base64.b64encode(output).decode("ascii")}],
        }))
        adapter = OmniRouteImageEditingAdapter(
            self.config, self._route("image.edit"), self.store,
            client_factory=lambda: fake,
        )

        result = adapter.edit(request)

        self.assertEqual(output, result.image_bytes)
        self.assertEqual("image/png", result.mime_type)
        self.assertEqual(1, len(fake.calls))
        method, endpoint, kwargs = fake.calls[0]
        self.assertEqual(("POST", "images/edits"), (method, endpoint))
        self.assertEqual(
            {
                "model": "verified-model",
                "prompt": "Make the car blue",
                "n": "1",
                "output_format": "png",
            },
            kwargs["data"],
        )
        self.assertEqual(
            ("input.png", image_bytes, "image/png"),
            kwargs["files"]["image"],
        )
        self.assertNotIn("Authorization", repr(kwargs))

    def test_wrong_route_type_cost_class_adapter_identity_and_version_are_rejected(self):
        cases = (
            self._route("image.generate", task_type="image.analyze"),
            self._route("image.generate", cost_class="local"),
            self._route("image.generate", adapter_id="other-adapter"),
            self._route("image.generate", adapter_version="2"),
            self._route("image.generate", model_id=None),
        )
        for route in cases:
            with self.subTest(route=route):
                with self.assertRaises(OmniRouteImageAdapterError) as raised:
                    OmniRouteImageGenerationAdapter(self.config, route, self.store)
                self.assertEqual("route_invalid", raised.exception.error_code)

    def test_input_ownership_and_mime_are_checked_before_provider_call(self):
        payload_id = self._save_input(_png(), "image/png")
        fake = FakeClient(FakeResponse({"choices": [{"message": {"content": "never"}}]}))
        adapter = OmniRouteImageUnderstandingAdapter(
            self.config, self._route("image.analyze"), self.store,
            client_factory=lambda: fake,
        )
        wrong_owner = ImageAnalysisRequest(
            task_id="task-999999999999", step_id=self.step_id,
            input_payload_id=payload_id, mime_type="image/png",
        )
        with self.assertRaises(OmniRouteImageAdapterError) as raised:
            adapter.analyze(wrong_owner)
        self.assertEqual("input_unavailable", raised.exception.error_code)

        wrong_mime = ImageAnalysisRequest(
            task_id=self.task_id, step_id=self.step_id,
            input_payload_id=payload_id, mime_type="image/jpeg",
        )
        with self.assertRaises(OmniRouteImageAdapterError) as raised:
            adapter.analyze(wrong_mime)
        self.assertEqual("input_invalid", raised.exception.error_code)
        self.assertEqual([], fake.calls)

    def test_generation_prompt_ownership_is_checked_before_provider_call(self):
        prompt_id = self._save_input(b"A red car", "text/plain")
        request = ImageGenerationRequest(
            task_id="task-999999999999", step_id=self.step_id,
            prompt_payload_id=prompt_id,
        )
        fake = FakeClient(FakeResponse({"data": []}))
        adapter = OmniRouteImageGenerationAdapter(
            self.config, self._route("image.generate"), self.store,
            client_factory=lambda: fake,
        )
        with self.assertRaises(OmniRouteImageAdapterError) as raised:
            adapter.generate(request)
        self.assertEqual("input_unavailable", raised.exception.error_code)
        self.assertEqual([], fake.calls)

    def test_url_only_and_malformed_base64_outputs_are_rejected_without_fetch(self):
        prompt_id = self._save_input(b"A red car", "text/plain")
        request = ImageGenerationRequest(
            task_id=self.task_id, step_id=self.step_id,
            prompt_payload_id=prompt_id,
        )
        for payload in (
            {"data": [{"url": "https://provider.example/image.png"}]},
            {"data": [{"b64_json": "not-base64"}]},
            {"data": []},
            {"data": [{"b64_json": base64.b64encode(b"not-image").decode("ascii")}]},
        ):
            with self.subTest(payload=payload):
                fake = FakeClient(FakeResponse(payload))
                adapter = OmniRouteImageGenerationAdapter(
                    self.config, self._route("image.generate"), self.store,
                    client_factory=lambda: fake,
                )
                with self.assertRaises(OmniRouteImageAdapterError) as raised:
                    adapter.generate(request)
                self.assertIn(raised.exception.error_code, {"output_invalid", "output_too_large"})
                self.assertEqual(1, len(fake.calls))
                self.assertNotIn("https://provider.example", str(raised.exception))

    def test_provider_errors_and_timeouts_are_sanitized_and_not_retried(self):
        prompt_id = self._save_input(b"private prompt", "text/plain")
        request = ImageGenerationRequest(
            task_id=self.task_id, step_id=self.step_id,
            prompt_payload_id=prompt_id,
        )
        errors = (
            (RuntimeError("private prompt and provider body"), "provider_failed"),
            (httpx.ReadTimeout("private prompt timeout"), "provider_timeout"),
        )
        for error, expected in errors:
            with self.subTest(expected=expected):
                fake = FakeClient(error=error)
                adapter = OmniRouteImageGenerationAdapter(
                    self.config, self._route("image.generate"), self.store,
                    client_factory=lambda: fake,
                )
                with self.assertRaises(OmniRouteImageAdapterError) as raised:
                    adapter.generate(request)
                self.assertEqual(expected, raised.exception.error_code)
                self.assertNotIn("private prompt", str(raised.exception))
                self.assertEqual(1, len(fake.calls))

    def test_oversized_provider_response_is_rejected_before_json_parsing(self):
        prompt_id = self._save_input(b"A red car", "text/plain")
        request = ImageGenerationRequest(
            task_id=self.task_id, step_id=self.step_id,
            prompt_payload_id=prompt_id,
        )
        fake = FakeClient(FakeResponse(
            raw=b" " * (27 * 1024 * 1024 + 1),
        ))
        adapter = OmniRouteImageGenerationAdapter(
            self.config, self._route("image.generate"), self.store,
            client_factory=lambda: fake,
        )
        with self.assertRaises(OmniRouteImageAdapterError) as raised:
            adapter.generate(request)
        self.assertEqual("output_too_large", raised.exception.error_code)
        self.assertEqual(1, len(fake.calls))

    def test_metadata_rejects_malformed_cost_and_does_not_round_positive_cost_down(self):
        prompt_id = self._save_input(b"A red car", "text/plain")
        request = ImageGenerationRequest(
            task_id=self.task_id, step_id=self.step_id,
            prompt_payload_id=prompt_id,
        )
        image_bytes = _png()
        encoded = base64.b64encode(image_bytes).decode("ascii")
        fake = FakeClient(FakeResponse(
            {"data": [{"b64_json": encoded}]},
            headers={"x-omniroute-response-cost": "0.0000000001"},
        ))
        adapter = OmniRouteImageGenerationAdapter(
            self.config, self._route("image.generate"), self.store,
            client_factory=lambda: fake,
        )
        result = adapter.generate(request)
        self.assertEqual(1, result.cost_microusd)

        malformed = FakeClient(FakeResponse(
            {"data": [{"b64_json": encoded}]},
            headers={"x-omniroute-response-cost": "not-a-cost"},
        ))
        adapter = OmniRouteImageGenerationAdapter(
            self.config, self._route("image.generate"), self.store,
            client_factory=lambda: malformed,
        )
        with self.assertRaises(OmniRouteImageAdapterError) as raised:
            adapter.generate(request)
        self.assertEqual("metadata_invalid", raised.exception.error_code)


if __name__ == "__main__":
    unittest.main()
