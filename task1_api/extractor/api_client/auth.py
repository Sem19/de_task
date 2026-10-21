"""OAuth token handling, caching, and account metadata."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx

from config import ACCOUNT_PATH, GRANT_TYPE, OAUTH_TOKEN_PATH

from .exceptions import PartnerAPIError
from .models import Account

if TYPE_CHECKING:
    from .client import PartnerAPIClient

logger = logging.getLogger(__name__)


def obtain_access_token(client: PartnerAPIClient) -> str:
    """Return a cached bearer token, refreshing near expiry.

    Args:
        client: Shared HTTP client that stores token state.

    Returns:
        Cached or newly issued access token.

    Raises:
        PartnerAPIError: If the token request fails or the body is invalid.
    """
    if client._access_token and _token_is_fresh(client):
        return client._access_token
    return _fetch_token(client)


def invalidate_access_token(client: PartnerAPIClient) -> None:
    """Drop the cached bearer token so the next call hits ``/oauth/token``.

    Args:
        client: Client whose token and expiry are cleared. Expiry is set
            to ``0`` so a later freshness check cannot reuse it.
    """
    client._access_token = None
    client._token_expires_at = 0.0


def refresh_access_token(client: PartnerAPIClient) -> str:
    """Discard the cache and obtain a new bearer token.

    Args:
        client: Shared HTTP client that stores token state.

    Returns:
        Newly issued access token.

    Raises:
        PartnerAPIError: If the token request fails or the body is invalid.
    """
    logger.info("Refreshing access token")
    invalidate_access_token(client)
    return _fetch_token(client)


def fetch_account(client: PartnerAPIClient) -> Account:
    """Fetch account reporting metadata.

    Args:
        client: Authenticated HTTP client.

    Returns:
        Currency, timezone, and attribution window from the partner.

    Raises:
        PartnerAPIError: If authentication or the account request fails.
    """
    logger.info("GET %s", ACCOUNT_PATH)
    response = client._authenticated("GET", ACCOUNT_PATH)
    client._ensure_success(response, ACCOUNT_PATH)
    return _parse_account(client, response)


def _token_is_fresh(client: PartnerAPIClient) -> bool:
    """Return whether the cached token is still inside the refresh margin.

    Args:
        client: Client holding the token and its monotonic expiry.

    Returns:
        ``True`` when monotonic time is earlier than expiry minus
        ``token_refresh_margin_s``.
    """
    refresh_at = client._token_expires_at - client._settings.token_refresh_margin_s
    return time.monotonic() < refresh_at


def _fetch_token(client: PartnerAPIClient) -> str:
    """Request a new bearer token and store it on ``client``.

    Args:
        client: HTTP client whose credentials are posted to ``/oauth/token``.

    Returns:
        The issued access token.

    Raises:
        PartnerAPIError: If the request fails or the body is invalid.
    """
    logger.info("POST %s", OAUTH_TOKEN_PATH)
    response = client._send(
        "POST",
        OAUTH_TOKEN_PATH,
        json={
            "client_id": client._settings.client_id,
            "client_secret": client._settings.client_secret,
            "grant_type": GRANT_TYPE,
        },
    )
    client._ensure_success(response, OAUTH_TOKEN_PATH)
    token, expires_in = _parse_token(client, response)
    client._access_token = token
    client._token_expires_at = time.monotonic() + expires_in
    logger.info("Obtained access token expires_in=%s", expires_in)
    return token


def _parse_token(
    client: PartnerAPIClient,
    response: httpx.Response,
) -> tuple[str, int]:
    """Read ``access_token`` and ``expires_in`` from a token response.

    Args:
        client: Client used to decode the JSON object.
        response: Successful ``POST /oauth/token`` response.

    Returns:
        The token string and its lifetime in seconds.

    Raises:
        PartnerAPIError: If either field is missing or the wrong type.
            ``expires_in`` must be an ``int``, not a ``bool``.
    """
    body = client._json_object(response, OAUTH_TOKEN_PATH)
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


def _parse_account(client: PartnerAPIClient, response: httpx.Response) -> Account:
    """Build an :class:`Account` from ``GET /v2/account``.

    Args:
        client: Client used to decode the JSON object.
        response: Successful account response.

    Returns:
        Currency, timezone, and attribution window.

    Raises:
        PartnerAPIError: If a required field is missing or invalid.
    """
    body = client._json_object(response, ACCOUNT_PATH)
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
