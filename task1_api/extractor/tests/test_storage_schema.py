"""Integration tests for warehouse schema verification and migrations."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.errors import NotNullViolation

from config import MOCK_ACCOUNT_TIMEZONE
from storage import SchemaError, apply_migrations, connection, verify_schema
from storage.schema import column_default, table_columns
from tests.test_storage_connection import (
    integration_dsn,
    postgres_available,
    settings_with_dsn,
)

WAREHOUSE_DIR = Path(__file__).resolve().parents[2] / "warehouse"
INIT_SQL_PATH = WAREHOUSE_DIR / "init.sql"
MIGRATION_SQL_PATH = WAREHOUSE_DIR / "migrations" / "001_add_reporting_timezone.sql"
TEST_SCHEMA = "storage_foundation_test"
PROBE_COUNTRY = "__storage_probe__"

# Pre-timezone snapshot used only to test migration of existing volumes.
LEGACY_DDL = """
CREATE TABLE partner_report (
    report_date     date        NOT NULL,
    country         text        NOT NULL,
    channel         text        NOT NULL,
    impressions     bigint      NOT NULL,
    clicks          bigint      NOT NULL,
    installs        bigint      NOT NULL,
    signups         bigint      NOT NULL,
    payers          bigint      NOT NULL,
    spend_minor     bigint      NOT NULL,
    revenue_minor   bigint      NOT NULL,
    currency        text        NOT NULL,
    extracted_at    timestamptz NOT NULL,
    data_version    bigint,
    PRIMARY KEY (report_date, country, channel)
);
CREATE TABLE extract_state (
    key         text        PRIMARY KEY,
    value       jsonb       NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
)
"""


def _statements(script: str) -> list[str]:
    """Split a SQL script into statements, dropping ``--`` comment lines.

    Args:
        script: Warehouse DDL. Statements are separated by semicolons.

    Returns:
        Non-empty statements with surrounding whitespace removed.
    """
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def _exec_script(conn: psycopg.Connection, script: str) -> None:
    """Execute each statement in ``script``. Does not commit.

    Args:
        conn: Open connection.
        script: SQL script split by :func:`_statements`.
    """
    for statement in _statements(script):
        conn.execute(statement)


def _reset_schema(conn: psycopg.Connection, schema: str) -> None:
    """Drop and recreate ``schema``, then commit.

    Args:
        conn: Open connection.
        schema: Isolated schema name.
    """
    ident = sql.Identifier(schema)
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(ident))
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
    conn.commit()


def _set_search_path(conn: psycopg.Connection, schema: str) -> None:
    """Point the session search path at ``schema``."""
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))


def _insert_probe(
    conn: psycopg.Connection,
    schema: str,
    *,
    include_timezone: bool,
) -> None:
    """Insert one probe row, with or without ``reporting_timezone``.

    Args:
        conn: Open connection. The insert is not committed here.
        schema: Warehouse schema.
        include_timezone: When false, omit the column so a legacy table can
            accept the row.
    """
    table = sql.Identifier(schema, "partner_report")
    extracted_at = datetime.now(timezone.utc)
    if include_timezone:
        conn.execute(
            sql.SQL(
                """
                INSERT INTO {} (
                    report_date, country, channel,
                    impressions, clicks, installs, signups, payers,
                    spend_minor, revenue_minor, currency, reporting_timezone,
                    extracted_at, data_version
                ) VALUES (
                    %s, %s, %s, 1, 0, 0, 0, 0, 101473, -250,
                    'EUR', %s, %s, 42
                )
                """
            ).format(table),
            (
                date(2099, 1, 1),
                PROBE_COUNTRY,
                "search",
                MOCK_ACCOUNT_TIMEZONE,
                extracted_at,
            ),
        )
        return
    conn.execute(
        sql.SQL(
            """
            INSERT INTO {} (
                report_date, country, channel,
                impressions, clicks, installs, signups, payers,
                spend_minor, revenue_minor, currency,
                extracted_at, data_version
            ) VALUES (
                %s, %s, %s, 1, 0, 0, 0, 0, 101473, -250,
                'EUR', %s, 42
            )
            """
        ).format(table),
        (date(2099, 1, 1), PROBE_COUNTRY, "search", extracted_at),
    )


class WarehouseSqlContractTest(unittest.TestCase):
    """Unit tests against SQL files in the repo (no PostgreSQL)."""

    def test_init_sql_is_the_authoritative_create(self) -> None:
        text = INIT_SQL_PATH.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS partner_report", text)
        self.assertIn("reporting_timezone text", text)
        self.assertIn("PRIMARY KEY (report_date, country, channel)", text)
        self.assertIn("spend_minor     bigint", text)
        self.assertNotIn("DEFAULT 'America/Los_Angeles'", text)

    def test_migration_sql_matches_mock_timezone_contract(self) -> None:
        text = MIGRATION_SQL_PATH.read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS reporting_timezone", text)
        self.assertIn(MOCK_ACCOUNT_TIMEZONE, text)
        self.assertIn("SET NOT NULL", text)
        self.assertIn("DROP DEFAULT", text)
        self.assertNotIn("CREATE TABLE", text)
        self.assertNotIn("DROP TABLE", text)


@unittest.skipUnless(postgres_available(), "PostgreSQL is not available")
class StorageSchemaIntegrationTest(unittest.TestCase):
    """Schema tests in an isolated schema; public tables are not dropped."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = settings_with_dsn(integration_dsn())
        cls.init_sql = INIT_SQL_PATH.read_text(encoding="utf-8")

    def setUp(self) -> None:
        with connection(self.settings) as conn:
            _reset_schema(conn, TEST_SCHEMA)

    def tearDown(self) -> None:
        with connection(self.settings) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(TEST_SCHEMA)
                )
            )
            conn.commit()

    def test_fresh_database_setup_from_init_sql(self) -> None:
        with connection(self.settings) as conn:
            _set_search_path(conn, TEST_SCHEMA)
            _exec_script(conn, self.init_sql)
            conn.commit()
            verify_schema(conn, schema=TEST_SCHEMA)
            self.assertIsNone(
                column_default(
                    conn, "partner_report", "reporting_timezone", schema=TEST_SCHEMA
                )
            )
            with self.assertRaises(NotNullViolation):
                _insert_probe(conn, TEST_SCHEMA, include_timezone=False)
            conn.rollback()
            _insert_probe(conn, TEST_SCHEMA, include_timezone=True)
            conn.commit()

    def test_migration_of_existing_schema(self) -> None:
        with connection(self.settings) as conn:
            _set_search_path(conn, TEST_SCHEMA)
            _exec_script(conn, LEGACY_DDL)
            _insert_probe(conn, TEST_SCHEMA, include_timezone=False)
            conn.commit()
            with self.assertRaises(SchemaError) as ctx:
                verify_schema(conn, schema=TEST_SCHEMA)
            self.assertIn("reporting_timezone", str(ctx.exception))
        apply_migrations(self.settings, schema=TEST_SCHEMA)
        apply_migrations(self.settings, schema=TEST_SCHEMA)
        with connection(self.settings) as conn:
            verify_schema(conn, schema=TEST_SCHEMA)
            row = conn.execute(
                sql.SQL(
                    """
                    SELECT spend_minor, revenue_minor, currency,
                           reporting_timezone, data_version
                    FROM {}
                    WHERE country = %s
                    """
                ).format(sql.Identifier(TEST_SCHEMA, "partner_report")),
                (PROBE_COUNTRY,),
            ).fetchone()
            self.assertEqual(
                row,
                (101473, -250, "EUR", MOCK_ACCOUNT_TIMEZONE, 42),
            )
            self.assertIsNone(
                column_default(
                    conn, "partner_report", "reporting_timezone", schema=TEST_SCHEMA
                )
            )
            with self.assertRaises(NotNullViolation):
                conn.execute(
                    sql.SQL(
                        """
                        INSERT INTO {} (
                            report_date, country, channel,
                            impressions, clicks, installs, signups, payers,
                            spend_minor, revenue_minor, currency,
                            extracted_at, data_version
                        ) VALUES (
                            %s, %s, %s, 0, 0, 0, 0, 0, 0, 0, 'EUR', %s, 1
                        )
                        """
                    ).format(sql.Identifier(TEST_SCHEMA, "partner_report")),
                    (date(2099, 1, 2), "GB", "display", datetime.now(timezone.utc)),
                )

    def test_repeated_migration_preserves_rows(self) -> None:
        with connection(self.settings) as conn:
            _set_search_path(conn, TEST_SCHEMA)
            _exec_script(conn, LEGACY_DDL)
            _insert_probe(conn, TEST_SCHEMA, include_timezone=False)
            conn.commit()
        apply_migrations(self.settings, schema=TEST_SCHEMA)
        apply_migrations(self.settings, schema=TEST_SCHEMA)
        with connection(self.settings) as conn:
            count = conn.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE country = %s").format(
                    sql.Identifier(TEST_SCHEMA, "partner_report")
                ),
                (PROBE_COUNTRY,),
            ).fetchone()
        self.assertEqual(count, (1,))

    def test_schema_compatibility_failure_does_not_drop_tables(self) -> None:
        with connection(self.settings) as conn:
            _set_search_path(conn, TEST_SCHEMA)
            conn.execute(
                """
                CREATE TABLE partner_report (
                    country text PRIMARY KEY
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE extract_state (
                    key text PRIMARY KEY,
                    value jsonb NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            conn.commit()
            with self.assertRaises(SchemaError):
                verify_schema(conn, schema=TEST_SCHEMA)
            remaining = table_columns(conn, "partner_report", schema=TEST_SCHEMA)
            self.assertIn("country", remaining)

    def test_verify_schema_does_not_commit_loader_transaction(self) -> None:
        with connection(self.settings) as conn:
            _set_search_path(conn, TEST_SCHEMA)
            _exec_script(conn, self.init_sql)
            conn.commit()
            _insert_probe(conn, TEST_SCHEMA, include_timezone=True)
            verify_schema(conn, schema=TEST_SCHEMA)
            conn.rollback()
            count = conn.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE country = %s").format(
                    sql.Identifier(TEST_SCHEMA, "partner_report")
                ),
                (PROBE_COUNTRY,),
            ).fetchone()
        self.assertEqual(count, (0,))

    def test_apply_migrations_does_not_commit_loader_transaction(self) -> None:
        with connection(self.settings) as setup:
            _set_search_path(setup, TEST_SCHEMA)
            _exec_script(setup, LEGACY_DDL)
            _insert_probe(setup, TEST_SCHEMA, include_timezone=False)
            setup.commit()
        with connection(self.settings) as loader:
            loader.execute(
                sql.SQL("INSERT INTO {} (key, value) VALUES (%s, %s::jsonb)").format(
                    sql.Identifier(TEST_SCHEMA, "extract_state")
                ),
                ("pending_job", '{"report_id": "rep_1"}'),
            )
            apply_migrations(self.settings, schema=TEST_SCHEMA)
            loader.rollback()
            state = loader.execute(
                sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier(TEST_SCHEMA, "extract_state")
                )
            ).fetchone()
            report = loader.execute(
                sql.SQL("SELECT reporting_timezone FROM {} WHERE country = %s").format(
                    sql.Identifier(TEST_SCHEMA, "partner_report")
                ),
                (PROBE_COUNTRY,),
            ).fetchone()
        self.assertEqual(state, (0,))
        self.assertEqual(report, (MOCK_ACCOUNT_TIMEZONE,))


@unittest.skipUnless(postgres_available(), "PostgreSQL is not available")
class ComposeVolumeMigrationTest(unittest.TestCase):
    """Upgrade the live Compose volume without dropping reporting data."""

    def test_public_schema_migration_is_idempotent(self) -> None:
        settings = settings_with_dsn(integration_dsn())
        apply_migrations(settings)
        apply_migrations(settings)
        with connection(settings) as conn:
            verify_schema(conn)
            self.assertIsNone(
                column_default(conn, "partner_report", "reporting_timezone")
            )
