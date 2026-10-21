"""Unit tests for configuration, OAuth, account metadata, and client lifecycle."""

from __future__ import annotations

import unittest

from api_client import Account, PartnerAPIClient, PartnerAPIError
from config import ConfigError, Settings

from tests.helpers import VALID_SETTINGS, MockAPI, make_client


class SettingsTest(unittest.TestCase):
    """Settings loaded from an environment mapping."""

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
    """OAuth, account metadata, and client lifecycle against MockAPI."""

    def test_successful_authentication(self) -> None:
        api = MockAPI()
        with make_client(api) as client:
            token = client.get_token()
        self.assertEqual(token, "tok_1")
        self.assertEqual(api.token_calls, 1)
        self.assertEqual(
            api.last_token_body,
            {
                "client_id": VALID_SETTINGS.client_id,
                "client_secret": VALID_SETTINGS.client_secret,
                "grant_type": "client_credentials",
            },
        )

    def test_invalid_credentials(self) -> None:
        api = MockAPI()
        settings = Settings(
            api_base=VALID_SETTINGS.api_base,
            client_id="wrong",
            client_secret="wrong",
        )
        with make_client(api, settings) as client:
            with self.assertRaises(PartnerAPIError) as ctx:
                client.get_token()
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.error, "invalid_client")
        self.assertNotIn("wrong", str(ctx.exception))

    def test_token_is_reused_while_fresh(self) -> None:
        api = MockAPI(expires_in=300)
        with make_client(api) as client:
            first = client.get_token()
            second = client.get_token()
        self.assertEqual(first, second)
        self.assertEqual(api.token_calls, 1)

    def test_token_is_refreshed_before_expiry(self) -> None:
        api = MockAPI(expires_in=30)
        settings = Settings(
            api_base=VALID_SETTINGS.api_base,
            client_id=VALID_SETTINGS.client_id,
            client_secret=VALID_SETTINGS.client_secret,
            token_refresh_margin_s=30,
        )
        with make_client(api, settings) as client:
            first = client.get_token()
            second = client.get_token()
        self.assertEqual(first, "tok_1")
        self.assertEqual(second, "tok_2")
        self.assertEqual(api.token_calls, 2)

    def test_get_account_returns_metadata(self) -> None:
        api = MockAPI()
        with make_client(api) as client:
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
        api = MockAPI()
        with make_client(api) as client:
            client.get_account()
            client.get_account()
        self.assertEqual(api.token_calls, 1)
        self.assertEqual(api.account_calls, 2)

    def test_close_releases_http_client(self) -> None:
        api = MockAPI()
        client = make_client(api)
        self.assertFalse(client.is_closed)
        client.get_token()
        client.close()
        self.assertTrue(client.is_closed)

    def test_context_manager_closes_client(self) -> None:
        api = MockAPI()
        with make_client(api) as client:
            client.get_token()
            self.assertFalse(client.is_closed)
        self.assertTrue(client.is_closed)


class PublicInterfaceTest(unittest.TestCase):
    """Package exports stay aligned with the client module."""

    def test_package_exports_match_previous_module(self) -> None:
        import api_client

        self.assertIs(api_client.PartnerAPIClient, PartnerAPIClient)
        for name in (
            "Account",
            "PartnerAPIError",
            "Report",
            "ReportNotFoundError",
            "ReportPaginationError",
            "ReportRows",
            "ReportStatusError",
            "ReportTimeoutError",
        ):
            self.assertTrue(hasattr(api_client, name), name)
