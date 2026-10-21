"""Unit and connection tests for PostgreSQL connectivity."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import psycopg

from config import ConfigError, Settings
from storage import StorageError, connect, connection, safe_dsn_context


def settings_with_dsn(pg_dsn: str) -> Settings:
    """Build settings that only vary by DSN."""
    return Settings(
        api_base="http://partner.test",
        client_id="acme-de-takehome",
        client_secret="unused",
        pg_dsn=pg_dsn,
    )


def integration_dsn() -> str:
    """DSN for the Compose warehouse from the host, or ``PG_DSN`` if set."""
    return os.environ.get("PG_DSN", "postgresql://acme:acme@localhost:5432/acme")


def postgres_available() -> bool:
    """Return True when the warehouse accepts connections."""
    try:
        with psycopg.connect(integration_dsn(), connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


class FakeConnection:
    """Stand-in for psycopg.Connection used by unit tests."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class StorageConfigTest(unittest.TestCase):
    """Unit tests: configuration only, no PostgreSQL."""

    def test_from_env_loads_pg_dsn(self) -> None:
        settings = Settings.from_env(
            {
                "API_CLIENT_ID": "acme-de-takehome",
                "API_CLIENT_SECRET": "s3cr3t-do-not-commit",
                "PG_DSN": "postgresql://u:p@localhost:5432/db",
            }
        )
        self.assertEqual(settings.pg_dsn, "postgresql://u:p@localhost:5432/db")

    def test_missing_pg_dsn_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            connect(settings_with_dsn(""))
        self.assertIn("PG_DSN", str(ctx.exception))

    def test_blank_pg_dsn_raises(self) -> None:
        with self.assertRaises(ConfigError):
            connect(settings_with_dsn("   "))

    def test_dsn_context_omits_password(self) -> None:
        context = safe_dsn_context(
            "postgresql://acme:supersecret@db.internal:5432/acme"
        )
        self.assertEqual(context, "user=acme host=db.internal port=5432 dbname=acme")
        self.assertNotIn("supersecret", context)
        self.assertNotIn("postgresql://", context)


class StorageConnectionUnitTest(unittest.TestCase):
    """Unit tests: mocked connections and refused endpoints, no warehouse."""

    def test_connection_failure(self) -> None:
        settings = settings_with_dsn(
            "postgresql://acme:supersecret@127.0.0.1:1/acme?connect_timeout=1"
        )
        with self.assertRaises(StorageError) as ctx:
            connect(settings)
        message = str(ctx.exception)
        self.assertIn("127.0.0.1", message)
        self.assertNotIn("supersecret", message)

    def test_invalid_dsn_raises(self) -> None:
        with self.assertRaises(StorageError):
            connect(settings_with_dsn("not-a-valid-dsn"))

    @patch("storage.connection.psycopg.connect", return_value=FakeConnection())
    def test_connection_cleanup(self, _mock_connect: object) -> None:
        settings = settings_with_dsn("postgresql://acme:acme@localhost:5432/acme")
        with connection(settings) as conn:
            self.assertFalse(conn.closed)
        self.assertTrue(conn.closed)

    @patch("storage.connection.psycopg.connect", return_value=FakeConnection())
    def test_connection_cleanup_after_error(self, _mock_connect: object) -> None:
        settings = settings_with_dsn("postgresql://acme:acme@localhost:5432/acme")
        with self.assertRaises(RuntimeError):
            with connection(settings) as conn:
                raise RuntimeError("boom")
        self.assertTrue(conn.closed)


@unittest.skipUnless(postgres_available(), "PostgreSQL is not available")
class StorageConnectionIntegrationTest(unittest.TestCase):
    """Live connection test against the Compose warehouse."""

    def test_successful_connection(self) -> None:
        settings = settings_with_dsn(integration_dsn())
        with connection(settings) as conn:
            row = conn.execute("SELECT current_database()").fetchone()
            self.assertIsNotNone(row)
        self.assertTrue(conn.closed)
