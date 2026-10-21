"""Unit tests for partner API authentication and account metadata."""

from __future__ import annotations

import json
import unittest

import httpx

from api_client import Account, PartnerAPIClient, PartnerAPIError
from config import ACCOUNT_PATH, ConfigError, OAUTH_TOKEN_PATH, Settings


VALID_SETTINGS = Settings(
    api_base="http://partner.test",
    client_id="acme-de-takehome",
    client_secret="s3cr3t-do-not-commit",
)

ACCOUNT_BODY = {
    "account_id": "acct_5512",
    "timezone": "America/Los_Angeles",
    "currency": "EUR",
    "attribution_window_days": 3,
}


class _API:
    """Minimal mock of the partner token and account endpoints."""

    def __init__(self, expires_in: int = 300) -> None:
        self.expires_in = expires_in
        self.token_calls = 0
        self.account_calls = 0
        self.last_token_body: dict | None = None
        self.last_authorization: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == OAUTH_TOKEN_PATH:
            return self._token(request)
        if request.method == "GET" and request.url.path == ACCOUNT_PATH:
            return self._account(request)
        return httpx.Response(404, json={"detail": {"error": "not_found"}})

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        body = json.loads(request.content)
        self.last_token_body = body
        if (
            body.get("client_id") != VALID_SETTINGS.client_id
            or body.get("client_secret") != VALID_SETTINGS.client_secret
        ):
            return httpx.Response(
                401,
                json={"detail": {"error": "invalid_client"}},
            )
        return httpx.Response(
            200,
            json={
                "access_token": f"tok_{self.token_calls}",
                "token_type": "Bearer",
                "expires_in": self.expires_in,
            },
        )

    def _account(self, request: httpx.Request) -> httpx.Response:
        self.account_calls += 1
        self.last_authorization = request.headers.get("Authorization")
        if self.last_authorization != f"Bearer tok_{self.token_calls}":
            return httpx.Response(
                401,
                json={"detail": {"error": "invalid_token"}},
            )
        return httpx.Response(200, json=ACCOUNT_BODY)


def _client(handler: _API, settings: Settings = VALID_SETTINGS) -> PartnerAPIClient:
    return PartnerAPIClient(settings, transport=httpx.MockTransport(handler))


class SettingsTest(unittest.TestCase):
    def test_from_env_loads_required_values(self) -> None:
        settings = Settings.from_env(
            {
                "API_BASE": "http://localhost:8080/",
                "API_CLIENT_ID": "acme-de-takehome",
                "API_CLIENT_SECRET": "s3cr3t-do-not-commit",
            }
        )
        self.assertEqual(settings.api_base, "http://localhost:8080")
        self.assertEqual(settings.client_id, "acme-de-takehome")
        self.assertEqual(settings.client_secret, "s3cr3t-do-not-commit")

    def test_missing_client_id_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env({"API_CLIENT_SECRET": "secret"})
        self.assertIn("API_CLIENT_ID", str(ctx.exception))

    def test_missing_client_secret_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env({"API_CLIENT_ID": "acme"})
        self.assertIn("API_CLIENT_SECRET", str(ctx.exception))

    def test_blank_credentials_raise(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env(
                {
                    "API_CLIENT_ID": "  ",
                    "API_CLIENT_SECRET": "secret",
                }
            )


class PartnerAPIClientTest(unittest.TestCase):
    def test_successful_authentication(self) -> None:
        api = _API()
        with _client(api) as client:
            token = client.get_token()
        self.assertEqual(token, "tok_1")
        self.assertEqual(api.token_calls, 1)
        self.assertEqual(api.last_token_body, {
            "client_id": VALID_SETTINGS.client_id,
            "client_secret": VALID_SETTINGS.client_secret,
            "grant_type": "client_credentials",
        })

    def test_invalid_credentials(self) -> None:
        api = _API()
        settings = Settings(
            api_base=VALID_SETTINGS.api_base,
            client_id="wrong",
            client_secret="wrong",
        )
        with _client(api, settings) as client:
            with self.assertRaises(PartnerAPIError) as ctx:
                client.get_token()
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.error, "invalid_client")
        self.assertNotIn("wrong", str(ctx.exception))

    def test_token_is_reused_while_fresh(self) -> None:
        api = _API(expires_in=300)
        with _client(api) as client:
            first = client.get_token()
            second = client.get_token()
        self.assertEqual(first, second)
        self.assertEqual(api.token_calls, 1)

    def test_token_is_refreshed_before_expiry(self) -> None:
        api = _API(expires_in=30)
        settings = Settings(
            api_base=VALID_SETTINGS.api_base,
            client_id=VALID_SETTINGS.client_id,
            client_secret=VALID_SETTINGS.client_secret,
            token_refresh_margin_s=30,
        )
        with _client(api, settings) as client:
            first = client.get_token()
            second = client.get_token()
        self.assertEqual(first, "tok_1")
        self.assertEqual(second, "tok_2")
        self.assertEqual(api.token_calls, 2)

    def test_get_account_returns_metadata(self) -> None:
        api = _API()
        with _client(api) as client:
            account = client.get_account()
        self.assertEqual(
            account,
            Account(
                account_id="acct_5512",
                timezone="America/Los_Angeles",
                currency="EUR",
                attribution_window_days=3,
            ),
        )
        self.assertEqual(api.account_calls, 1)
        self.assertEqual(api.last_authorization, "Bearer tok_1")

    def test_get_account_reuses_cached_token(self) -> None:
        api = _API()
        with _client(api) as client:
            client.get_account()
            client.get_account()
        self.assertEqual(api.token_calls, 1)
        self.assertEqual(api.account_calls, 2)

    def test_close_releases_http_client(self) -> None:
        api = _API()
        client = _client(api)
        self.assertFalse(client.is_closed)
        client.get_token()
        client.close()
        self.assertTrue(client.is_closed)

    def test_context_manager_closes_client(self) -> None:
        api = _API()
        with _client(api) as client:
            client.get_token()
            self.assertFalse(client.is_closed)
        self.assertTrue(client.is_closed)


if __name__ == "__main__":
    unittest.main()
