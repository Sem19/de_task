"""Explicit warehouse migrations. Never run as part of a loader transaction.

The extractor container only mounts ``task1_api/extractor`` (see docker-compose).
``warehouse/migrations/*.sql`` is therefore not readable at runtime. This module
applies the same idempotent steps as
``warehouse/migrations/001_add_reporting_timezone.sql``.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import sql

from config import MOCK_ACCOUNT_TIMEZONE, Settings, WAREHOUSE_SCHEMA

from .connection import StorageError, connection
from .schema import column_default, table_columns

logger = logging.getLogger(__name__)


def apply_migrations(
    settings: Settings,
    *,
    schema: str = WAREHOUSE_SCHEMA,
) -> None:
    """Apply pending warehouse DDL on a dedicated connection.

    Opens its own connection so a loader transaction on another connection
    cannot be committed. Does not create or drop ``partner_report`` /
    ``extract_state``; missing tables are left for ``verify_schema`` to reject.

    Args:
        settings: Runtime settings including ``pg_dsn``.
        schema: Warehouse schema. Defaults to ``public``.

    Raises:
        StorageError: If a migration statement fails.
    """
    with connection(settings) as conn:
        try:
            if not _reporting_timezone_pending(conn, schema):
                logger.info("No warehouse migrations pending schema=%s", schema)
                return
            logger.info("Applying reporting_timezone migration schema=%s", schema)
            _add_reporting_timezone(conn, schema)
            conn.commit()
        except psycopg.Error as exc:
            conn.rollback()
            raise StorageError("failed to apply warehouse migrations") from exc


def _reporting_timezone_pending(conn: psycopg.Connection, schema: str) -> bool:
    """Return whether ``partner_report.reporting_timezone`` still needs DDL.

    A missing table is not pending; ``verify_schema`` rejects that later.
    A missing column, a nullable column, or a leftover default is pending.

    Args:
        conn: Open PostgreSQL connection.
        schema: Warehouse schema.

    Returns:
        ``True`` when the column must be added or tightened.
    """
    columns = table_columns(conn, "partner_report", schema=schema)
    if not columns:
        return False
    found = columns.get("reporting_timezone")
    if found is None:
        return True
    _data_type, is_nullable = found
    if is_nullable:
        return True
    return (
        column_default(conn, "partner_report", "reporting_timezone", schema=schema)
        is not None
    )


def _add_reporting_timezone(conn: psycopg.Connection, schema: str) -> None:
    """Add ``reporting_timezone``, backfill it, and drop the default.

    Existing nulls are set to ``MOCK_ACCOUNT_TIMEZONE``. The statements are
    not committed here.

    Args:
        conn: Open PostgreSQL connection.
        schema: Warehouse schema.
    """
    table = sql.Identifier(schema, "partner_report")
    conn.execute(
        sql.SQL(
            "ALTER TABLE {} ADD COLUMN IF NOT EXISTS reporting_timezone text"
        ).format(table)
    )
    conn.execute(
        sql.SQL(
            "UPDATE {} SET reporting_timezone = %s " "WHERE reporting_timezone IS NULL"
        ).format(table),
        (MOCK_ACCOUNT_TIMEZONE,),
    )
    conn.execute(
        sql.SQL("ALTER TABLE {} ALTER COLUMN reporting_timezone SET NOT NULL").format(
            table
        )
    )
    conn.execute(
        sql.SQL("ALTER TABLE {} ALTER COLUMN reporting_timezone DROP DEFAULT").format(
            table
        )
    )
