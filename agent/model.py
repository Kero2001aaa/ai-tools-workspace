"""Load optional OpenAI-compatible chat models with explicit configuration."""

import os

from dotenv import load_dotenv


def build_chat_model(
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    timeout: int = 30,
    max_retries: int = 0,
):
    """Construct a ChatOpenAI client from explicit, already-authorized values.

    Client construction does not issue a network request. The returned client
    performs inference only when a later pipeline invokes it.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise RuntimeError("A non-empty API key is required for model construction.")
    if not isinstance(model, str) or not model.strip():
        raise RuntimeError("A non-empty model identifier is required for model construction.")
    if base_url is not None and (
        not isinstance(base_url, str) or not base_url.strip()
    ):
        raise RuntimeError("base_url must be None or a non-empty URL.")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 120:
        raise ValueError("timeout must be an integer from 1 through 120.")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries != 0:
        raise ValueError("Automatic model retries are disabled; max_retries must be 0.")

    if base_url is None or base_url.rstrip("/") != "https://openrouter.ai/api/v1" or model != "openrouter/free":
        raise RuntimeError("Configured route has no verified free-only execution grant")
    import httpx
    from langchain_openai import ChatOpenAI
    from agent.free_route_guard import FreeRouteTransport, AsyncFreeRouteTransport, PROVIDER
    return ChatOpenAI(
        api_key=api_key, model=model, base_url=base_url,
        timeout=timeout, max_retries=0, max_tokens=512, streaming=False,
        extra_body={"provider": PROVIDER},
        http_client=httpx.Client(transport=FreeRouteTransport(), follow_redirects=False),
        http_async_client=httpx.AsyncClient(transport=AsyncFreeRouteTransport(), follow_redirects=False),
    )


def load_chat_model():
    """Return the Workspace ChatOpenAI adapter, or None for safe offline mode.

    This compatibility loader deliberately reads only the Workspace MODEL_*
    variables. Adult configuration is resolved separately by its own adapter.
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

    return build_chat_model(
        api_key=api_key, model=model_name, base_url=base_url or None,
        timeout=30, max_retries=0,
    )
