"""Durable extract_state checkpoints for partner_report.

Watermarks advance only after ``replace_date_slice`` succeeds on the same
connection. This module does not commit; the caller commits data and state
together, or rolls both back.

The first successful window is the coverage baseline. Later snapshots extend
``loaded_through`` / ``settled_through`` only when they adjoin or overlap that
contiguous prefix. Historical backfills rewrite earlier dates in place and do
not jump the incremental cursor. Orchestration owns ``EXTRACT_START_DATE``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import psycopg
from psycopg import sql
from psycopg.types.json import Json

from config import EXTRACT_STATE_KEY, WAREHOUSE_SCHEMA
from validation.models import ValidatedReport
from validation.rows import parse_calendar_date

from .connection import StorageError
from .loader import LoadResult, replace_date_slice

logger = logging.getLogger(__name__)

_ONE_DAY = timedelta(days=1)


class StateConflictError(StorageError):
    """Raised when account currency or timezone disagrees with the checkpoint."""


@dataclass(frozen=True)
class ExtractState:
    """Checkpoint stored in ``extract_state`` for ``partner_report``.

    Attributes:
        loaded_through: Inclusive end of the contiguous loaded prefix,
            including mutable days. The first successful window sets this
            baseline. A later island does not jump it across a gap.
            ``None`` before the first success.
        loaded_from: Inclusive start of that same contiguous prefix.
            Later chunks and historical islands with a gap do not move it
            forward. Used to replan when nothing has settled yet.
        settled_through: Inclusive end of immutable dates in that same
            contiguous prefix. Next incremental start is the following day.
            Historical backfills do not move this cursor forward.
        currency: Account currency frozen after the first commit.
        reporting_timezone: Account timezone frozen after the first commit.
        account_id: Partner account from the last successful snapshot.
        last_report_id: Partner job id of the last committed snapshot.
        last_extracted_at: Load timestamp of the last committed snapshot.
        last_data_version: Partner ``data_version`` of that snapshot.
        last_restated_through: Oldest mutable date from that snapshot.
        last_date_from: Inclusive window start of that snapshot.
        last_date_to: Inclusive window end of that snapshot.
    """

    loaded_through: date | None
    loaded_from: date
    settled_through: date | None
    currency: str
    reporting_timezone: str
    account_id: str
    last_report_id: str
    last_extracted_at: datetime
    last_data_version: int
    last_restated_through: date
    last_date_from: date
    last_date_to: date

    @property
    def next_incremental_start(self) -> date | None:
        """Return ``settled_through + 1 day``, or ``None`` if never settled."""
        if self.settled_through is None:
            return None
        return self.settled_through + _ONE_DAY


def read_state(
    conn: psycopg.Connection,
    *,
    schema: str = WAREHOUSE_SCHEMA,
) -> ExtractState | None:
    """Return the stored checkpoint, or ``None`` if this is the first run.

    Args:
        conn: Open PostgreSQL connection.
        schema: Warehouse schema. Defaults to ``public``.
    """
    table = sql.Identifier(schema, "extract_state")
    row = conn.execute(
        sql.SQL("SELECT value FROM {} WHERE key = %s").format(table),
        (EXTRACT_STATE_KEY,),
    ).fetchone()
    if row is None:
        return None
    return _from_payload(row[0])


def apply_snapshot(
    conn: psycopg.Connection,
    snapshot: ValidatedReport,
    *,
    extracted_at: datetime,
    schema: str = WAREHOUSE_SCHEMA,
) -> tuple[LoadResult, ExtractState]:
    """Replace the date slice and write the checkpoint. Does not commit.

    Args:
        conn: Open load connection. Must not autocommit.
        snapshot: Integrity-checked report.
        extracted_at: Timezone-aware load timestamp.
        schema: Warehouse schema. Defaults to ``public``.

    Returns:
        Load counts and the pending checkpoint. Durable only after the
        caller commits. This function does not log a successful commit.

    Raises:
        StateConflictError: If currency or timezone changed versus state.
        StorageError: If the slice replace or state write fails.
    """
    current = read_state(conn, schema=schema)
    _reject_identity_change(current, snapshot)
    load_result = replace_date_slice(
        conn, snapshot, extracted_at=extracted_at, schema=schema
    )
    next_state = _next_state(current, snapshot, extracted_at)
    _write_state(conn, next_state, schema=schema)
    logger.info(
        "Pending extract_state write (uncommitted) report_id=%s "
        "loaded_through=%s settled_through=%s next_start=%s",
        next_state.last_report_id,
        _iso(next_state.loaded_through),
        _iso(next_state.settled_through),
        _iso(next_state.next_incremental_start),
    )
    return load_result, next_state


def _reject_identity_change(
    current: ExtractState | None, snapshot: ValidatedReport
) -> None:
    if current is None:
        return
    if snapshot.currency != current.currency:
        raise StateConflictError(
            f"account currency changed from {current.currency!r} "
            f"to {snapshot.currency!r}"
        )
    if snapshot.timezone != current.reporting_timezone:
        raise StateConflictError(
            f"account timezone changed from {current.reporting_timezone!r} "
            f"to {snapshot.timezone!r}"
        )


def _next_state(
    current: ExtractState | None,
    snapshot: ValidatedReport,
    extracted_at: datetime,
) -> ExtractState:
    return ExtractState(
        loaded_through=_next_loaded_through(current, snapshot),
        loaded_from=_next_loaded_from(current, snapshot),
        settled_through=_next_settled_through(current, snapshot),
        currency=snapshot.currency if current is None else current.currency,
        reporting_timezone=(
            snapshot.timezone if current is None else current.reporting_timezone
        ),
        account_id=snapshot.account_id,
        last_report_id=snapshot.report_id,
        last_extracted_at=extracted_at,
        last_data_version=snapshot.data_version,
        last_restated_through=snapshot.restated_through,
        last_date_from=snapshot.date_from,
        last_date_to=snapshot.date_to,
    )


def _next_loaded_through(
    current: ExtractState | None, snapshot: ValidatedReport
) -> date | None:
    """Extend the contiguous coverage prefix, or keep the previous end.

    The first successful window becomes the baseline even if earlier dates
    were never loaded.
    """
    previous = None if current is None else current.loaded_through
    if not _extends_coverage(current, snapshot):
        return previous
    if previous is None:
        return snapshot.date_to
    return max(previous, snapshot.date_to)


def _next_loaded_from(
    current: ExtractState | None, snapshot: ValidatedReport
) -> date:
    """Return the start of contiguous loaded coverage.

    The first window is the baseline. A later adjoining chunk keeps the
    earliest start. A gapped historical island does not pretend the hole
    was loaded.
    """
    if current is None:
        return snapshot.date_from
    previous_start = current.loaded_from
    previous_end = current.loaded_through
    if previous_end is None:
        return min(previous_start, snapshot.date_from)
    if _overlaps_or_adjoins(
        previous_start, previous_end, snapshot.date_from, snapshot.date_to
    ):
        return min(previous_start, snapshot.date_from)
    return previous_start


def _overlaps_or_adjoins(
    prefix_start: date, prefix_end: date, date_from: date, date_to: date
) -> bool:
    return date_from <= prefix_end + _ONE_DAY and date_to >= prefix_start - _ONE_DAY


def _next_settled_through(
    current: ExtractState | None, snapshot: ValidatedReport
) -> date | None:
    """Settle only immutable dates that adjoin already-processed settlement.

    Dates ``>= restated_through`` stay mutable. A gap after
    ``settled_through`` does not skip. When there is no checkpoint yet, the
    current window is the baseline.
    """
    previous = None if current is None else current.settled_through
    immutable_end = snapshot.restated_through - _ONE_DAY
    chunk_settled = min(snapshot.date_to, immutable_end)
    if chunk_settled < snapshot.date_from:
        return previous
    if not _contiguous_with_settled(previous, snapshot.date_from):
        return previous
    if previous is None:
        return chunk_settled
    return max(previous, chunk_settled)


def _extends_coverage(current: ExtractState | None, snapshot: ValidatedReport) -> bool:
    if current is None or current.loaded_through is None:
        return True
    if _contiguous_with_settled(current.loaded_through, snapshot.date_from):
        return True
    if current.settled_through is None:
        return False
    return _contiguous_with_settled(current.settled_through, snapshot.date_from)


def _contiguous_with_settled(previous: date | None, date_from: date) -> bool:
    """Return True if ``date_from`` adjoins or overlaps ``previous``.

    ``previous is None`` means no checkpoint yet, so the current window is
    accepted as the initial baseline.
    """
    if previous is None:
        return True
    return date_from <= previous + _ONE_DAY


def _write_state(conn: psycopg.Connection, state: ExtractState, *, schema: str) -> None:
    table = sql.Identifier(schema, "extract_state")
    try:
        conn.execute(
            sql.SQL(
                """
                INSERT INTO {} (key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """
            ).format(table),
            (EXTRACT_STATE_KEY, Json(_to_payload(state)), state.last_extracted_at),
        )
    except psycopg.Error as exc:
        raise StorageError("failed to write extract_state") from exc


def _to_payload(state: ExtractState) -> dict[str, object]:
    extracted_at = state.last_extracted_at
    if extracted_at.tzinfo is None:
        raise StorageError("last_extracted_at must be timezone-aware")
    return {
        "loaded_through": _iso(state.loaded_through),
        "loaded_from": state.loaded_from.isoformat(),
        "settled_through": _iso(state.settled_through),
        "currency": state.currency,
        "reporting_timezone": state.reporting_timezone,
        "account_id": state.account_id,
        "last_report_id": state.last_report_id,
        "last_extracted_at": extracted_at.isoformat(),
        "last_data_version": state.last_data_version,
        "last_restated_through": state.last_restated_through.isoformat(),
        "last_date_from": state.last_date_from.isoformat(),
        "last_date_to": state.last_date_to.isoformat(),
    }


def _from_payload(payload: dict[str, object]) -> ExtractState:
    extracted_raw = str(payload["last_extracted_at"])
    extracted_at = datetime.fromisoformat(extracted_raw)
    loaded_from_raw = payload.get("loaded_from")
    if loaded_from_raw is None:
        loaded_from = parse_calendar_date(str(payload["last_date_from"]))
    else:
        loaded_from = parse_calendar_date(str(loaded_from_raw))
    return ExtractState(
        loaded_through=_optional_date(payload.get("loaded_through")),
        loaded_from=loaded_from,
        settled_through=_optional_date(payload.get("settled_through")),
        currency=str(payload["currency"]),
        reporting_timezone=str(payload["reporting_timezone"]),
        account_id=str(payload["account_id"]),
        last_report_id=str(payload["last_report_id"]),
        last_extracted_at=extracted_at,
        last_data_version=int(payload["last_data_version"]),
        last_restated_through=parse_calendar_date(
            str(payload["last_restated_through"])
        ),
        last_date_from=parse_calendar_date(str(payload["last_date_from"])),
        last_date_to=parse_calendar_date(str(payload["last_date_to"])),
    )


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    return parse_calendar_date(str(value))


def _iso(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
