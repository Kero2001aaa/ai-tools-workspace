"""Tests for Adult OmniRoute configuration and typed adapter contracts."""
import os
import unittest
from dataclasses import asdict
from unittest.mock import patch

from agent.adult.integrations.omniroute_chat import (
    AdultOmniRouteConfig, AdultOmniRouteConfigError, OmniRouteChatAdapter,
)
from agent.adult.integrations.protocols import ImageAnalysisRequest
from agent.model import build_chat_model, load_chat_model


class AdultOmniRouteAdapterTests(unittest.TestCase):
    def test_adult_config_reads_adult_namespace_only_and_hides_key(self):
        values = {
            "MODEL_API_KEY": "workspace-secret",
            "MODEL_NAME": "workspace-model",
            "MODEL_BASE_URL": "https://workspace.example/v1",
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
                api_key="test-key", model="test-model",
                base_url="https://example.invalid/v1",
            )
        options = chat_model.call_args.kwargs
        self.assertEqual(30, options["timeout"])
        self.assertEqual(0, options["max_retries"])
        self.assertEqual("https://example.invalid/v1", options["base_url"])

    def test_workspace_loader_keeps_existing_model_namespace(self):
        with patch.dict(os.environ, {
            "MODEL_API_KEY": "workspace-key",
            "MODEL_NAME": "workspace-model",
            "MODEL_BASE_URL": "https://workspace.example/v1",
        }, clear=True), patch("agent.model.load_dotenv"), patch("langchain_openai.ChatOpenAI") as chat_model:
            load_chat_model()
        options = chat_model.call_args.kwargs
        self.assertEqual("workspace-key", options["api_key"])
        self.assertEqual("workspace-model", options["model"])
        self.assertEqual("https://workspace.example/v1", options["base_url"])

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


if __name__ == "__main__":
    unittest.main()
