"""Unit tests for whole-report snapshot integrity checks."""

from __future__ import annotations

from datetime import date
from typing import Any

import unittest

from api_client import Account, Report, ReportRows
from tests.test_validation_rows import valid_row
from validation import (
    ReportValidationError,
    RowValidationError,
    ValidatedReport,
    validate_report,
)

ACCOUNT = Account(
    account_id="acct_5512",
    timezone="America/Los_Angeles",
    currency="EUR",
    attribution_window_days=3,
)

DATE_FROM = "2026-10-15"
DATE_TO = "2026-10-21"


def make_report(**overrides: Any) -> Report:
    payload: dict[str, Any] = {
        "report_id": "rep_1",
        "status": "DONE",
        "row_count": 1,
    }
    payload.update(overrides)
    return Report(**payload)


def make_rows(raw: list[dict[str, Any]], **overrides: Any) -> ReportRows:
    payload: dict[str, Any] = {
        "rows": raw,
        "total": len(raw),
        "currency": "EUR",
        "timezone": "America/Los_Angeles",
        "data_version": 42,
        "restated_through": "2026-10-19",
        "truncated": False,
    }
    payload.update(overrides)
    return ReportRows(**payload)


class ValidateReportTest(unittest.TestCase):
    def test_valid_complete_report(self) -> None:
        raw = [
            valid_row(date="2026-10-15", country="US", channel="search"),
            valid_row(date="2026-10-21", country="AU", channel="display"),
        ]
        result = validate_report(
            make_report(row_count=2),
            make_rows(raw),
            ACCOUNT,
            DATE_FROM,
            DATE_TO,
        )
        self.assertIsInstance(result, ValidatedReport)
        self.assertEqual(result.report_id, "rep_1")
        self.assertEqual(result.account_id, "acct_5512")
        self.assertEqual(result.date_from, date(2026, 10, 15))
        self.assertEqual(result.date_to, date(2026, 10, 21))
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(result.timezone, "America/Los_Angeles")
        self.assertEqual(result.data_version, 42)
        self.assertEqual(result.restated_through, date(2026, 10, 19))
        self.assertEqual(result.attribution_window_days, 3)
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.rows[0].grain, (date(2026, 10, 15), "US", "search"))
        with self.assertRaises(AttributeError):
            result.rows = ()  # type: ignore[misc]

    def test_not_done_is_rejected(self) -> None:
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(status="PENDING", row_count=None),
                make_rows([valid_row()]),
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "not_done")

    def test_count_mismatches(self) -> None:
        raw = [valid_row(), valid_row(country="US")]
        cases = (
            make_report(row_count=1),
            make_report(row_count=2),
        )
        mismatched_rows = (
            make_rows(raw, total=1),
            make_rows(raw[:1], total=2),
        )
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(cases[0], make_rows(raw), ACCOUNT, DATE_FROM, DATE_TO)
        self.assertEqual(ctx.exception.error, "inconsistent_total")
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(cases[1], mismatched_rows[0], ACCOUNT, DATE_FROM, DATE_TO)
        self.assertEqual(ctx.exception.error, "inconsistent_total")
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(row_count=2),
                mismatched_rows[1],
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "inconsistent_total")
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(row_count=None),
                make_rows([]),
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "inconsistent_total")

    def test_duplicate_business_keys_across_pages(self) -> None:
        duplicate = valid_row(date="2026-10-21", country="US", channel="search")
        other = valid_row(date="2026-10-21", country="GB", channel="search")
        raw = [duplicate, other, dict(duplicate)]
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(row_count=3),
                make_rows(raw),
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "duplicate_grain")
        self.assertIn("rows 0 and 2", str(ctx.exception))

    def test_truncation_is_rejected_even_when_counts_match(self) -> None:
        raw = [valid_row()]
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(row_count=1),
                make_rows(raw, truncated=True),
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "truncated")

    def test_currency_mismatch(self) -> None:
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(),
                make_rows([valid_row()], currency="USD"),
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "currency_mismatch")

    def test_timezone_mismatch(self) -> None:
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(),
                make_rows([valid_row()], timezone="UTC"),
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "timezone_mismatch")

    def test_dates_outside_requested_range(self) -> None:
        raw = [valid_row(date="2026-10-14")]
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(),
                make_rows(raw),
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "date_out_of_range")
        raw = [valid_row(date="2026-10-22")]
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(),
                make_rows(raw),
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "date_out_of_range")

    def test_sparse_date_ranges_and_unequal_daily_counts(self) -> None:
        raw = [
            valid_row(date="2026-10-15", country="US", channel="search"),
            valid_row(date="2026-10-15", country="US", channel="social"),
            valid_row(date="2026-10-21", country="DE", channel="display"),
        ]
        result = validate_report(
            make_report(row_count=3),
            make_rows(raw),
            ACCOUNT,
            DATE_FROM,
            DATE_TO,
        )
        dates = [row.report_date for row in result.rows]
        self.assertEqual(
            dates, [date(2026, 10, 15), date(2026, 10, 15), date(2026, 10, 21)]
        )
        self.assertNotIn(date(2026, 10, 16), dates)

    def test_empty_valid_report(self) -> None:
        result = validate_report(
            make_report(row_count=0),
            make_rows([]),
            ACCOUNT,
            DATE_FROM,
            DATE_TO,
        )
        self.assertEqual(result.rows, ())
        self.assertEqual(result.data_version, 42)
        self.assertEqual(result.restated_through, date(2026, 10, 19))

    def test_historical_window_may_be_fully_settled(self) -> None:
        raw = [valid_row(date="2026-01-02", country="US", channel="search")]
        result = validate_report(
            make_report(),
            make_rows(raw, restated_through="2026-10-19"),
            ACCOUNT,
            "2026-01-01",
            "2026-01-07",
        )
        self.assertLess(result.rows[0].report_date, result.restated_through)

    def test_invalid_restatement_metadata(self) -> None:
        invalid = ("", "2026-13-01", "2026-10-19T00:00:00", "yesterday")
        for value in invalid:
            with self.subTest(restated_through=value):
                with self.assertRaises(ReportValidationError) as ctx:
                    validate_report(
                        make_report(row_count=0),
                        make_rows([], restated_through=value),
                        ACCOUNT,
                        DATE_FROM,
                        DATE_TO,
                    )
                self.assertEqual(ctx.exception.error, "invalid_restated_through")
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(
                make_report(row_count=0),
                make_rows([]),
                Account(
                    account_id="acct_5512",
                    timezone="America/Los_Angeles",
                    currency="EUR",
                    attribution_window_days=0,
                ),
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.error, "invalid_restated_through")

    def test_exact_preservation_of_financial_values(self) -> None:
        raw = [
            valid_row(spend="101473", revenue="-250"),
            valid_row(country="US", spend="0", revenue="9223372036854775807"),
        ]
        result = validate_report(
            make_report(row_count=2),
            make_rows(raw),
            ACCOUNT,
            DATE_FROM,
            DATE_TO,
        )
        self.assertEqual(result.rows[0].spend_minor, 101473)
        self.assertEqual(result.rows[0].revenue_minor, -250)
        self.assertEqual(result.rows[1].spend_minor, 0)
        self.assertEqual(result.rows[1].revenue_minor, 9223372036854775807)
        self.assertIsInstance(result.rows[0].spend_minor, int)
        self.assertIsInstance(result.rows[0].revenue_minor, int)

    def test_malformed_row_fails_the_snapshot(self) -> None:
        raw = [valid_row(), valid_row(spend="12.34")]
        with self.assertRaises(RowValidationError) as ctx:
            validate_report(
                make_report(row_count=2),
                make_rows(raw),
                ACCOUNT,
                DATE_FROM,
                DATE_TO,
            )
        self.assertEqual(ctx.exception.field, "spend")
        self.assertEqual(ctx.exception.index, 1)
