"""Unit tests for per-row validation and normalization."""

from __future__ import annotations

from datetime import date
from typing import Any

import unittest

from config import (
    POSTGRES_BIGINT_MAX,
    REPORT_COUNT_METRICS,
    REPORT_DIMENSIONS,
    REPORT_METRICS,
    REPORT_MONEY_METRICS,
    REPORT_REQUIRED_FIELDS,
)
from validation import NormalizedRow, RowValidationError, normalize_row, normalize_rows


def valid_row(**overrides: Any) -> dict[str, Any]:
    """Return a raw API row matching the mock payload shape."""
    row: dict[str, Any] = {
        "date": "2026-10-21",
        "country": "AU",
        "channel": "display",
        "impressions": 38421,
        "clicks": 812,
        "installs": 146,
        "signups": 58,
        "payers": 7,
        "spend": "101473",
        "revenue": "12600",
    }
    row.update(overrides)
    return row


class NormalizeRowTest(unittest.TestCase):
    def test_valid_record_normalization(self) -> None:
        result = normalize_row(valid_row())
        self.assertEqual(
            result,
            NormalizedRow(
                report_date=date(2026, 10, 21),
                country="AU",
                channel="display",
                impressions=38421,
                clicks=812,
                installs=146,
                signups=58,
                payers=7,
                spend_minor=101473,
                revenue_minor=12600,
            ),
        )
        self.assertEqual(result.grain, (date(2026, 10, 21), "AU", "display"))
        self.assertIsInstance(result.report_date, date)
        self.assertIsInstance(result.spend_minor, int)
        self.assertIsInstance(result.revenue_minor, int)
        with self.assertRaises(AttributeError):
            result.spend_minor = 0  # type: ignore[misc]

    def test_strips_business_keys_and_ignores_extra_fields(self) -> None:
        result = normalize_row(
            valid_row(country="  US ", channel=" search ", extra="ignored"),
        )
        self.assertEqual(result.country, "US")
        self.assertEqual(result.channel, "search")

    def test_zero_metrics_are_valid(self) -> None:
        result = normalize_row(
            valid_row(
                impressions=0,
                clicks=0,
                installs=0,
                signups=0,
                payers=0,
                spend="0",
                revenue="0",
            ),
        )
        self.assertEqual(result.impressions, 0)
        self.assertEqual(result.spend_minor, 0)
        self.assertEqual(result.revenue_minor, 0)

    def test_missing_mandatory_fields(self) -> None:
        for field in REPORT_REQUIRED_FIELDS:
            with self.subTest(field=field):
                row = valid_row()
                del row[field]
                with self.assertRaises(RowValidationError) as ctx:
                    normalize_row(row, index=3)
                self.assertEqual(ctx.exception.field, field)
                self.assertEqual(ctx.exception.index, 3)
                self.assertIn("missing", str(ctx.exception))
                self.assertIn(field, str(ctx.exception))

    def test_null_mandatory_fields(self) -> None:
        for field in REPORT_REQUIRED_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(RowValidationError) as ctx:
                    normalize_row(valid_row(**{field: None}), index=1)
                self.assertEqual(ctx.exception.field, field)
                self.assertIn("null", str(ctx.exception))

    def test_invalid_dates(self) -> None:
        invalid = (
            "2026-13-01",
            "2026-02-30",
            "2026/10/21",
            "21-10-2026",
            "2026-10-21T00:00:00",
            "20261021",
            "",
            "  ",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(RowValidationError) as ctx:
                    normalize_row(valid_row(date=value))
                self.assertEqual(ctx.exception.field, "date")

    def test_empty_country_and_channel(self) -> None:
        for field, value in (
            ("country", ""),
            ("country", "   "),
            ("channel", ""),
            ("channel", "\t"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(RowValidationError) as ctx:
                    normalize_row(valid_row(**{field: value}))
                self.assertEqual(ctx.exception.field, field)
                self.assertIn("non-empty", str(ctx.exception))

    def test_incorrect_numeric_types_for_counts(self) -> None:
        for field in REPORT_COUNT_METRICS:
            for value in ("100", 100.0, 1.5, [1], {"n": 1}):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(RowValidationError) as ctx:
                        normalize_row(valid_row(**{field: value}))
                    self.assertEqual(ctx.exception.field, field)
                    self.assertIn("integer", str(ctx.exception))

    def test_boolean_values_rejected_for_counts(self) -> None:
        for field in REPORT_COUNT_METRICS:
            for value in (True, False):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(RowValidationError) as ctx:
                        normalize_row(valid_row(**{field: value}))
                    self.assertEqual(ctx.exception.field, field)

    def test_invalid_monetary_strings(self) -> None:
        invalid = (
            "",
            "-",
            "+",
            "+12",
            "12.34",
            "12.0",
            "1e3",
            "1E2",
            " 101473",
            "101473 ",
            "10_1473",
            "EUR101473",
            "NaN",
        )
        for field in REPORT_MONEY_METRICS:
            for value in invalid:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(RowValidationError) as ctx:
                        normalize_row(valid_row(**{field: value}))
                    self.assertEqual(ctx.exception.field, field)
                    self.assertIn("minor-unit string", str(ctx.exception))

    def test_negative_monetary_values_are_preserved(self) -> None:
        result = normalize_row(valid_row(spend="-250", revenue="-1"))
        self.assertEqual(result.spend_minor, -250)
        self.assertEqual(result.revenue_minor, -1)

    def test_very_large_valid_integers(self) -> None:
        result = normalize_row(
            valid_row(
                impressions=POSTGRES_BIGINT_MAX,
                spend=str(POSTGRES_BIGINT_MAX),
                revenue=str(-POSTGRES_BIGINT_MAX),
            ),
        )
        self.assertEqual(result.impressions, POSTGRES_BIGINT_MAX)
        self.assertEqual(result.spend_minor, POSTGRES_BIGINT_MAX)
        self.assertEqual(result.revenue_minor, -POSTGRES_BIGINT_MAX)

    def test_bigint_overflow_is_rejected(self) -> None:
        too_large = POSTGRES_BIGINT_MAX + 1
        with self.assertRaises(RowValidationError) as ctx:
            normalize_row(valid_row(impressions=too_large))
        self.assertEqual(ctx.exception.field, "impressions")
        with self.assertRaises(RowValidationError) as ctx:
            normalize_row(valid_row(spend=str(too_large)))
        self.assertEqual(ctx.exception.field, "spend")

    def test_unexpected_field_types(self) -> None:
        cases = (
            ("date", date(2026, 10, 21)),
            ("country", ["US"]),
            ("channel", {"name": "search"}),
            ("spend", 101473),
            ("spend", 101473.0),
            ("spend", True),
            ("revenue", -12),
            ("impressions", None),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                row = valid_row()
                row[field] = value
                with self.assertRaises(RowValidationError) as ctx:
                    normalize_row(row)
                self.assertEqual(ctx.exception.field, field)

    def test_row_must_be_an_object(self) -> None:
        with self.assertRaises(RowValidationError) as ctx:
            normalize_row(["not", "an", "object"], index=4)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.field, "row")
        self.assertEqual(ctx.exception.index, 4)
        self.assertIn("row 4", str(ctx.exception))

    def test_error_includes_record_context(self) -> None:
        with self.assertRaises(RowValidationError) as ctx:
            normalize_row(valid_row(spend="12.34"), index=2)
        message = str(ctx.exception)
        self.assertIn("row 2", message)
        self.assertIn("field 'spend'", message)
        self.assertIn("date='2026-10-21'", message)
        self.assertIn("country='AU'", message)
        self.assertIn("channel='display'", message)

    def test_normalize_rows_preserves_order_and_grain(self) -> None:
        first = valid_row(country="AU", channel="display")
        second = valid_row(country="US", channel="search", spend="-9")
        result = normalize_rows([first, second])
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].grain, (date(2026, 10, 21), "AU", "display"))
        self.assertEqual(result[1].spend_minor, -9)

    def test_normalize_rows_empty(self) -> None:
        self.assertEqual(normalize_rows([]), ())

    def test_normalize_rows_fail_fast_with_index(self) -> None:
        rows = [valid_row(), valid_row(clicks=True), valid_row()]
        with self.assertRaises(RowValidationError) as ctx:
            normalize_rows(rows)
        self.assertEqual(ctx.exception.index, 1)
        self.assertEqual(ctx.exception.field, "clicks")

    def test_does_not_use_floating_point_for_money(self) -> None:
        result = normalize_row(valid_row(spend="000101473", revenue="-0003"))
        self.assertEqual(result.spend_minor, 101473)
        self.assertEqual(result.revenue_minor, -3)


class RequiredFieldsContractTest(unittest.TestCase):
    def test_required_fields_cover_grain_counts_and_money(self) -> None:
        self.assertEqual(REPORT_DIMENSIONS, ("date", "country", "channel"))
        self.assertEqual(
            REPORT_COUNT_METRICS,
            ("impressions", "clicks", "installs", "signups", "payers"),
        )
        self.assertEqual(REPORT_MONEY_METRICS, ("spend", "revenue"))
        self.assertEqual(REPORT_METRICS, REPORT_COUNT_METRICS + REPORT_MONEY_METRICS)
        self.assertEqual(REPORT_REQUIRED_FIELDS, REPORT_DIMENSIONS + REPORT_METRICS)
