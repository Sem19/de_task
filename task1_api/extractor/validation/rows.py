"""Pure validation and normalization of partner report rows."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, NoReturn

from config import (
    POSTGRES_BIGINT_MAX,
    POSTGRES_BIGINT_MIN,
    REPORT_DIMENSIONS,
)

from .exceptions import RowValidationError
from .models import NormalizedRow

_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MONEY_RE = re.compile(r"^-?[0-9]+$")


def parse_calendar_date(value: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` calendar date.

    Args:
        value: Date string from the partner API.

    Returns:
        The calendar date.

    Raises:
        ValueError: If ``value`` is not a valid calendar date.
    """
    if not isinstance(value, str):
        raise ValueError("must be a YYYY-MM-DD string")
    text = value.strip()
    if not _ISO_DATE_RE.fullmatch(text):
        raise ValueError("must be a YYYY-MM-DD string")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("is not a valid calendar date") from exc


def normalize_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[NormalizedRow, ...]:
    """Validate every raw row and return immutable normalized records.

    Stops on the first invalid row. Duplicate grain keys are kept; that is
    a whole-report check, not a per-row check.

    Args:
        rows: Raw objects from ``GET /v2/reports/{id}/rows``.

    Returns:
        Normalized rows in input order.

    Raises:
        RowValidationError: If any row is missing fields or has invalid types.
    """
    return tuple(normalize_row(row, index=index) for index, row in enumerate(rows))


def normalize_row(
    row: Mapping[str, Any],
    *,
    index: int | None = None,
) -> NormalizedRow:
    """Validate one raw API row and return a typed record.

    Args:
        row: One object from a report page.
        index: Zero-based position in the fetched snapshot, when known.

    Returns:
        An immutable row with ``datetime.date`` and integer minor units.

    Raises:
        RowValidationError: If a required field is missing or invalid.
    """
    payload = _as_mapping(row, index=index)
    return NormalizedRow(
        report_date=_parse_date(payload, index=index),
        country=_parse_key(payload, "country", index=index),
        channel=_parse_key(payload, "channel", index=index),
        impressions=_parse_count(payload, "impressions", index=index),
        clicks=_parse_count(payload, "clicks", index=index),
        installs=_parse_count(payload, "installs", index=index),
        signups=_parse_count(payload, "signups", index=index),
        payers=_parse_count(payload, "payers", index=index),
        spend_minor=_parse_money(payload, "spend", index=index),
        revenue_minor=_parse_money(payload, "revenue", index=index),
    )


def _as_mapping(row: Any, *, index: int | None) -> Mapping[str, Any]:
    """Return ``row`` when it is a mapping.

    Args:
        row: One raw page element.
        index: Zero-based position when known.

    Returns:
        The same mapping.

    Raises:
        RowValidationError: If ``row`` is not a mapping. Field name ``"row"``.
    """
    if isinstance(row, Mapping):
        return row
    _fail("must be an object", field="row", index=index, row=None)


def _parse_date(row: Mapping[str, Any], *, index: int | None) -> date:
    """Parse the required ``date`` field as a calendar date.

    Args:
        row: Raw row object.
        index: Zero-based position when known.

    Returns:
        The reporting date.

    Raises:
        RowValidationError: If ``date`` is missing, null, or not ``YYYY-MM-DD``.
    """
    value = _require_value(row, "date", index=index)
    try:
        return parse_calendar_date(value)
    except ValueError as exc:
        _fail(str(exc), field="date", index=index, row=row)


def _parse_key(row: Mapping[str, Any], field: str, *, index: int | None) -> str:
    """Return a stripped, non-empty string dimension.

    Args:
        row: Raw row object.
        field: Dimension name, such as ``country`` or ``channel``.
        index: Zero-based position when known.

    Returns:
        The stripped key.

    Raises:
        RowValidationError: If the field is missing, null, or blank.
    """
    value = _require_value(row, field, index=index)
    if not isinstance(value, str):
        _fail("must be a non-empty string", field=field, index=index, row=row)
    text = value.strip()
    if not text:
        _fail("must be a non-empty string", field=field, index=index, row=row)
    return text


