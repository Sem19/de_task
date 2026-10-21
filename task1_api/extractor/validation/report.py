"""Whole-report integrity checks on a completed partner snapshot."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from api_client import Account, Report, ReportRows
from config import REPORT_STATUS_DONE

from .exceptions import ReportValidationError
from .models import NormalizedRow, ValidatedReport
from .rows import normalize_rows, parse_calendar_date


def validate_report(
    report: Report,
    rows: ReportRows,
    account: Account,
    date_from: str,
    date_to: str,
) -> ValidatedReport:
    """Validate a downloaded snapshot before it is loaded into PostgreSQL.

    Completeness is ``DONE`` plus matching counts, no truncation, unique
    grain keys, and dates inside the requested window. HTTP 200 is not
    enough. Invalid rows raise ``RowValidationError``; the function never
    returns a half-valid snapshot.

    Args:
        report: Status payload after polling reached a terminal state.
        rows: Concatenated pages and partner ``meta``.
        account: Account currency, timezone, and attribution window.
        date_from: Inclusive requested start, ``YYYY-MM-DD``.
        date_to: Inclusive requested end, ``YYYY-MM-DD``.

    Returns:
        Immutable normalized rows plus metadata needed for loading.

    Raises:
        ReportValidationError: If the snapshot is incomplete or inconsistent.
        RowValidationError: If any row fails normalization.
    """
    window_from, window_to = _requested_window(date_from, date_to)
    _require_done(report)
    _require_complete(report, rows)
    _require_currency(account, rows)
    _require_timezone(account, rows)
    restated_through = _restated_through(rows, account)
    normalized = normalize_rows(rows.rows)
    _require_unique_grain(normalized)
    _require_dates_in_window(normalized, window_from, window_to)
    return ValidatedReport(
        report_id=report.report_id,
        account_id=account.account_id,
        date_from=window_from,
        date_to=window_to,
        currency=rows.currency,
        timezone=rows.timezone,
        data_version=rows.data_version,
        restated_through=restated_through,
        attribution_window_days=account.attribution_window_days,
        rows=normalized,
    )


def _requested_window(date_from: str, date_to: str) -> tuple[date, date]:
    """Parse the inclusive window the caller asked the partner for.

    Args:
        date_from: Inclusive start, ``YYYY-MM-DD``.
        date_to: Inclusive end, ``YYYY-MM-DD``.

    Returns:
        The start and end dates.

    Raises:
        ReportValidationError: If either value is not a calendar date, or
            ``date_to`` is before ``date_from``. Error code ``invalid_window``.
    """
    try:
        start = parse_calendar_date(date_from)
        end = parse_calendar_date(date_to)
    except ValueError as exc:
        raise ReportValidationError(
            f"requested window {date_from!r}..{date_to!r} is invalid",
            error="invalid_window",
        ) from exc
    if end < start:
        raise ReportValidationError(
            f"date_to {date_to} is before date_from {date_from}",
            error="invalid_window",
        )
    return start, end


def _require_done(report: Report) -> None:
    """Reject a job that has not reached ``DONE``.

    Args:
        report: Status payload.

    Raises:
        ReportValidationError: If ``status`` is not ``DONE``. Error code
            ``not_done``.
    """
    if report.status != REPORT_STATUS_DONE:
        raise ReportValidationError(
            f"report {report.report_id!r} status is {report.status!r}, expected DONE",
            error="not_done",
        )


def _require_complete(report: Report, rows: ReportRows) -> None:
    """Reject a truncated snapshot or disagreeing row counts.

    Fetched length, page ``total``, and ``report.row_count`` must all match.

    Args:
        report: ``DONE`` status payload.
        rows: Downloaded pages.

    Raises:
        ReportValidationError: Error code ``truncated`` or
            ``inconsistent_total``.
    """
    if rows.truncated:
        raise ReportValidationError(
            f"report {report.report_id!r} is truncated",
            error="truncated",
        )
    fetched = len(rows.rows)
    if report.row_count is None:
        raise ReportValidationError(
            f"report {report.report_id!r} is DONE but row_count is missing",
            error="inconsistent_total",
        )
    if fetched != rows.total or fetched != report.row_count:
        raise ReportValidationError(
            f"report {report.report_id!r} count mismatch: "
            f"fetched={fetched} total={rows.total} row_count={report.row_count}",
            error="inconsistent_total",
        )


def _require_currency(account: Account, rows: ReportRows) -> None:
    """Reject a snapshot whose currency differs from the account.

    Args:
        account: Account metadata.
        rows: Downloaded pages.

    Raises:
        ReportValidationError: Error code ``currency_mismatch``.
    """
    if rows.currency != account.currency:
        raise ReportValidationError(
            f"report currency {rows.currency!r} does not match "
            f"account currency {account.currency!r}",
            error="currency_mismatch",
        )


def _require_timezone(account: Account, rows: ReportRows) -> None:
    """Reject a snapshot whose timezone differs from the account.

    Args:
        account: Account metadata.
        rows: Downloaded pages.

    Raises:
        ReportValidationError: Error code ``timezone_mismatch``.
    """
    if rows.timezone != account.timezone:
        raise ReportValidationError(
            f"report timezone {rows.timezone!r} does not match "
            f"account timezone {account.timezone!r}",
            error="timezone_mismatch",
        )


def _restated_through(rows: ReportRows, account: Account) -> date:
    """Parse the oldest still-mutable date; do not treat it as settled.

    The mock sets ``restated_through = account_today - (window - 1)``.
    Dates on or after that value may still move. This check only verifies
    the metadata is a calendar date and the account window is usable. It
    does not require ``restated_through`` to fall inside the requested range.

    Args:
        rows: Downloaded pages. ``restated_through`` is read from metadata.
        account: Account whose attribution window must be at least one day.

    Returns:
        The oldest date that is still mutable.

    Raises:
        ReportValidationError: Error code ``invalid_restated_through`` when
            the attribution window is below 1 or the date is not a calendar
            date.
    """
    if account.attribution_window_days < 1:
        raise ReportValidationError(
            "attribution_window_days must be >= 1",
            error="invalid_restated_through",
        )
    try:
        return parse_calendar_date(rows.restated_through)
    except ValueError as exc:
        raise ReportValidationError(
            f"restated_through {rows.restated_through!r} is not a valid date",
            error="invalid_restated_through",
        ) from exc


def _require_unique_grain(rows: Sequence[NormalizedRow]) -> None:
    """Reject two rows that share ``(report_date, country, channel)``.

    Args:
        rows: Normalized snapshot in page order.

    Raises:
        ReportValidationError: Error code ``duplicate_grain``.
    """
    seen: dict[tuple[date, str, str], int] = {}
    for index, row in enumerate(rows):
        previous = seen.get(row.grain)
        if previous is not None:
            raise ReportValidationError(
                f"duplicate grain {row.grain!r} at rows {previous} and {index}",
                error="duplicate_grain",
            )
        seen[row.grain] = index


def _require_dates_in_window(
    rows: Sequence[NormalizedRow],
    date_from: date,
    date_to: date,
) -> None:
    """Reject a row date outside the requested inclusive window.

    Args:
        rows: Normalized snapshot.
        date_from: Inclusive start.
        date_to: Inclusive end.

    Raises:
        ReportValidationError: Error code ``date_out_of_range``.
    """
    for index, row in enumerate(rows):
        if row.report_date < date_from or row.report_date > date_to:
            raise ReportValidationError(
                f"row {index} date {row.report_date.isoformat()} is outside "
                f"{date_from.isoformat()}..{date_to.isoformat()}",
                error="date_out_of_range",
            )
