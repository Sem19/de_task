"""Isolated PostgreSQL tests for transactional date-slice loading."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql

from config import MOCK_ACCOUNT_TIMEZONE
from storage import LoadResult, StorageError, connection, replace_date_slice
from tests.test_storage_connection import (
    integration_dsn,
    postgres_available,
    settings_with_dsn,
)
from tests.test_storage_schema import _exec_script, _reset_schema, _set_search_path
from validation.models import NormalizedRow, ValidatedReport

WAREHOUSE_DIR = Path(__file__).resolve().parents[2] / "warehouse"
INIT_SQL_PATH = WAREHOUSE_DIR / "init.sql"
TEST_SCHEMA = "storage_loader_test"
EXTRACTED_AT = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


def _row(**overrides: object) -> NormalizedRow:
    payload: dict[str, object] = {
        "report_date": date(2026, 10, 21),
        "country": "US",
        "channel": "search",
        "impressions": 100,
        "clicks": 10,
        "installs": 4,
        "signups": 2,
        "payers": 1,
        "spend_minor": 101473,
        "revenue_minor": 12600,
    }
    payload.update(overrides)
    return NormalizedRow(**payload)  # type: ignore[arg-type]


def _snapshot(
    rows: tuple[NormalizedRow, ...],
    *,
    date_from: date = date(2026, 10, 20),
    date_to: date = date(2026, 10, 21),
    data_version: int = 42,
    currency: str = "EUR",
    timezone_name: str = MOCK_ACCOUNT_TIMEZONE,
) -> ValidatedReport:
    return ValidatedReport(
        report_id="rep_1",
        account_id="acct_5512",
        date_from=date_from,
        date_to=date_to,
        currency=currency,
        timezone=timezone_name,
        data_version=data_version,
        restated_through=date(2026, 10, 19),
        attribution_window_days=3,
        rows=rows,
    )


def _fetch_slice(
    conn: psycopg.Connection,
    *,
    date_from: date,
    date_to: date,
) -> list[tuple[object, ...]]:
    table = sql.Identifier(TEST_SCHEMA, "partner_report")
    return list(
        conn.execute(
            sql.SQL(
                """
                SELECT report_date, country, channel, spend_minor, revenue_minor,
                       currency, reporting_timezone, data_version, extracted_at
                FROM {}
                WHERE report_date >= %s AND report_date <= %s
                ORDER BY report_date, country, channel
                """
            ).format(table),
            (date_from, date_to),
        ).fetchall()
    )


def _count_all(conn: psycopg.Connection) -> int:
    table = sql.Identifier(TEST_SCHEMA, "partner_report")
    row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()
    assert row is not None
    return int(row[0])


@unittest.skipUnless(postgres_available(), "PostgreSQL is not available")
class ReplaceDateSliceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = settings_with_dsn(integration_dsn())
        cls.init_sql = INIT_SQL_PATH.read_text(encoding="utf-8")

    def setUp(self) -> None:
        with connection(self.settings) as conn:
            _reset_schema(conn, TEST_SCHEMA)
            _set_search_path(conn, TEST_SCHEMA)
            _exec_script(conn, self.init_sql)
            conn.commit()

    def tearDown(self) -> None:
        with connection(self.settings) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(TEST_SCHEMA)
                )
            )
            conn.commit()

    def _load(
        self, snapshot: ValidatedReport, extracted_at: datetime = EXTRACTED_AT
    ) -> LoadResult:
        with connection(self.settings) as conn:
            result = replace_date_slice(
                conn, snapshot, extracted_at=extracted_at, schema=TEST_SCHEMA
            )
            conn.commit()
        return result

    def test_first_load(self) -> None:
        snapshot = _snapshot(
            (
                _row(report_date=date(2026, 10, 20), country="US"),
                _row(report_date=date(2026, 10, 21), country="DE", channel="display"),
            )
        )
        result = self._load(snapshot)
        self.assertEqual(result.deleted, 0)
        self.assertEqual(result.inserted, 2)
        self.assertEqual(result.added, 2)
        self.assertEqual(result.removed, 0)
        self.assertEqual(result.updated, 0)
        self.assertEqual(result.unchanged, 0)
        with connection(self.settings) as conn:
            rows = _fetch_slice(
                conn, date_from=date(2026, 10, 20), date_to=date(2026, 10, 21)
            )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][3], 101473)
        self.assertEqual(rows[0][5], "EUR")
        self.assertEqual(rows[0][6], MOCK_ACCOUNT_TIMEZONE)
        self.assertEqual(rows[0][7], 42)
        self.assertEqual(rows[0][8], EXTRACTED_AT)

    def test_same_report_loaded_twice(self) -> None:
        snapshot = _snapshot((_row(),))
        first = self._load(snapshot)
        second = self._load(snapshot, EXTRACTED_AT + timedelta(minutes=15))
        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.deleted, 1)
        self.assertEqual(second.inserted, 1)
        self.assertEqual(second.added, 0)
        self.assertEqual(second.removed, 0)
        self.assertEqual(second.updated, 0)
        self.assertEqual(second.unchanged, 1)
        with connection(self.settings) as conn:
            self.assertEqual(_count_all(conn), 1)

    def test_updated_financial_metrics(self) -> None:
        self._load(_snapshot((_row(spend_minor=101473, revenue_minor=12600),)))
        result = self._load(_snapshot((_row(spend_minor=90, revenue_minor=-250),)))
        self.assertEqual(result.deleted, 1)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.unchanged, 0)
        with connection(self.settings) as conn:
            spend, revenue = conn.execute(
                sql.SQL(
                    "SELECT spend_minor, revenue_minor FROM {} WHERE country = %s"
                ).format(sql.Identifier(TEST_SCHEMA, "partner_report")),
                ("US",),
            ).fetchone()
        self.assertEqual(spend, 90)
        self.assertEqual(revenue, -250)

    def test_deleted_source_keys(self) -> None:
        self._load(
            _snapshot(
                (
                    _row(country="US", channel="search"),
                    _row(country="GB", channel="social"),
                )
            )
        )
        result = self._load(_snapshot((_row(country="US", channel="search"),)))
        self.assertEqual(result.deleted, 2)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.removed, 1)
        self.assertEqual(result.added, 0)
        self.assertEqual(result.unchanged, 1)
        with connection(self.settings) as conn:
            countries = conn.execute(
                sql.SQL("SELECT country FROM {} ORDER BY country").format(
                    sql.Identifier(TEST_SCHEMA, "partner_report")
                )
            ).fetchall()
        self.assertEqual(countries, [("US",)])

    def test_valid_empty_snapshot_deletes_the_slice(self) -> None:
        self._load(
            _snapshot(
                (_row(report_date=date(2026, 10, 21)),),
                date_from=date(2026, 10, 21),
                date_to=date(2026, 10, 21),
            )
        )
        result = self._load(
            _snapshot(
                (),
                date_from=date(2026, 10, 21),
                date_to=date(2026, 10, 21),
            )
        )
        self.assertEqual(result.deleted, 1)
        self.assertEqual(result.inserted, 0)
        self.assertEqual(result.removed, 1)
        with connection(self.settings) as conn:
            self.assertEqual(_count_all(conn), 0)

    def test_transaction_rollback(self) -> None:
        snapshot = _snapshot((_row(),))
        self._load(snapshot)
        with connection(self.settings) as conn:
            with patch(
                "storage.loader._insert_rows",
                side_effect=StorageError("injected copy failure"),
            ):
                with self.assertRaises(StorageError):
                    replace_date_slice(
                        conn,
                        _snapshot((_row(spend_minor=1),)),
                        extracted_at=EXTRACTED_AT,
                        schema=TEST_SCHEMA,
                    )
            conn.rollback()
            spend = conn.execute(
                sql.SQL("SELECT spend_minor FROM {}").format(
                    sql.Identifier(TEST_SCHEMA, "partner_report")
                )
            ).fetchone()
        self.assertEqual(spend, (101473,))

    def test_unaffected_dates_are_preserved(self) -> None:
        outside = _snapshot(
            (_row(report_date=date(2026, 10, 14), spend_minor=7),),
            date_from=date(2026, 10, 14),
            date_to=date(2026, 10, 14),
        )
        inside = _snapshot(
            (_row(report_date=date(2026, 10, 21), spend_minor=101473),),
            date_from=date(2026, 10, 20),
            date_to=date(2026, 10, 21),
        )
        self._load(outside)
        self._load(inside)
        self._load(
            _snapshot(
                (_row(report_date=date(2026, 10, 21), spend_minor=3),),
                date_from=date(2026, 10, 20),
                date_to=date(2026, 10, 21),
            )
        )
        with connection(self.settings) as conn:
            rows = conn.execute(
                sql.SQL(
                    """
                    SELECT report_date, spend_minor
                    FROM {}
                    ORDER BY report_date
                    """
                ).format(sql.Identifier(TEST_SCHEMA, "partner_report"))
            ).fetchall()
        self.assertEqual(
            rows,
            [(date(2026, 10, 14), 7), (date(2026, 10, 21), 3)],
        )

    def test_currency_timezone_and_version(self) -> None:
        snapshot = _snapshot(
            (_row(),),
            currency="EUR",
            timezone_name=MOCK_ACCOUNT_TIMEZONE,
            data_version=2987657,
        )
        self._load(snapshot)
        with connection(self.settings) as conn:
            row = conn.execute(
                sql.SQL(
                    """
                    SELECT currency, reporting_timezone, data_version, spend_minor
                    FROM {}
                    """
                ).format(sql.Identifier(TEST_SCHEMA, "partner_report"))
            ).fetchone()
        self.assertEqual(row, ("EUR", MOCK_ACCOUNT_TIMEZONE, 2987657, 101473))

    def test_naive_extracted_at_is_rejected(self) -> None:
        with connection(self.settings) as conn:
            with self.assertRaises(StorageError):
                replace_date_slice(
                    conn,
                    _snapshot((_row(),)),
                    extracted_at=datetime(2026, 10, 21, 15, 0),
                    schema=TEST_SCHEMA,
                )

    def test_replace_does_not_commit(self) -> None:
        snapshot = _snapshot((_row(),))
        with connection(self.settings) as conn:
            replace_date_slice(
                conn, snapshot, extracted_at=EXTRACTED_AT, schema=TEST_SCHEMA
            )
            conn.rollback()
            self.assertEqual(_count_all(conn), 0)
