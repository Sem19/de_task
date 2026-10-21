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
        """Return the reporting grain ``(report_date, country, channel)``."""
        return (self.report_date, self.country, self.channel)
