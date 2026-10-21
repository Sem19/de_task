"""Extractor configuration and partner API constants."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

OAUTH_TOKEN_PATH = "/oauth/token"
ACCOUNT_PATH = "/v2/account"

DEFAULT_API_BASE = "http://mock-api:8080"
GRANT_TYPE = "client_credentials"
REQUEST_TIMEOUT_S = 30.0
TOKEN_REFRESH_MARGIN_S = 30


class ConfigError(Exception):
    """Raised when required environment configuration is missing or empty."""


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from the environment.

    Attributes:
        api_base: Partner API origin, without a trailing slash.
        client_id: OAuth client id.
        client_secret: OAuth client secret.
        request_timeout_s: httpx timeout applied to every request.
        token_refresh_margin_s: Refresh the bearer token this many seconds
            before ``expires_in`` elapses.
    """

    api_base: str
    client_id: str
    client_secret: str
    request_timeout_s: float = REQUEST_TIMEOUT_S
    token_refresh_margin_s: int = TOKEN_REFRESH_MARGIN_S

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from environment variables.

        Args:
            env: Mapping to read. Defaults to ``os.environ``.

        Returns:
            Populated settings.

        Raises:
            ConfigError: If ``API_CLIENT_ID`` or ``API_CLIENT_SECRET`` is missing.
        """
        source = os.environ if env is None else env
        api_base = _optional(source, "API_BASE", DEFAULT_API_BASE).rstrip("/")
        return cls(
            api_base=api_base,
            client_id=_require(source, "API_CLIENT_ID"),
            client_secret=_require(source, "API_CLIENT_SECRET"),
        )


def _optional(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    if value is None or not str(value).strip():
        return default
    return str(value).strip()


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not str(value).strip():
        raise ConfigError(f"missing required environment variable: {name}")
    return str(value).strip()
