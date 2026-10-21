"""Dataclasses for partner API responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Account:
    """Account metadata from ``GET /v2/account``.

    Attributes:
        account_id: Partner account identifier.
        timezone: Reporting timezone label returned by the API.
        currency: Reporting currency code returned by the API.
        attribution_window_days: Number of trailing dates that still restate.
    """

    account_id: str
    timezone: str
    currency: str
    attribution_window_days: int


@dataclass(frozen=True)
class Report:
    """A submitted or polled report job.

    Attributes:
        report_id: Partner job identifier.
        status: Partner status string (``PENDING``, ``RUNNING``, ``DONE``, …).
        row_count: Total rows once the job is ``DONE``; otherwise ``None``.
    """

    report_id: str
    status: str
    row_count: int | None = None


@dataclass(frozen=True)
class ReportRows:
    """Full snapshot of report rows plus partner completeness metadata.

    ``rows`` are the raw API objects, including any duplicate keys. The
    validation layer decides whether duplicates are acceptable.

    Attributes:
        rows: Concatenated page payloads, in fetch order.
        total: Partner-reported row count.
        currency: Reporting currency from page ``meta``.
        timezone: Reporting timezone label from page ``meta``.
        data_version: Partner snapshot version from page ``meta``.
        restated_through: Oldest still-mutable date, ``YYYY-MM-DD``.
        truncated: Whether the partner dropped rows above its cap.
    """

    rows: list[dict[str, Any]]
    total: int
    currency: str
    timezone: str
    data_version: int
    restated_through: str
    truncated: bool


@dataclass(frozen=True)
class Page:
    """One ``/rows`` response after type checks."""

    rows: list[dict[str, Any]]
    total: int
    has_more: bool
    currency: str
    timezone: str
    data_version: int
    restated_through: str
    truncated: bool
