"""Read-only warehouse schema compatibility checks.

This module never creates, drops, or alters tables. Fresh databases come from
``warehouse/init.sql``. Existing volumes are upgraded by ``apply_migrations``.
"""

from __future__ import annotations

import logging

import psycopg

from config import WAREHOUSE_SCHEMA

from .connection import StorageError

logger = logging.getLogger(__name__)

PARTNER_REPORT_COLUMNS = {
    "report_date": "date",
    "country": "text",
    "channel": "text",
    "impressions": "bigint",
    "clicks": "bigint",
    "installs": "bigint",
    "signups": "bigint",
    "payers": "bigint",
    "spend_minor": "bigint",
    "revenue_minor": "bigint",
    "currency": "text",
    "reporting_timezone": "text",
    "extracted_at": "timestamp with time zone",
    "data_version": "bigint",
}
PARTNER_REPORT_NULLABLE = frozenset({"data_version"})
PARTNER_REPORT_GRAIN = ("report_date", "country", "channel")
PARTNER_REPORT_NO_DEFAULT = frozenset({"reporting_timezone"})

EXTRACT_STATE_COLUMNS = {
    "key": "text",
    "value": "jsonb",
    "updated_at": "timestamp with time zone",
}
EXTRACT_STATE_NULLABLE: frozenset[str] = frozenset()
EXTRACT_STATE_KEY = ("key",)


class SchemaError(StorageError):
    """Raised when warehouse tables or columns cannot store ValidatedReport."""


def verify_schema(
    conn: psycopg.Connection,
    *,
    schema: str = WAREHOUSE_SCHEMA,
) -> None:
    """Check that landing tables can store a validated snapshot.

    Read-only: does not commit, roll back, create, or drop objects. Safe to
    call inside a loader transaction.

    Args:
        conn: Open PostgreSQL connection.
        schema: Warehouse schema. Defaults to ``public``.

    Raises:
        SchemaError: If a required table, column, type, or primary key
            does not match ``warehouse/init.sql``.
    """
    _require_table(
        conn,
        "partner_report",
        PARTNER_REPORT_COLUMNS,
        PARTNER_REPORT_NULLABLE,
        PARTNER_REPORT_GRAIN,
        schema=schema,
        no_default=PARTNER_REPORT_NO_DEFAULT,
    )
    _require_table(
        conn,
        "extract_state",
        EXTRACT_STATE_COLUMNS,
        EXTRACT_STATE_NULLABLE,
        EXTRACT_STATE_KEY,
        schema=schema,
    )
    logger.info("Warehouse schema %s is compatible with ValidatedReport", schema)


def table_columns(
    conn: psycopg.Connection,
    table: str,
    *,
    schema: str = WAREHOUSE_SCHEMA,
) -> dict[str, tuple[str, bool]]:
    """Return ``{column: (data_type, is_nullable)}`` for one table.

    A missing table is an empty mapping, not an error. ``data_type`` is the
    ``information_schema`` name (``bigint``, ``text``,
    ``timestamp with time zone``).

    Args:
        conn: Open PostgreSQL connection. Only a catalog query is issued.
        table: Unqualified table name.
        schema: Warehouse schema. Defaults to ``public``.

    Returns:
        Column name to ``(data_type, is_nullable)``. ``is_nullable`` is
        ``True`` when the catalog says ``YES``.
    """
    rows = conn.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    ).fetchall()
    return {name: (data_type, nullable == "YES") for name, data_type, nullable in rows}


def table_primary_key(
    conn: psycopg.Connection,
    table: str,
    *,
    schema: str = WAREHOUSE_SCHEMA,
) -> tuple[str, ...]:
    """Return primary-key columns in ``ordinal_position`` order.

    Only ``PRIMARY KEY`` is considered. A missing table or a table with no
    primary key is an empty tuple, not an error.

    Args:
        conn: Open PostgreSQL connection. Only a catalog query is issued.
        table: Unqualified table name.
        schema: Warehouse schema. Defaults to ``public``.

    Returns:
        Column names from left to right. Empty when the table is missing
        or has no primary key.
    """
    rows = conn.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = %s
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (schema, table),
    ).fetchall()
    return tuple(name for (name,) in rows)


def column_default(
    conn: psycopg.Connection,
    table: str,
    column: str,
    *,
    schema: str = WAREHOUSE_SCHEMA,
) -> str | None:
    """Return the catalog default expression, or ``None`` if unset or missing.

    The value is the expression stored by PostgreSQL, such as ``now()``,
    not the value that expression would produce.

    Args:
        conn: Open PostgreSQL connection. Only a catalog query is issued.
        table: Unqualified table name.
        column: Column name.
        schema: Warehouse schema. Defaults to ``public``.

    Returns:
        ``column_default`` as text, or ``None`` when the column has no
        default or the column does not exist.
    """
    row = conn.execute(
        """
        SELECT column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
        """,
        (schema, table, column),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _require_table(
    conn: psycopg.Connection,
    table: str,
    expected: dict[str, str],
    nullable: frozenset[str],
    grain: tuple[str, ...],
    *,
    schema: str,
    no_default: frozenset[str] = frozenset(),
) -> None:
    """Reject a table whose columns or primary key cannot store the contract.

    Args:
        conn: Open PostgreSQL connection. Only catalog queries are issued.
        table: Unqualified table name.
        expected: Required ``{column: data_type}`` mapping.
        nullable: Columns that may be nullable. Every other expected column
            must be ``NOT NULL``.
        grain: Expected primary-key columns in ordinal order.
        schema: Warehouse schema.
        no_default: Columns that must not have a column default.

    Raises:
        SchemaError: If the table is missing, or a column, type, nullability,
            default, or primary key does not match.
    """
    columns = table_columns(conn, table, schema=schema)
    if not columns:
        raise SchemaError(f"table {schema}.{table} does not exist")
    for name, data_type in expected.items():
        found = columns.get(name)
        if found is None:
            raise SchemaError(f"table {schema}.{table} is missing column {name}")
        actual_type, is_nullable = found
        if actual_type != data_type:
            raise SchemaError(
                f"table {schema}.{table} column {name} has type {actual_type}, "
                f"expected {data_type}",
            )
        if name not in nullable and is_nullable:
            raise SchemaError(
                f"table {schema}.{table} column {name} is nullable, "
                "expected NOT NULL",
            )
        if name in no_default and column_default(conn, table, name, schema=schema):
            raise SchemaError(
                f"table {schema}.{table} column {name} has a column default; "
                "new inserts must supply the value explicitly",
            )
    actual_grain = table_primary_key(conn, table, schema=schema)
    if actual_grain != grain:
        raise SchemaError(
            f"table {schema}.{table} primary key is {actual_grain}, "
            f"expected {grain}",
        )
