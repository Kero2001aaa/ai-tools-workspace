"""Load an optional OpenAI-compatible chat model from environment variables."""
import os

from dotenv import load_dotenv


def load_chat_model():
    """Return a configured ChatOpenAI adapter, or None for safe offline mode.

    The endpoint can be a hosted provider or a self-hosted OpenAI-compatible
    server. This function does not make a network request; inference happens
    only when the compiled graph invokes the returned model.
    """
    load_dotenv()

    api_key = os.getenv("MODEL_API_KEY", "").strip()
    model_name = os.getenv("MODEL_NAME", "").strip()
    base_url = os.getenv("MODEL_BASE_URL", "").strip()

    if not api_key and not model_name and not base_url:
        return None

    if not api_key or not model_name:
        raise RuntimeError(
            "Model configuration is incomplete. Set both MODEL_API_KEY "
            "and MODEL_NAME, or leave all MODEL_* variables blank for offline mode."
        )

    from langchain_openai import ChatOpenAI

    options = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0,\n        "timeout": 30,
    }
    if base_url:
        options["base_url"] = base_url

    return ChatOpenAI(**options)