def _parse_count(row: Mapping[str, Any], field: str, *, index: int | None) -> int:
    """Return a required count as a PostgreSQL bigint.

    Booleans are rejected even though ``bool`` is a subclass of ``int``.

    Args:
        row: Raw row object.
        field: Count metric name.
        index: Zero-based position when known.

    Returns:
        The integer count.

    Raises:
        RowValidationError: If the field is missing, not an ``int``, or
            outside the bigint range.
    """
    value = _require_value(row, field, index=index)
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("must be an integer", field=field, index=index, row=row)
    return _require_bigint(value, field=field, index=index, row=row)


def _parse_money(row: Mapping[str, Any], field: str, *, index: int | None) -> int:
    """Parse a minor-unit money string into a PostgreSQL bigint.

    Args:
        row: Raw row object.
        field: ``spend`` or ``revenue``.
        index: Zero-based position when known.

    Returns:
        The integer minor units. The sign is preserved.

    Raises:
        RowValidationError: If the field is missing, or is not an
            optional-sign digit string inside the bigint range.
    """
    value = _require_value(row, field, index=index)
    if not isinstance(value, str) or not _MONEY_RE.fullmatch(value):
        _fail(
            "must be an integer minor-unit string",
            field=field,
            index=index,
            row=row,
        )
    return _require_bigint(int(value), field=field, index=index, row=row)


def _require_value(row: Mapping[str, Any], field: str, *, index: int | None) -> Any:
    """Return a required field that is present and not null.

    Args:
        row: Raw row object.
        field: API field name.
        index: Zero-based position when known.

    Returns:
        The raw value.

    Raises:
        RowValidationError: If the field is absent or ``None``.
    """
    if field not in row:
        _fail("is missing", field=field, index=index, row=row)
    value = row[field]
    if value is None:
        _fail("is null", field=field, index=index, row=row)
    return value


def _require_bigint(
    value: int,
    *,
    field: str,
    index: int | None,
    row: Mapping[str, Any],
) -> int:
    """Return ``value`` when it fits in a signed PostgreSQL bigint.

    Args:
        value: Already parsed integer.
        field: API field name used in the error.
        index: Zero-based position when known.
        row: Raw row, used only to add grain context to the message.

    Returns:
        ``value``.

    Raises:
        RowValidationError: If ``value`` is outside
            ``POSTGRES_BIGINT_MIN``..``POSTGRES_BIGINT_MAX``.
    """
    if not POSTGRES_BIGINT_MIN <= value <= POSTGRES_BIGINT_MAX:
        _fail(
            "is outside the PostgreSQL bigint range",
            field=field,
            index=index,
            row=row,
        )
    return value


def _fail(
    reason: str,
    *,
    field: str,
    index: int | None,
    row: Mapping[str, Any] | None,
) -> NoReturn:
    """Raise :class:`RowValidationError` with the row index and grain context.

    Args:
        reason: Phrase appended after the field name.
        field: API field that failed.
        index: Zero-based position when known.
        row: Raw row used for dimension context. ``None`` omits that context.

    Raises:
        RowValidationError: Always.
    """
    parts: list[str] = []
    if index is not None:
        parts.append(f"row {index}")
    parts.append(f"field {field!r}")
    message = f"{' '.join(parts)}: {reason}"
    context = _record_context(row)
    if context:
        message = f"{message} ({context})"
    raise RowValidationError(message, field=field, index=index)


def _record_context(row: Mapping[str, Any] | None) -> str:
    """Return ``date``, ``country``, and ``channel`` fragments for an error.

    Args:
        row: Raw row, or ``None`` when the payload was not an object.

    Returns:
        A comma-separated ``key=value`` string, or ``""`` when no dimension
        is present.
    """
    if row is None:
        return ""
    bits = []
    for key in REPORT_DIMENSIONS:
        if key in row and row[key] is not None:
            bits.append(f"{key}={row[key]!r}")
    return ", ".join(bits)
