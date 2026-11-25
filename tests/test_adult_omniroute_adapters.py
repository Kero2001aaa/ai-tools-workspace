"""Tests for Adult OmniRoute configuration and typed adapter contracts."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict
from unittest.mock import patch

from agent.adult.integrations.omniroute_chat import (
    AdultOmniRouteConfig, AdultOmniRouteConfigError, OmniRouteChatAdapter,
)
from agent.adult.integrations.protocols import ImageAnalysisRequest, TextRequest
from agent.adult.integrations.omniroute_text import (
    OmniRouteTextAdapter, OmniRouteTextAdapterError,
)
from agent.adult.adult_text_input import encode_text_input
from agent.adult.models import RouteCandidate
from agent.adult.payload_store import AdultArtifactStore
from agent.model import build_chat_model, load_chat_model


class AdultOmniRouteAdapterTests(unittest.TestCase):
    def test_adult_config_reads_adult_namespace_only_and_hides_key(self):
        values = {
            "MODEL_API_KEY": "workspace-secret",
            "MODEL_NAME": "openrouter/free",
            "MODEL_BASE_URL": "https://openrouter.ai/api/v1",
            "ADULT_OMNIROUTE_API_KEY": "adult-secret",
            "ADULT_OMNIROUTE_BASE_URL": "https://adult.example/v1",
        }
        config = AdultOmniRouteConfig.from_env(values)
        self.assertEqual("https://adult.example/v1", config.base_url)
        self.assertNotIn("adult-secret", repr(config))
        self.assertNotIn("workspace-secret", repr(config))
        self.assertEqual({"base_url": "https://adult.example/v1"}, config.to_dict())
        with self.assertRaises(TypeError):
            asdict(config)
        self.assertFalse(hasattr(config, "__dict__"))
        with self.assertRaises(AttributeError):
            config.api_key = "replacement-secret"
        other = AdultOmniRouteConfig("https://adult.example/v1", "different-secret")
        self.assertEqual(config, other)

    def test_incomplete_adult_config_stays_offline(self):
        self.assertIsNone(AdultOmniRouteConfig.from_env({}))
        self.assertIsNone(AdultOmniRouteConfig.from_env({
            "ADULT_OMNIROUTE_API_KEY": "partial-secret",
        }))

    def test_non_loopback_http_endpoint_is_rejected(self):
        with self.assertRaises(AdultOmniRouteConfigError):
            AdultOmniRouteConfig("http://remote.example/v1", "adult-secret")

    def test_adapter_builds_client_lazily_from_adult_config(self):
        config = AdultOmniRouteConfig("https://adult.example/v1", "adult-secret")
        adapter = OmniRouteChatAdapter(config, "verified-model")
        with patch("agent.adult.integrations.omniroute_chat.build_chat_model", return_value="mock-client") as factory:
            self.assertIsNone(adapter._client)
            self.assertEqual("mock-client", adapter.build_client())
            self.assertEqual("mock-client", adapter.build_client())
        factory.assert_called_once_with(
            api_key="adult-secret", model="verified-model",
            base_url="https://adult.example/v1", timeout=30, max_retries=0,
        )

    def test_explicit_factory_uses_bounded_no_retry_configuration(self):
        with patch("langchain_openai.ChatOpenAI") as chat_model:
            build_chat_model(
                api_key="test-key", model="openrouter/free",
                base_url="https://openrouter.ai/api/v1",
            )
        options = chat_model.call_args.kwargs
        self.assertEqual(30, options["timeout"])
        self.assertEqual(0, options["max_retries"])
        self.assertEqual("https://openrouter.ai/api/v1", options["base_url"])

    def test_workspace_loader_keeps_existing_model_namespace(self):
        with patch.dict(os.environ, {
            "MODEL_API_KEY": "workspace-key",
            "MODEL_NAME": "openrouter/free",
            "MODEL_BASE_URL": "https://openrouter.ai/api/v1",
        }, clear=True), patch("agent.model.load_dotenv"), patch("langchain_openai.ChatOpenAI") as chat_model:
            load_chat_model()
        options = chat_model.call_args.kwargs
        self.assertEqual("workspace-key", options["api_key"])
        self.assertEqual("openrouter/free", options["model"])
        self.assertEqual("https://openrouter.ai/api/v1", options["base_url"])

    def test_adapter_and_artifact_results_reject_unsafe_metadata(self):
        from agent.adult.integrations.protocols import AdapterResult, ArtifactResult
        with self.assertRaises(ValueError):
            AdapterResult("output-reference-123456", {"tokens": True})
        with self.assertRaises(ValueError):
            ArtifactResult("artifact-1234567890", "image/svg+xml", 100, "a" * 64)
        with self.assertRaises(ValueError):
            ArtifactResult("artifact-1234567890", "image/png", 0, "a" * 64)

    def test_image_request_rejects_unsupported_mime_type(self):
        with self.assertRaises(ValueError):
            ImageAnalysisRequest(
                task_id="task-123456789012", step_id="step-123456789012",
                input_payload_id="payload-123456789", mime_type="image/svg+xml",
            )


class AdultOmniRouteTextAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AdultArtifactStore(Path(self.temp.name) / "payloads")
        self.task_id = "task-123456789012"
        self.step_id = "step-123456789012"
        self.config = AdultOmniRouteConfig(
            "https://adult.example/v1", "adult-secret",
        )
        self.route = RouteCandidate(
            adapter_id="omniroute", adapter_version="1",
            provider_id="provider", model_id="verified-model",
            model_version="1", tool_id=None, tool_version=None,
            task_type="text.generate", cost_class="free_verified",
            capability_version="a" * 64,
            verified_at="2026-11-21T12:00:00.000000Z",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _request(self, task_type="text.generate", text="source text", instruction="be concise"):
        payload = encode_text_input(task_type, text, instruction)
        ref = self.store.save_input(
            self.task_id, payload, "application/json", retain_encrypted=False,
        )
        return TextRequest(
            task_id=self.task_id, step_id=self.step_id,
            task_type=task_type, input_payload_id=ref.payload_id,
        )

    def _adapter(self, client, route=None, factory_calls=None):
        def factory():
            if factory_calls is not None:
                factory_calls.append(True)
            return client
        return OmniRouteTextAdapter(
            self.config, route or self.route, self.store,
            client_factory=factory,
        )

    def test_invokes_fixed_system_instruction_and_returns_bounded_text_metadata(self):
        class FakeClient:
            def __init__(self):
                self.messages = None
            def invoke(self, messages):
                self.messages = messages
                return SimpleNamespace(
                    content="generated result",
                    usage_metadata={"input_tokens": 12, "output_tokens": 7, "unsafe": "drop"},
                    response_metadata={"cost_microusd": 0, "provider_body": "must be ignored"},
                )

        client = FakeClient()
        calls = []
        adapter = self._adapter(client, factory_calls=calls)
        result = adapter.invoke(self._request())
        self.assertEqual("generated result", result.text)
        self.assertEqual({"input_tokens": 12, "output_tokens": 7}, result.usage)
        self.assertEqual(0, result.cost_microusd)
        self.assertEqual(1, len(calls))
        self.assertEqual("system", client.messages[0][0])
        self.assertIn("source_text", client.messages[1][1])
        self.assertIn("user_instruction", client.messages[1][1])
        self.assertNotIn("source text", client.messages[0][1])
        self.assertNotIn("generated result", repr(result))

    def test_rejects_route_task_mismatch_before_provider_call(self):
        class FakeClient:
            def invoke(self, _messages):
                raise AssertionError("provider must not be called")
        adapter = self._adapter(FakeClient())
        request = self._request("text.rewrite")
        with self.assertRaises(OmniRouteTextAdapterError) as raised:
            adapter.invoke(request)
        self.assertEqual("route_invalid", raised.exception.error_code)

    def test_rejects_non_text_and_oversized_provider_content(self):
        class FakeClient:
            def __init__(self, content):
                self.content = content
            def invoke(self, _messages):
                return SimpleNamespace(content=self.content)
        for content, code in (([{"type": "text", "text": "x"}], "output_invalid"),
                              ("x" * 20001, "output_too_large")):
            with self.subTest(code=code):
                adapter = self._adapter(FakeClient(content))
                with self.assertRaises(OmniRouteTextAdapterError) as raised:
                    adapter.invoke(self._request())
                self.assertEqual(code, raised.exception.error_code)

    def test_provider_exception_is_sanitized(self):
        class FakeClient:
            def invoke(self, _messages):
                raise RuntimeError("provider response contains private prompt")
        adapter = self._adapter(FakeClient())
        with self.assertRaises(OmniRouteTextAdapterError) as raised:
            adapter.invoke(self._request())
        self.assertEqual("provider_failed", raised.exception.error_code)
        self.assertNotIn("private prompt", str(raised.exception))

    def test_input_payload_ownership_is_enforced(self):
        class FakeClient:
            def invoke(self, _messages):
                raise AssertionError("provider must not be called")
        adapter = self._adapter(FakeClient())
        request = TextRequest(
            task_id="task-999999999999", step_id=self.step_id,
            task_type="text.generate", input_payload_id="missing-payload-123",
        )
        with self.assertRaises(OmniRouteTextAdapterError) as raised:
            adapter.invoke(request)
        self.assertEqual("input_unavailable", raised.exception.error_code)

    def test_only_free_verified_routes_are_accepted(self):
        local_route = RouteCandidate(
            adapter_id="omniroute", adapter_version="1",
            provider_id="provider", model_id="verified-model",
            model_version="1", tool_id=None, tool_version=None,
            task_type="text.generate", cost_class="local",
            capability_version="b" * 64,
            verified_at="2026-11-21T12:00:00.000000Z",
        )
        with self.assertRaises(OmniRouteTextAdapterError) as raised:
            self._adapter(object(), route=local_route)
        self.assertEqual("route_invalid", raised.exception.error_code)



if __name__ == "__main__":
    unittest.main()
