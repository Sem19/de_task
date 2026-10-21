"""Compare a validated partner snapshot to PostgreSQL without float money.

Helpers are test-only. They reuse production types and do not ingest data.
Comparison is always against one ``report_id`` / ``data_version`` snapshot,
never against a newly generated mutable report.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from collections.abc import Sequence

import psycopg
from psycopg import sql

from api_client import PartnerAPIClient
from storage import apply_snapshot, connection
from validation import ValidatedReport, validate_report
from validation.models import NormalizedRow

Grain = tuple[date, str, str]

METRIC_FIELDS = (
    "impressions",
    "clicks",
    "installs",
    "signups",
    "payers",
    "spend_minor",
    "revenue_minor",
)
IDENTITY_FIELDS = ("currency", "timezone", "data_version")
COMPARED_FIELDS = METRIC_FIELDS + IDENTITY_FIELDS

_STORED_COLUMNS = (
    "report_date",
    "country",
    "channel",
    *METRIC_FIELDS,
    "currency",
    "reporting_timezone",
    "data_version",
)


@dataclass(frozen=True)
class StoredRow:
    """One ``partner_report`` row used for grain-level comparison."""

    report_date: date
    country: str
    channel: str
    impressions: int
    clicks: int
    installs: int
    signups: int
    payers: int
    spend_minor: int
    revenue_minor: int
    currency: str
    timezone: str
    data_version: int

    @property
    def grain(self) -> Grain:
        """Return ``(report_date, country, channel)``."""
        return (self.report_date, self.country, self.channel)


@dataclass(frozen=True)
class FieldMismatch:
    """One business field that differs for a grain."""

    grain: Grain
    field: str
    expected: object
    actual: object


@dataclass(frozen=True)
class Reconciliation:
    """Counts from comparing one snapshot to stored rows."""

    report_id: str
    data_version: int
    date_from: date
    date_to: date
    source_rows: int
    stored_rows: int
    missing: tuple[Grain, ...]
    extra: tuple[Grain, ...]
    duplicates: tuple[Grain, ...]
    mismatches: tuple[FieldMismatch, ...]

    @property
    def ok(self) -> bool:
        """Return True when the stored slice matches the snapshot exactly."""
        return not (self.missing or self.extra or self.duplicates or self.mismatches)

    def summary(self) -> str:
        """Return a one-line count summary for assertions and logs."""
        return (
            f"report_id={self.report_id} data_version={self.data_version} "
            f"window={self.date_from.isoformat()}..{self.date_to.isoformat()} "
            f"source={self.source_rows} stored={self.stored_rows} "
            f"missing={len(self.missing)} extra={len(self.extra)} "
            f"duplicates={len(self.duplicates)} mismatched={len(self.mismatches)}"
        )


def fetch_validated_snapshot(
    client: PartnerAPIClient,
    date_from: str,
    date_to: str,
) -> ValidatedReport:
    """Download one completed report and validate it with production checks."""
    account = client.get_account()
    submitted = client.submit_report(date_from, date_to)
    done = client.wait_for_report(submitted.report_id)
    rows = client.fetch_rows(done.report_id)
    return validate_report(done, rows, account, date_from, date_to)


def commit_snapshot(
    settings,
    snapshot: ValidatedReport,
    *,
    schema: str,
    extracted_at: datetime | None = None,
) -> None:
    """Load a snapshot with ``apply_snapshot`` and commit data with state."""
    loaded_at = extracted_at or datetime.now(timezone.utc)
    with connection(settings) as conn:
        apply_snapshot(conn, snapshot, extracted_at=loaded_at, schema=schema)
        conn.commit()


def fetch_stored_rows(
    conn: psycopg.Connection,
    *,
    schema: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[StoredRow, ...]:
    """Return stored partner_report rows as typed integer records."""
    table = sql.Identifier(schema, "partner_report")
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in _STORED_COLUMNS)
    query = sql.SQL("SELECT {} FROM {}").format(columns, table)
    params: list[object] = []
    if date_from is not None and date_to is not None:
        query = sql.SQL(
            "SELECT {} FROM {} WHERE report_date >= %s AND report_date <= %s"
        ).format(columns, table)
        params.extend((date_from, date_to))
    query = query + sql.SQL(" ORDER BY report_date, country, channel")
    fetched = conn.execute(query, params).fetchall()
    return tuple(_stored_row(item) for item in fetched)


def reconcile(
    snapshot: ValidatedReport,
    stored: Sequence[StoredRow],
) -> Reconciliation:
    """Compare PostgreSQL to the same ``report_id`` snapshot.

    Money and counts are compared as integers. ``extracted_at`` is ignored.
    Duplicate stored grains are reported instead of being silently merged.
    """
    expected_map: dict[Grain, dict[str, object]] = {}
    for row in snapshot.rows:
        grain, values = _expected_values(row, snapshot)
        expected_map[grain] = values
    counts = Counter(row.grain for row in stored)
    duplicates = tuple(sorted(grain for grain, n in counts.items() if n > 1))
    actual_map: dict[Grain, dict[str, object]] = {}
    for row in stored:
        if counts[row.grain] == 1:
            actual_map[row.grain] = _stored_values(row)
    expected_keys = set(expected_map)
    actual_keys = set(actual_map)
    mismatches: list[FieldMismatch] = []
    for grain in sorted(expected_keys & actual_keys):
        left = expected_map[grain]
        right = actual_map[grain]
        for field in COMPARED_FIELDS:
            if left[field] != right[field]:
                mismatches.append(
                    FieldMismatch(
                        grain=grain,
                        field=field,
                        expected=left[field],
                        actual=right[field],
                    )
                )
    return Reconciliation(
        report_id=snapshot.report_id,
        data_version=snapshot.data_version,
        date_from=snapshot.date_from,
        date_to=snapshot.date_to,
        source_rows=len(snapshot.rows),
        stored_rows=len(stored),
        missing=tuple(sorted(expected_keys - actual_keys)),
        extra=tuple(sorted(actual_keys - expected_keys)),
        duplicates=duplicates,
        mismatches=tuple(mismatches),
    )


def _expected_values(
    row: NormalizedRow, snapshot: ValidatedReport
) -> tuple[Grain, dict[str, object]]:
    """Return the grain and compared fields expected from one source row.

    Args:
        row: Normalized snapshot row.
        snapshot: Source of currency, timezone, and ``data_version``.

    Returns:
        The reporting grain and the integer fields used in comparison.

    Raises:
        TypeError: If a compared metric is not an ``int``.
    """
    values = {
        "impressions": _require_int(row.impressions, "impressions"),
        "clicks": _require_int(row.clicks, "clicks"),
        "installs": _require_int(row.installs, "installs"),
        "signups": _require_int(row.signups, "signups"),
        "payers": _require_int(row.payers, "payers"),
        "spend_minor": _require_int(row.spend_minor, "spend_minor"),
        "revenue_minor": _require_int(row.revenue_minor, "revenue_minor"),
        "currency": snapshot.currency,
        "timezone": snapshot.timezone,
        "data_version": _require_int(snapshot.data_version, "data_version"),
    }
    return row.grain, values


def _stored_values(row: StoredRow) -> dict[str, object]:
    """Return the compared fields of one warehouse row.

    Raises:
        TypeError: If a compared metric is not an ``int``.
    """
    return {
        "impressions": _require_int(row.impressions, "impressions"),
        "clicks": _require_int(row.clicks, "clicks"),
        "installs": _require_int(row.installs, "installs"),
        "signups": _require_int(row.signups, "signups"),
        "payers": _require_int(row.payers, "payers"),
        "spend_minor": _require_int(row.spend_minor, "spend_minor"),
        "revenue_minor": _require_int(row.revenue_minor, "revenue_minor"),
        "currency": row.currency,
        "timezone": row.timezone,
        "data_version": _require_int(row.data_version, "data_version"),
    }


def _stored_row(item: tuple[object, ...]) -> StoredRow:
    """Build a :class:`StoredRow` from one ``partner_report`` tuple.

    Args:
        item: Selected columns in comparison order.

    Returns:
        A typed warehouse row.

    Raises:
        TypeError: If ``data_version`` is null or a metric is not an ``int``.
    """
    (
        report_date,
        country,
        channel,
        impressions,
        clicks,
        installs,
        signups,
        payers,
        spend_minor,
        revenue_minor,
        currency,
        timezone_name,
        data_version,
    ) = item
    if data_version is None:
        raise TypeError("stored data_version is null")
    return StoredRow(
        report_date=report_date,  # type: ignore[arg-type]
        country=str(country),
        channel=str(channel),
        impressions=_require_int(impressions, "impressions"),
        clicks=_require_int(clicks, "clicks"),
        installs=_require_int(installs, "installs"),
        signups=_require_int(signups, "signups"),
        payers=_require_int(payers, "payers"),
        spend_minor=_require_int(spend_minor, "spend_minor"),
        revenue_minor=_require_int(revenue_minor, "revenue_minor"),
        currency=str(currency),
        timezone=str(timezone_name),
        data_version=_require_int(data_version, "data_version"),
    )


def _require_int(value: object, field: str) -> int:
    """Return ``value`` when it is an ``int`` and not a ``bool``.

    Args:
        value: Candidate metric.
        field: Name included in the error.

    Returns:
        The same integer.

    Raises:
        TypeError: If ``value`` is a boolean or any non-``int``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int, got {type(value).__name__}")
    return value
