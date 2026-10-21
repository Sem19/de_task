"""Typed contracts for normalized partner report rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class NormalizedRow:
    """One validated report row ready for ``partner_report``.

    Attributes:
        report_date: Reporting date in the account timezone.
        country: Non-empty country key.
        channel: Non-empty channel key.
        impressions: Impression count.
        clicks: Click count.
        installs: Install count.
        signups: Signup count.
        payers: Payer count.
        spend_minor: Spend in integer minor units of the account currency.
        revenue_minor: Revenue in integer minor units of the account currency.
    """

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

    @property
    def grain(self) -> tuple[date, str, str]:
        """Return the reporting grain.

        Returns:
            ``(report_date, country, channel)``.
        """
        return (self.report_date, self.country, self.channel)


@dataclass(frozen=True)
class ValidatedReport:
    """A snapshot that passed row and report integrity checks.

    Dates on or after ``restated_through`` are still mutable. Dates strictly
    before it are settled. This object does not compute watermarks.

    Attributes:
        report_id: Partner job identifier.
        account_id: Partner account identifier.
        date_from: Inclusive requested start date.
        date_to: Inclusive requested end date.
        currency: Account and report currency, after they were checked equal.
        timezone: Account and report timezone label, after they were checked equal.
        data_version: Partner snapshot version from page ``meta``.
        restated_through: Oldest still-mutable reporting date.
        attribution_window_days: Trailing mutable dates from account metadata.
        rows: Normalized records in fetch order, including sparse dates.
    """

    report_id: str
    account_id: str
    date_from: date
    date_to: date
    currency: str
    timezone: str
    data_version: int
    restated_through: date
    attribution_window_days: int
    rows: tuple[NormalizedRow, ...]
