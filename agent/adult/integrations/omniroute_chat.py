"""Adult-only OmniRoute configuration and lazy ChatOpenAI client construction."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from dotenv import load_dotenv

from agent.model import build_chat_model
from agent.adult.models import _identifier


class AdultOmniRouteConfigError(ValueError):
    """Adult OmniRoute settings are incomplete or unsafe."""


@dataclass(frozen=True)
class AdultOmniRouteConfig:
    base_url: str
    api_key: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not isinstance(self.api_key, str) or not self.api_key.strip():
            raise AdultOmniRouteConfigError("Adult OmniRoute configuration is incomplete.")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.rstrip("/").endswith("/v1")
        ):
            raise AdultOmniRouteConfigError("Adult OmniRoute base URL must be a clean /v1 endpoint.")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise AdultOmniRouteConfigError("Non-loopback Adult OmniRoute endpoints require HTTPS.")
        if len(self.base_url) > 512 or len(self.api_key) > 4096:
            raise AdultOmniRouteConfigError("Adult OmniRoute configuration exceeds its size limit.")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "AdultOmniRouteConfig | None":
        if environ is None:
            load_dotenv()
            source = os.environ
        else:
            source = environ
        base_url = source.get("ADULT_OMNIROUTE_BASE_URL", "").strip()
        api_key = source.get("ADULT_OMNIROUTE_API_KEY", "").strip()
        if not base_url and not api_key:
            return None
        if not base_url or not api_key:
            return None
        return cls(base_url=base_url, api_key=api_key)


class OmniRouteChatAdapter:
    """Constructs a configured chat client lazily; Stage 6 never invokes it."""

    def __init__(self, config: AdultOmniRouteConfig, model_id: str):
        if not isinstance(config, AdultOmniRouteConfig):
            raise AdultOmniRouteConfigError("A validated AdultOmniRouteConfig is required.")
        _identifier("model_id", model_id)
        self._config = config
        self._model_id = model_id
        self._client = None

    def build_client(self):
        if self._client is None:
            self._client = build_chat_model(
                api_key=self._config.api_key,
                model=self._model_id,
                base_url=self._config.base_url,
                timeout=30,
                max_retries=0,
            )
        return self._client
