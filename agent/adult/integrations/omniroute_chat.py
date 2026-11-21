"""Adult-only OmniRoute configuration and lazy ChatOpenAI client construction."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

from agent.model import build_chat_model
from agent.adult.models import _identifier


class AdultOmniRouteConfigError(ValueError):
    """Adult OmniRoute settings are incomplete or unsafe."""


class AdultOmniRouteConfig:
    """Immutable Adult-only configuration with no generic dataclass serialization."""

    __slots__ = ("_base_url", "_api_key")

    def __init__(self, base_url: str, api_key: str):
        if not isinstance(base_url, str) or not isinstance(api_key, str) or not api_key.strip():
            raise AdultOmniRouteConfigError("Adult OmniRoute configuration is incomplete.")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.rstrip("/").endswith("/v1")
        ):
            raise AdultOmniRouteConfigError("Adult OmniRoute base URL must be a clean /v1 endpoint.")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise AdultOmniRouteConfigError("Non-loopback Adult OmniRoute endpoints require HTTPS.")
        if len(base_url) > 512 or len(api_key) > 4096:
            raise AdultOmniRouteConfigError("Adult OmniRoute configuration exceeds its size limit.")
        object.__setattr__(self, "_base_url", base_url)
        object.__setattr__(self, "_api_key", api_key)

    def __setattr__(self, name, value):
        raise AttributeError("AdultOmniRouteConfig is immutable.")

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str:
        return self._api_key

    def __repr__(self) -> str:
        return f"AdultOmniRouteConfig(base_url={self._base_url!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, AdultOmniRouteConfig) and self._base_url == other._base_url

    def __hash__(self) -> int:
        return hash(self._base_url)

    def to_dict(self) -> dict[str, str]:
        """Return a deliberately redacted serializable view."""
        return {"base_url": self._base_url}

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "AdultOmniRouteConfig | None":
        if environ is None:
            load_dotenv()
            source = os.environ
        else:
            source = environ
        base_url = source.get("ADULT_OMNIROUTE_BASE_URL", "")
        api_key = source.get("ADULT_OMNIROUTE_API_KEY", "")
        if not isinstance(base_url, str) or not isinstance(api_key, str):
            raise AdultOmniRouteConfigError("Adult OmniRoute settings must be strings.")
        base_url = base_url.strip()
        api_key = api_key.strip()
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
