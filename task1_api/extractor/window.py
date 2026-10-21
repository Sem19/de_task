"""Plan partner report date windows from checkpoints and EXTRACT_START_DATE.

This module does not read PostgreSQL or call the API. Incremental coverage
starts at ``ExtractState.next_incremental_start`` (``settled_through + 1``)
when anything has settled. If nothing has settled, it starts at
``loaded_from``, the earliest date of the contiguous loaded prefix, not the
last snapshot's ``last_date_from``.

``EXTRACT_START_DATE`` is the configured initial coverage baseline on the first
run. After a checkpoint exists it is an explicit backfill floor: if it is
earlier than the incremental cursor, this run also requests that earlier range
as consecutive chunks, then continues into incremental dates. A later
``EXTRACT_START_DATE`` cannot skip unprocessed dates; the watermark wins.
The first successful window is that baseline, not proof that every earlier
date was ever loaded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Mapping

from config import (
    ACCOUNT_TZ_OFFSET_HOURS,
    DEFAULT_LOOKBACK_SPAN_DAYS,
    EXTRACT_START_DATE_ENV,
    REPORT_MAX_SPAN_DAYS,
)
from storage.state import ExtractState
from validation.rows import parse_calendar_date

logger = logging.getLogger(__name__)

_ONE_DAY = timedelta(days=1)
_CHUNK_SPAN = timedelta(days=REPORT_MAX_SPAN_DAYS)
_LOOKBACK_SPAN = timedelta(days=DEFAULT_LOOKBACK_SPAN_DAYS)

WINDOW_INCREMENTAL = "incremental"
WINDOW_BACKFILL = "backfill"
WindowKind = Literal["incremental", "backfill"]


class WindowError(ValueError):
    """Raised when extract window inputs are invalid."""


@dataclass(frozen=True)
class ReportWindow:
    """One inclusive partner report request, at most 7 days.

    Attributes:
        date_from: Inclusive start.
        date_to: Inclusive end.
        kind: ``backfill`` for explicit historical override; ``incremental``
            for watermark catch-up, including mutable dates.
    """

    date_from: date
    date_to: date
    kind: WindowKind

    @property
    def inclusive_days(self) -> int:
        """Return how many calendar dates the inclusive window covers.

        Returns:
            ``date_to - date_from + 1``. A single day is ``1``.
        """
        return (self.date_to - self.date_from).days + 1


def account_today(now_utc: datetime) -> date:
    """Return the mock reporting date for an aware UTC instant.

    The mock uses a fixed UTC−7 offset, not ``zoneinfo`` for
    ``America/Los_Angeles``. That label observes DST (UTC−8 in winter);
    the mock does not.

    Args:
        now_utc: Timezone-aware clock instant. Converted to UTC first.

    Returns:
        The calendar date in the mock's fixed UTC−7 offset.

    Raises:
        WindowError: If ``now_utc`` is naive.
    """
    utc = _as_utc(now_utc)
    return (utc + timedelta(hours=ACCOUNT_TZ_OFFSET_HOURS)).date()


def parse_extract_start_date(value: str | None) -> date | None:
    """Parse ``EXTRACT_START_DATE``, or return ``None`` if unset.

    Args:
        value: Raw env value. Blank is treated as unset.

    Returns:
        The calendar date, or ``None`` when ``value`` is ``None`` or blank.

    Raises:
        WindowError: If the value is not a calendar ``YYYY-MM-DD``.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return parse_calendar_date(text)
    except ValueError as exc:
        raise WindowError(f"invalid {EXTRACT_START_DATE_ENV}: {text!r}") from exc


def load_extract_start_date(env: Mapping[str, str]) -> date | None:
    """Read ``EXTRACT_START_DATE`` from an environment mapping.

    Args:
        env: Environment mapping. A missing key is treated as unset.

    Returns:
        The calendar date, or ``None`` when the variable is missing or blank.

    Raises:
        WindowError: If the value is not a calendar ``YYYY-MM-DD``.
    """
    return parse_extract_start_date(env.get(EXTRACT_START_DATE_ENV))


