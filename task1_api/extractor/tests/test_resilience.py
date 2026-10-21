"""Unit tests for retries, 429 handling, token recovery, and local rate limits."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

import httpx

from api_client import (
    AmbiguousRequestError,
    PartnerAPIClient,
    PartnerAPIError,
    RetryExhaustedError,
)
from config import ACCOUNT_PATH, OAUTH_TOKEN_PATH, REPORTS_PATH, Settings

from tests.helpers import ACCOUNT_BODY, VALID_SETTINGS, FakeClock


def _settings(**overrides: Any) -> Settings:
    """Return test settings with short retries, plus ``overrides``."""
    values = {
        "api_base": VALID_SETTINGS.api_base,
        "client_id": VALID_SETTINGS.client_id,
        "client_secret": VALID_SETTINGS.client_secret,
        "retry_backoff_s": 0.5,
        "retry_after_buffer_s": 1.0,
        "max_attempts": 3,
    }
    values.update(overrides)
    return Settings(**values)


def _error(
    status: int,
    error: str,
    *,
    nested: bool = False,
    retry_after: str | None = None,
) -> httpx.Response:
    """Build an error response with a flat or nested partner error code.

    Args:
        status: HTTP status.
        error: Partner error code.
        nested: When true, wrap the code in ``{"detail": {"error": ...}}``.
        retry_after: Optional ``Retry-After`` header value.

    Returns:
        An ``httpx.Response`` with no request attached.
    """
    body: dict[str, Any]
    if nested:
        body = {"detail": {"error": error}}
    else:
        body = {"error": error}
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(status, json=body, headers=headers)


class ScriptedAPI:
    """Queue-driven mock for resilience tests."""

    def __init__(self) -> None:
        """Start with empty call and response queues."""
        self.calls: list[tuple[str, str]] = []
        self.token_calls = 0
        self.account_queue: list[Any] = []
        self.report_queue: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Dispatch token, account, and report calls to the scripted queues."""
        self.calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == OAUTH_TOKEN_PATH:
            return self._token(request)
        if request.method == "GET" and request.url.path == ACCOUNT_PATH:
            return self._next(self.account_queue, request, default_account=True)
        if request.method == "POST" and request.url.path == REPORTS_PATH:
            return self._next(self.report_queue, request, default_account=False)
        return httpx.Response(404, json={"error": "not_found"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        """Return a bearer token, or 401 when the client credentials are wrong."""
        self.token_calls += 1
        body = json.loads(request.content)
        if (
            body.get("client_id") != VALID_SETTINGS.client_id
            or body.get("client_secret") != VALID_SETTINGS.client_secret
        ):
            return _error(401, "invalid_client", nested=True)
        return httpx.Response(
            200,
            json={
                "access_token": f"tok_{self.token_calls}",
                "token_type": "Bearer",
                "expires_in": 300,
            },
        )

    def _next(
        self,
        queue: list[Any],
        request: httpx.Request,
        *,
        default_account: bool,
    ) -> httpx.Response:
        """Pop the next scripted result, or return a successful default.

        A queued ``"connect"`` raises :class:`httpx.ConnectError`. A queued
        response is returned as-is.

        Args:
            queue: Account or report script. The first item is consumed.
            request: Request attached to a simulated connect error.
            default_account: When true and the queue is empty, return the
                account body. Otherwise return a pending report.

        Returns:
            The next scripted response, or the default success body.
        """
        if queue:
            item = queue.pop(0)
            if item == "connect":
                raise httpx.ConnectError("temporary", request=request)
            if isinstance(item, httpx.Response):
                return item
        if default_account:
            return httpx.Response(200, json=ACCOUNT_BODY)
        return httpx.Response(
            200,
            json={"report_id": "rep_1", "status": "PENDING"},
        )


def _client(api: ScriptedAPI, settings: Settings | None = None) -> PartnerAPIClient:
    """Return a client whose HTTP transport is ``api``."""
    return PartnerAPIClient(
        settings or _settings(),
        transport=httpx.MockTransport(api),
    )


class ResilienceTest(unittest.TestCase):
    """Retries, 429 handling, token refresh, and the local rate limiter."""

    def test_retries_429_rate_limited_with_retry_after(self) -> None:
        api = ScriptedAPI()
        api.account_queue = [
            _error(429, "rate_limited", retry_after="5"),
            httpx.Response(200, json=ACCOUNT_BODY),
        ]
        with patch("api_client.retry.time.sleep") as sleep:
            with _client(api) as client:
                account = client.get_account()
        self.assertEqual(account.currency, "EUR")
        self.assertEqual(api.calls.count(("GET", ACCOUNT_PATH)), 2)
        sleep.assert_called_once_with(6.0)

    def test_retries_too_many_jobs_nested_error(self) -> None:
        api = ScriptedAPI()
        api.report_queue = [
            _error(429, "too_many_jobs", nested=True, retry_after="5"),
            httpx.Response(200, json={"report_id": "rep_1", "status": "PENDING"}),
        ]
        with patch("api_client.retry.time.sleep") as sleep:
            with _client(api) as client:
                report = client.submit_report("2026-10-01", "2026-10-02")
        self.assertEqual(report.report_id, "rep_1")
        self.assertEqual(api.calls.count(("POST", REPORTS_PATH)), 2)
        sleep.assert_called_once_with(6.0)

    def test_retries_http_500_then_succeeds(self) -> None:
        api = ScriptedAPI()
        api.account_queue = [
            _error(500, "unavailable"),
            httpx.Response(200, json=ACCOUNT_BODY),
        ]
        with patch("api_client.retry.time.sleep") as sleep:
            with _client(api) as client:
                client.get_account()
        self.assertEqual(api.calls.count(("GET", ACCOUNT_PATH)), 2)
        sleep.assert_called_once()

    def test_retries_http_503_then_succeeds(self) -> None:
        api = ScriptedAPI()
        api.account_queue = [
            _error(503, "unavailable"),
            httpx.Response(200, json=ACCOUNT_BODY),
        ]
        with patch("api_client.retry.time.sleep"):
            with _client(api) as client:
                client.get_account()
        self.assertEqual(api.calls.count(("GET", ACCOUNT_PATH)), 2)

    def test_retries_temporary_network_failure(self) -> None:
        api = ScriptedAPI()
        api.account_queue = ["connect", httpx.Response(200, json=ACCOUNT_BODY)]
        with patch("api_client.retry.time.sleep"):
            with _client(api) as client:
                account = client.get_account()
        self.assertEqual(account.account_id, "acct_5512")
        self.assertEqual(api.calls.count(("GET", ACCOUNT_PATH)), 2)

    def test_retry_exhaustion_raises(self) -> None:
        api = ScriptedAPI()
        api.account_queue = [
            _error(500, "unavailable"),
            _error(500, "unavailable"),
            _error(500, "unavailable"),
        ]
        with patch("api_client.retry.time.sleep") as sleep:
            with _client(api) as client:
                with self.assertRaises(RetryExhaustedError) as ctx:
                    client.get_account()
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(api.calls.count(("GET", ACCOUNT_PATH)), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_permanent_http_400_is_not_retried(self) -> None:
        api = ScriptedAPI()
        api.account_queue = [_error(400, "bad_date", nested=True)]
        with patch("api_client.retry.time.sleep") as sleep:
            with _client(api) as client:
                with self.assertRaises(PartnerAPIError) as ctx:
                    client.get_account()
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.error, "bad_date")
        self.assertEqual(api.calls.count(("GET", ACCOUNT_PATH)), 1)
        sleep.assert_not_called()
        self.assertNotIsInstance(ctx.exception, RetryExhaustedError)

    def test_token_expired_refreshes_once_and_retries(self) -> None:
        api = ScriptedAPI()
        api.account_queue = [
            _error(401, "token_expired", nested=True),
            httpx.Response(200, json=ACCOUNT_BODY),
        ]
        with _client(api) as client:
            account = client.get_account()
        self.assertEqual(account.currency, "EUR")
        self.assertEqual(api.token_calls, 2)
        self.assertEqual(api.calls.count(("GET", ACCOUNT_PATH)), 2)

    def test_invalid_token_does_not_retry_forever(self) -> None:
        api = ScriptedAPI()
        api.account_queue = [
            _error(401, "invalid_token", nested=True),
            _error(401, "invalid_token", nested=True),
            _error(401, "invalid_token", nested=True),
        ]
        with _client(api) as client:
            with self.assertRaises(PartnerAPIError) as ctx:
                client.get_account()
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.error, "invalid_token")
        self.assertEqual(api.token_calls, 2)
        self.assertEqual(api.calls.count(("GET", ACCOUNT_PATH)), 2)

    def test_flat_and_nested_error_envelopes(self) -> None:
        api = ScriptedAPI()
        api.account_queue = [_error(400, "bad_range", nested=False)]
        with _client(api) as client:
            with self.assertRaises(PartnerAPIError) as ctx:
                client.get_account()
        self.assertEqual(ctx.exception.error, "bad_range")

        api = ScriptedAPI()
        api.account_queue = [_error(400, "bad_range", nested=True)]
        with _client(api) as client:
            with self.assertRaises(PartnerAPIError) as ctx:
                client.get_account()
        self.assertEqual(ctx.exception.error, "bad_range")

    def test_ambiguous_post_5xx_is_not_retried(self) -> None:
        api = ScriptedAPI()
        api.report_queue = [_error(500, "unavailable")]
        with patch("api_client.retry.time.sleep") as sleep:
            with _client(api) as client:
                with self.assertRaises(AmbiguousRequestError) as ctx:
                    client.submit_report("2026-10-01", "2026-10-02")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(api.calls.count(("POST", REPORTS_PATH)), 1)
        sleep.assert_not_called()

    def test_ambiguous_post_network_error_is_not_retried(self) -> None:
        api = ScriptedAPI()
        api.report_queue = ["connect"]
        with patch("api_client.retry.time.sleep") as sleep:
            with _client(api) as client:
                with self.assertRaises(AmbiguousRequestError):
                    client.submit_report("2026-10-01", "2026-10-02")
        self.assertEqual(api.calls.count(("POST", REPORTS_PATH)), 1)
        sleep.assert_not_called()

    def test_local_v2_rate_limiter(self) -> None:
        api = ScriptedAPI()
        settings = _settings(v2_rate_limit_per_min=2)
        clock = FakeClock()
        with patch("api_client.retry.time.monotonic", clock.monotonic):
            with patch("api_client.retry.time.sleep", clock.sleep):
                with _client(api, settings) as client:
                    client.get_account()
                    client.get_account()
                    client.get_account()
        self.assertGreaterEqual(clock.now, 60.0)
        self.assertEqual(api.calls.count(("GET", ACCOUNT_PATH)), 3)
        self.assertEqual(api.token_calls, 1)
