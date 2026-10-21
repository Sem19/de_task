"""HTTP client for the partner Traffic & Revenue API."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from config import (
    ACCOUNT_PATH,
    GRANT_TYPE,
    OAUTH_TOKEN_PATH,
    Settings,
)

logger = logging.getLogger(__name__)


class PartnerAPIError(Exception):
    """Raised when the partner API returns an error or an unexpected payload.

    Attributes:
        status_code: HTTP status, or ``None`` for transport failures.
        error: Partner error code when present in the response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error


@dataclass(frozen=True)
class Account:
    """Account metadata from ``GET /v2/account``.

    Attributes:
        account_id: Partner account identifier.
        timezone: Reporting timezone label returned by the API.
        currency: Reporting currency code returned by the API.
        attribution_window_days: Number of trailing dates that still restate.
    """

    account_id: str
    timezone: str
    currency: str
    attribution_window_days: int


class PartnerAPIClient:
    """Client for OAuth and account metadata.

    Reuses a single ``httpx.Client``. Obtain an instance, use it, then call
    :meth:`close` or use the client as a context manager.

    Args:
        settings: API origin, credentials, and timeouts.
        transport: Optional httpx transport. Used by tests to mock HTTP.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._http = httpx.Client(
            base_url=settings.api_base,
            timeout=settings.request_timeout_s,
            transport=transport,
        )
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def __enter__(self) -> PartnerAPIClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def is_closed(self) -> bool:
        """Whether the underlying HTTP connection pool has been closed."""
        return self._http.is_closed

    def close(self) -> None:
        """Close the shared ``httpx.Client``."""
        self._http.close()

    def get_token(self) -> str:
        """Return a valid bearer token, refreshing when it is close to expiry.

        Returns:
            Cached or newly issued access token.

        Raises:
            PartnerAPIError: If the token request fails or the body is invalid.
        """
        if self._access_token and self._token_is_fresh():
            return self._access_token
        return self._fetch_token()

    def get_account(self) -> Account:
        """Fetch account reporting metadata.

        Returns:
            Currency, timezone, and attribution window from the partner.

        Raises:
            PartnerAPIError: If authentication or the account request fails.
        """
        token = self.get_token()
        logger.info("GET %s", ACCOUNT_PATH)
        response = self._send(
            "GET",
            ACCOUNT_PATH,
            headers={"Authorization": f"Bearer {token}"},
        )
        self._ensure_success(response, ACCOUNT_PATH)
        return self._parse_account(response)

    def _token_is_fresh(self) -> bool:
        refresh_at = self._token_expires_at - self._settings.token_refresh_margin_s
        return time.monotonic() < refresh_at

    def _fetch_token(self) -> str:
        logger.info("POST %s", OAUTH_TOKEN_PATH)
        response = self._send(
            "POST",
            OAUTH_TOKEN_PATH,
            json={
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "grant_type": GRANT_TYPE,
            },
        )
        self._ensure_success(response, OAUTH_TOKEN_PATH)
        token, expires_in = self._parse_token(response)
        self._access_token = token
        self._token_expires_at = time.monotonic() + expires_in
        logger.info("Obtained access token expires_in=%s", expires_in)
        return token

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise PartnerAPIError(f"{method} {path} failed: {exc}") from exc

    def _ensure_success(self, response: httpx.Response, path: str) -> None:
        if response.is_success:
            return
        error = _error_code(response)
        status = response.status_code
        suffix = f" {error}" if error else ""
        raise PartnerAPIError(
            f"{response.request.method} {path} failed: HTTP {status}{suffix}",
            status_code=status,
            error=error,
        )

    def _parse_token(self, response: httpx.Response) -> tuple[str, int]:
        body = _json_object(response, OAUTH_TOKEN_PATH)
        token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not isinstance(token, str) or not token:
            raise PartnerAPIError(
                "POST /oauth/token returned no access_token",
                status_code=response.status_code,
            )
        if not isinstance(expires_in, int) or isinstance(expires_in, bool):
            raise PartnerAPIError(
                "POST /oauth/token returned invalid expires_in",
                status_code=response.status_code,
            )
        return token, expires_in

    def _parse_account(self, response: httpx.Response) -> Account:
        body = _json_object(response, ACCOUNT_PATH)
        try:
            account_id = body["account_id"]
            timezone = body["timezone"]
            currency = body["currency"]
            window = body["attribution_window_days"]
        except KeyError as exc:
            raise PartnerAPIError(
                f"GET {ACCOUNT_PATH} missing field {exc.args[0]!r}",
                status_code=response.status_code,
            ) from exc
        if not isinstance(account_id, str) or not account_id:
            raise PartnerAPIError("GET /v2/account returned invalid account_id")
        if not isinstance(timezone, str) or not timezone:
            raise PartnerAPIError("GET /v2/account returned invalid timezone")
        if not isinstance(currency, str) or not currency:
            raise PartnerAPIError("GET /v2/account returned invalid currency")
        if not isinstance(window, int) or isinstance(window, bool) or window < 0:
            raise PartnerAPIError(
                "GET /v2/account returned invalid attribution_window_days",
            )
        logger.info(
            "Fetched account_id=%s currency=%s timezone=%s attribution_window_days=%s",
            account_id,
            currency,
            timezone,
            window,
        )
        return Account(
            account_id=account_id,
            timezone=timezone,
            currency=currency,
            attribution_window_days=window,
        )


def _json_object(response: httpx.Response, path: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PartnerAPIError(
            f"{response.request.method} {path} returned non-JSON body",
            status_code=response.status_code,
        ) from exc
    if not isinstance(payload, dict):
        raise PartnerAPIError(
            f"{response.request.method} {path} returned a non-object JSON body",
            status_code=response.status_code,
        )
    return payload


def _error_code(response: httpx.Response) -> str | None:
    """Read the partner error code from either response envelope."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail", payload)
    if isinstance(detail, dict):
        error = detail.get("error")
        return str(error) if error is not None else None
    if isinstance(detail, str) and detail:
        return detail
    error = payload.get("error")
    return str(error) if error is not None else None