def plan_extract_windows(
    *,
    now_utc: datetime,
    state: ExtractState | None = None,
    extract_start_date: date | None = None,
) -> tuple[ReportWindow, ...]:
    """Return gap-free report chunks from coverage start through account today.

    Args:
        now_utc: Timezone-aware clock used to compute ``account_today``.
        state: Last committed checkpoint, or ``None`` on the first run.
        extract_start_date: First-run baseline, and later an earlier-only
            backfill override. Must not skip dates after the watermark.

    Returns:
        Oldest-first chunks of at most 7 inclusive days. Empty when the
        incremental cursor is already past ``account_today``.
    """
    today = account_today(now_utc)
    incremental_start = _incremental_start(state, extract_start_date, today)
    start = _coverage_start(incremental_start, extract_start_date)
    if start > today:
        logger.info("No report windows account_today=%s start=%s", today, start)
        return ()
    windows = _windows_for_range(start, today, incremental_start)
    logger.info(
        "Planned %s report window(s) account_today=%s start=%s end=%s",
        len(windows),
        today.isoformat(),
        start.isoformat(),
        today.isoformat(),
    )
    return tuple(windows)


def _incremental_start(
    state: ExtractState | None,
    extract_start_date: date | None,
    today: date,
) -> date:
    """Return the first date that incremental coverage must include.

    With no checkpoint, that is ``extract_start_date`` or the default
    lookback ending on ``today``. With a checkpoint, it is
    ``next_incremental_start`` when anything has settled, otherwise
    ``loaded_from``.

    Args:
        state: Last committed checkpoint, or ``None`` on the first run.
        extract_start_date: Optional configured baseline.
        today: Mock reporting date for this run.

    Returns:
        Inclusive start of the incremental portion of the plan.
    """
    if state is None:
        if extract_start_date is not None:
            return extract_start_date
        return today - _LOOKBACK_SPAN
    if state.next_incremental_start is not None:
        return state.next_incremental_start
    return state.loaded_from


def _coverage_start(incremental_start: date, extract_start_date: date | None) -> date:
    """Return the oldest date this run should request.

    An ``extract_start_date`` earlier than ``incremental_start`` pulls the
    plan backward for a backfill. A later or missing value leaves the
    incremental cursor in charge.

    Args:
        incremental_start: First date the watermark still needs.
        extract_start_date: Optional backfill floor.

    Returns:
        The earlier of the two dates, or ``incremental_start`` when the
        override is absent or not earlier.
    """
    if extract_start_date is None or extract_start_date >= incremental_start:
        return incremental_start
    return extract_start_date


def _windows_for_range(
    start: date,
    today: date,
    incremental_start: date,
) -> list[ReportWindow]:
    """Split ``start`` through ``today`` into backfill and incremental chunks.

    Dates before ``incremental_start`` are ``backfill``. The rest, through
    ``today``, are ``incremental``.

    Args:
        start: Oldest date to request.
        today: Inclusive end of the plan.
        incremental_start: First date classified as incremental.

    Returns:
        Oldest-first windows.
    """
    if start < incremental_start:
        backfill_end = incremental_start - _ONE_DAY
        return _inclusive_chunks(
            start, backfill_end, WINDOW_BACKFILL
        ) + _inclusive_chunks(incremental_start, today, WINDOW_INCREMENTAL)
    return _inclusive_chunks(start, today, WINDOW_INCREMENTAL)


def _inclusive_chunks(start: date, end: date, kind: WindowKind) -> list[ReportWindow]:
    """Split an inclusive range into chunks of at most seven days.

    Args:
        start: Inclusive first date.
        end: Inclusive last date.
        kind: Label applied to every chunk.

    Returns:
        Contiguous, non-overlapping windows. Empty when ``start`` is after
        ``end``.
    """
    if start > end:
        return []
    windows: list[ReportWindow] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + _CHUNK_SPAN, end)
        windows.append(ReportWindow(date_from=cursor, date_to=chunk_end, kind=kind))
        cursor = chunk_end + _ONE_DAY
    return windows


def _as_utc(now_utc: datetime) -> datetime:
    """Convert an aware instant to UTC.

    Args:
        now_utc: Clock instant. Naive values are rejected.

    Returns:
        The same instant in UTC.

    Raises:
        WindowError: If ``now_utc`` has no ``tzinfo``.
    """
    if now_utc.tzinfo is None:
        raise WindowError("now_utc must be timezone-aware")
    return now_utc.astimezone(timezone.utc)
