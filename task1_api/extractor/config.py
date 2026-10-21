"""Extractor configuration and partner API constants."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

OAUTH_TOKEN_PATH = "/oauth/token"
ACCOUNT_PATH = "/v2/account"
REPORTS_PATH = "/v2/reports"

DEFAULT_API_BASE = "http://mock-api:8080"
GRANT_TYPE = "client_credentials"
REQUEST_TIMEOUT_S = 30.0
TOKEN_REFRESH_MARGIN_S = 30

# Inclusive window is 7 days, i.e. date_to - date_from <= 6.
REPORT_MAX_SPAN_DAYS = 6
REPORT_POLL_INTERVAL_S = 1.0
REPORT_POLL_TIMEOUT_S = 60.0

# The mock silently caps page size at 1000; requesting 5000 would skip rows.
REPORT_PAGE_LIMIT = 1000
REPORT_ORDER_BY = "date,country,channel"

REPORT_DIMENSIONS = ("date", "country", "channel")
REPORT_METRICS = (
    "impressions",
    "clicks",
    "installs",
    "signups",
    "payers",
    "spend",
    "revenue",
)

REPORT_STATUS_PENDING = "PENDING"
REPORT_STATUS_RUNNING = "RUNNING"
REPORT_STATUS_DONE = "DONE"
REPORT_STATUSES_IN_PROGRESS = frozenset({REPORT_STATUS_PENDING, REPORT_STATUS_RUNNING})
REPORT_STATUSES_FAILED = frozenset({"FAILED", "ERROR", "CANCELLED"})


def report_status_path(report_id: str) -> str:
    """Return ``GET /v2/reports/{report_id}``."""
    return f"{REPORTS_PATH}/{report_id}"


def report_rows_path(report_id: str) -> str:
    """Return ``GET /v2/reports/{report_id}/rows``."""
    return f"{REPORTS_PATH}/{report_id}/rows"


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
        poll_interval_s: Delay between report status polls.
        poll_timeout_s: Maximum time to wait for a report to reach ``DONE``.
    """

    api_base: str
    client_id: str
    client_secret: str
    request_timeout_s: float = REQUEST_TIMEOUT_S
    token_refresh_margin_s: int = TOKEN_REFRESH_MARGIN_S
    poll_interval_s: float = REPORT_POLL_INTERVAL_S
    poll_timeout_s: float = REPORT_POLL_TIMEOUT_S

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
