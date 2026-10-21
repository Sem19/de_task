"""Transactional date-slice replace for validated partner reports.

Writes ``partner_report`` only. Does not commit and does not update
``extract_state``. Pair with ``apply_snapshot`` on the same connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg import sql

from config import WAREHOUSE_SCHEMA
from validation.models import NormalizedRow, ValidatedReport

from .connection import StorageError
from .schema import PARTNER_REPORT_COLUMNS, PARTNER_REPORT_GRAIN

logger = logging.getLogger(__name__)

Grain = tuple[date, str, str]

# Counts, money, and frozen account identity. ``data_version`` and
# ``extracted_at`` are snapshot metadata and are not compared here.
_BUSINESS_FIELDS = (
    "impressions",
    "clicks",
    "installs",
    "signups",
    "payers",
    "spend_minor",
    "revenue_minor",
    "currency",
    "reporting_timezone",
)
_RETURNING = PARTNER_REPORT_GRAIN + _BUSINESS_FIELDS
_GRAIN_LEN = len(PARTNER_REPORT_GRAIN)


@dataclass(frozen=True)
class LoadResult:
    """Counts from a date-slice replace. Does not imply a commit.

    ``deleted`` / ``inserted`` are physical DELETE and COPY row counts.
    ``added`` / ``removed`` / ``updated`` / ``unchanged`` are grain-level
    business changes. ``data_version`` and ``extracted_at`` do not count
    as updates.

    Attributes:
        deleted: Physical rows removed from ``[date_from, date_to]``.
        inserted: Physical rows written from the snapshot.
        added: Grains that did not exist in the slice before this load.
        removed: Grains deleted and not present in the snapshot.
        updated: Grains present in both whose business fields changed.
        unchanged: Grains present in both with identical business fields.
        date_from: Inclusive slice start.
        date_to: Inclusive slice end.
    """

    deleted: int
    inserted: int
    added: int
    removed: int
    updated: int
    unchanged: int
    date_from: date
    date_to: date


def replace_date_slice(
    conn: psycopg.Connection,
    snapshot: ValidatedReport,
    *,
    extracted_at: datetime,
    schema: str = WAREHOUSE_SCHEMA,
) -> LoadResult:
    """Replace ``partner_report`` rows in the snapshot window.

    Runs ``DELETE`` then ``INSERT`` on ``conn`` and does not commit, so a
    later ``extract_state`` write can join this transaction. On failure the
    connection is left aborted; the caller must roll back.

    Args:
        conn: Open PostgreSQL connection. Must not autocommit.
        snapshot: Integrity-checked report. Unvalidated API rows are rejected
            by the type contract.
        extracted_at: Timezone-aware load timestamp stored on every inserted row.
        schema: Warehouse schema. Defaults to ``public``.

    Returns:
        Delete/insert and grain-change counts for this uncommitted replace.
        Not a durability guarantee; the caller logs a successful commit.

    Raises:
        StorageError: If ``extracted_at`` is naive or a statement fails.
    """
    if extracted_at.tzinfo is None:
        raise StorageError("extracted_at must be timezone-aware")
    table = sql.Identifier(schema, "partner_report")
    try:
        previous = _delete_slice(conn, table, snapshot.date_from, snapshot.date_to)
        _insert_rows(conn, table, snapshot, extracted_at)
    except psycopg.Error as exc:
        raise StorageError(
            f"failed to replace partner_report "
            f"{snapshot.date_from.isoformat()}..{snapshot.date_to.isoformat()}"
        ) from exc
    result = _summarize(snapshot, previous)
    logger.info(
        "Pending partner_report replace (uncommitted) report_id=%s "
        "window=%s..%s deleted=%s inserted=%s added=%s removed=%s "
        "updated=%s unchanged=%s",
        snapshot.report_id,
        result.date_from.isoformat(),
        result.date_to.isoformat(),
        result.deleted,
        result.inserted,
        result.added,
        result.removed,
        result.updated,
        result.unchanged,
    )
    return result


def _delete_slice(
    conn: psycopg.Connection,
    table: sql.Identifier,
    date_from: date,
    date_to: date,
) -> list[tuple[Any, ...]]:
    returning = sql.SQL(", ").join(sql.Identifier(name) for name in _RETURNING)
    cursor = conn.execute(
        sql.SQL(
            "DELETE FROM {} WHERE report_date >= %s AND report_date <= %s "
            "RETURNING {}"
        ).format(table, returning),
        (date_from, date_to),
    )
    return list(cursor.fetchall())


def _insert_rows(
    conn: psycopg.Connection,
    table: sql.Identifier,
    snapshot: ValidatedReport,
    extracted_at: datetime,
) -> None:
    if not snapshot.rows:
        return
    columns = sql.SQL(", ").join(
        sql.Identifier(name) for name in PARTNER_REPORT_COLUMNS
    )
    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(table, columns)
    with conn.cursor() as cursor:
        with cursor.copy(copy_sql) as copy:
            for row in snapshot.rows:
                copy.write_row(_copy_tuple(row, snapshot, extracted_at))


def _copy_tuple(
    row: NormalizedRow,
    snapshot: ValidatedReport,
    extracted_at: datetime,
) -> tuple[object, ...]:
    return (
        row.report_date,
        row.country,
        row.channel,
        row.impressions,
        row.clicks,
        row.installs,
        row.signups,
        row.payers,
        row.spend_minor,
        row.revenue_minor,
        snapshot.currency,
        snapshot.timezone,
        extracted_at,
        snapshot.data_version,
    )


def _summarize(
    snapshot: ValidatedReport,
    previous: list[tuple[Any, ...]],
) -> LoadResult:
    old_map = {_row_grain(item): _row_metrics(item) for item in previous}
    new_map = {
        _snapshot_grain(row): _snapshot_metrics(row, snapshot) for row in snapshot.rows
    }
    old_keys = set(old_map)
    new_keys = set(new_map)
    common = old_keys & new_keys
    updated = sum(1 for key in common if old_map[key] != new_map[key])
    return LoadResult(
        deleted=len(previous),
        inserted=len(snapshot.rows),
        added=len(new_keys - old_keys),
        removed=len(old_keys - new_keys),
        updated=updated,
        unchanged=len(common) - updated,
        date_from=snapshot.date_from,
        date_to=snapshot.date_to,
    )


def _row_grain(item: tuple[Any, ...]) -> Grain:
    report_date, country, channel = item[:_GRAIN_LEN]
    return (report_date, country, channel)


def _row_metrics(item: tuple[Any, ...]) -> tuple[Any, ...]:
    return item[_GRAIN_LEN:]


def _snapshot_grain(row: NormalizedRow) -> Grain:
    return row.grain


def _snapshot_metrics(
    row: NormalizedRow, snapshot: ValidatedReport
) -> tuple[object, ...]:
    return (
        row.impressions,
        row.clicks,
        row.installs,
        row.signups,
        row.payers,
        row.spend_minor,
        row.revenue_minor,
        snapshot.currency,
        snapshot.timezone,
    )
