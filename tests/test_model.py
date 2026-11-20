"""Tests for OpenAI-compatible model configuration."""
import os
import unittest
from unittest.mock import patch

from agent.model import load_chat_model


class ModelConfigurationTests(unittest.TestCase):
    def test_model_requests_have_bounded_timeout_and_no_retries(self):
        with patch.dict(os.environ, {
            "MODEL_API_KEY": "test-key",
            "MODEL_NAME": "test-model",
            "MODEL_BASE_URL": "https://example.invalid/v1",
        }, clear=True), patch("langchain_openai.ChatOpenAI") as chat_model:
            load_chat_model()
        options = chat_model.call_args.kwargs
        self.assertEqual(30, options["timeout"])
        self.assertEqual(0, options["max_retries"])
        self.assertEqual("https://example.invalid/v1", options["base_url"])

    def test_empty_model_configuration_stays_offline(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("agent.model.load_dotenv"):
            self.assertIsNone(load_chat_model())


if __name__ == "__main__":
    unittest.main()
