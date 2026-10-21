"""Isolated PostgreSQL tests for extract_state and advisory locking."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from psycopg import sql
from psycopg.types.json import Json

from config import MOCK_ACCOUNT_TIMEZONE
from storage import (
    StateConflictError,
    StorageError,
    acquire_extraction_lock,
    apply_snapshot,
    connection,
    read_state,
)
from tests.test_storage_connection import (
    integration_dsn,
    postgres_available,
    settings_with_dsn,
)
from tests.test_storage_loader import EXTRACTED_AT, _row, _snapshot
from tests.test_storage_schema import _exec_script, _reset_schema, _set_search_path
from window import plan_extract_windows

WAREHOUSE_DIR = Path(__file__).resolve().parents[2] / "warehouse"
INIT_SQL_PATH = WAREHOUSE_DIR / "init.sql"
TEST_SCHEMA = "storage_state_test"
TEST_LOCK_ID = 91043
LATER = EXTRACTED_AT + timedelta(minutes=15)


def _count_rows(conn, *, report_date: date | None = None) -> int:
    table = sql.Identifier(TEST_SCHEMA, "partner_report")
    if report_date is None:
        row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()
    else:
        row = conn.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE report_date = %s").format(table),
            (report_date,),
        ).fetchone()
    assert row is not None
    return int(row[0])


@unittest.skipUnless(postgres_available(), "PostgreSQL is not available")
class ExtractStateTest(unittest.TestCase):
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

    def _commit(self, snapshot, extracted_at=EXTRACTED_AT):
        with connection(self.settings) as conn:
            load_result, _pending = apply_snapshot(
                conn, snapshot, extracted_at=extracted_at, schema=TEST_SCHEMA
            )
            conn.commit()
            state = read_state(conn, schema=TEST_SCHEMA)
        return load_result, state

    def test_first_successful_checkpoint(self) -> None:
        """The first committed window is the coverage and settlement baseline."""
        snapshot = _snapshot(
            (
                _row(report_date=date(2026, 10, 15)),
                _row(report_date=date(2026, 10, 21), country="DE"),
            ),
            date_from=date(2026, 10, 15),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        load_result, state = self._commit(snapshot)
        self.assertEqual(load_result.inserted, 2)
        self.assertIsNotNone(state)
        self.assertEqual(state.loaded_through, date(2026, 10, 21))
        self.assertEqual(state.loaded_from, date(2026, 10, 15))
        self.assertEqual(state.settled_through, date(2026, 10, 18))
        self.assertEqual(state.next_incremental_start, date(2026, 10, 19))
        self.assertEqual(state.currency, "EUR")
        self.assertEqual(state.reporting_timezone, MOCK_ACCOUNT_TIMEZONE)
        self.assertEqual(state.last_report_id, "rep_1")
        self.assertEqual(state.last_data_version, 42)

    def test_identical_rerun(self) -> None:
        snapshot = _snapshot(
            (_row(report_date=date(2026, 10, 21)),),
            date_from=date(2026, 10, 19),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        _, first = self._commit(snapshot)
        rerun = _snapshot(
            (_row(report_date=date(2026, 10, 21)),),
            date_from=date(2026, 10, 19),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
            report_id="rep_2",
        )
        load_result, second = self._commit(rerun, LATER)
        self.assertEqual(load_result.unchanged, 1)
        self.assertEqual(second.loaded_through, first.loaded_through)
        self.assertEqual(second.settled_through, first.settled_through)
        self.assertEqual(second.last_report_id, "rep_2")
        self.assertEqual(second.last_data_version, first.last_data_version)

    def test_mutable_only_window(self) -> None:
        snapshot = _snapshot(
            (_row(report_date=date(2026, 10, 21)),),
            date_from=date(2026, 10, 19),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        _, state = self._commit(snapshot)
        self.assertEqual(state.loaded_through, date(2026, 10, 21))
        self.assertIsNone(state.settled_through)
        self.assertIsNone(state.next_incremental_start)

    def test_progress_through_consecutive_immutable_chunks(self) -> None:
        first = _snapshot(
            (_row(report_date=date(2026, 10, 7), country="US"),),
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 7),
            restated_through=date(2026, 10, 30),
        )
        _, state = self._commit(first)
        self.assertEqual(state.settled_through, date(2026, 10, 7))
        second = _snapshot(
            (_row(report_date=date(2026, 10, 14), country="GB"),),
            date_from=date(2026, 10, 8),
            date_to=date(2026, 10, 14),
            restated_through=date(2026, 10, 30),
        )
        _, state = self._commit(second, LATER)
        self.assertEqual(state.settled_through, date(2026, 10, 14))
        third = _snapshot(
            (_row(report_date=date(2026, 10, 21), country="DE"),),
            date_from=date(2026, 10, 15),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        _, state = self._commit(third, LATER + timedelta(minutes=15))
        self.assertEqual(state.loaded_through, date(2026, 10, 21))
        self.assertEqual(state.settled_through, date(2026, 10, 18))
        self.assertEqual(state.next_incremental_start, date(2026, 10, 19))

    def test_empty_complete_report(self) -> None:
        seeded = _snapshot(
            (_row(report_date=date(2026, 10, 21)),),
            date_from=date(2026, 10, 15),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        self._commit(seeded)
        empty = _snapshot(
            (),
            date_from=date(2026, 10, 19),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        load_result, state = self._commit(empty, LATER)
        self.assertEqual(load_result.inserted, 0)
        self.assertEqual(load_result.removed, 1)
        self.assertEqual(state.loaded_through, date(2026, 10, 21))
        self.assertEqual(state.settled_through, date(2026, 10, 18))
        with connection(self.settings) as conn:
            self.assertEqual(_count_rows(conn, report_date=date(2026, 10, 21)), 0)

    def test_failed_transaction_leaves_data_and_checkpoint_unchanged(self) -> None:
        snapshot = _snapshot(
            (_row(),),
            date_from=date(2026, 10, 15),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        self._commit(snapshot)
        with connection(self.settings) as conn:
            with patch(
                "storage.state._write_state",
                side_effect=StorageError("injected checkpoint failure"),
            ):
                with self.assertRaises(StorageError):
                    apply_snapshot(
                        conn,
                        _snapshot(
                            (_row(spend_minor=1),),
                            date_from=date(2026, 10, 15),
                            date_to=date(2026, 10, 21),
                            restated_through=date(2026, 10, 19),
                        ),
                        extracted_at=LATER,
                        schema=TEST_SCHEMA,
                    )
            conn.rollback()
            state = read_state(conn, schema=TEST_SCHEMA)
            spend = conn.execute(
                sql.SQL("SELECT spend_minor FROM {}").format(
                    sql.Identifier(TEST_SCHEMA, "partner_report")
                )
            ).fetchone()
        self.assertEqual(state.last_report_id, "rep_1")
        self.assertEqual(state.settled_through, date(2026, 10, 18))
        self.assertEqual(spend, (101473,))

    def test_cannot_skip_unsuccessful_earlier_chunk(self) -> None:
        first = _snapshot(
            (_row(report_date=date(2026, 10, 7)),),
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 7),
            restated_through=date(2026, 10, 30),
        )
        self._commit(first)
        skipped = _snapshot(
            (_row(report_date=date(2026, 10, 21), country="DE"),),
            date_from=date(2026, 10, 15),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        _, state = self._commit(skipped, LATER)
        self.assertEqual(state.settled_through, date(2026, 10, 7))
        self.assertEqual(state.loaded_through, date(2026, 10, 7))
        self.assertEqual(state.next_incremental_start, date(2026, 10, 8))
        with connection(self.settings) as conn:
            self.assertEqual(_count_rows(conn, report_date=date(2026, 10, 21)), 1)
            self.assertEqual(_count_rows(conn, report_date=date(2026, 10, 7)), 1)

    def test_one_day_gap_does_not_advance_watermarks(self) -> None:
        first = _snapshot(
            (_row(report_date=date(2026, 10, 7)),),
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 7),
            restated_through=date(2026, 10, 30),
        )
        self._commit(first)
        gapped = _snapshot(
            (_row(report_date=date(2026, 10, 14), country="GB"),),
            date_from=date(2026, 10, 9),
            date_to=date(2026, 10, 14),
            restated_through=date(2026, 10, 30),
        )
        _, state = self._commit(gapped, LATER)
        self.assertEqual(state.settled_through, date(2026, 10, 7))
        self.assertEqual(state.loaded_through, date(2026, 10, 7))
        self.assertEqual(state.next_incremental_start, date(2026, 10, 8))
        with connection(self.settings) as conn:
            self.assertEqual(_count_rows(conn, report_date=date(2026, 10, 14)), 1)
            self.assertEqual(_count_rows(conn, report_date=date(2026, 10, 7)), 1)

    def test_never_settled_multi_chunk_does_not_skip_earlier_mutable_dates(self) -> None:
        """A 14-day mutable window spans two 7-day reports; none settle.

        After the second chunk commits, the next plan must still start at
        the first chunk's date_from, not the last snapshot's date_from.
        """
        restated = date(2026, 10, 8)
        first = _snapshot(
            (_row(report_date=date(2026, 10, 8)),),
            date_from=date(2026, 10, 8),
            date_to=date(2026, 10, 14),
            restated_through=restated,
        )
        _, state = self._commit(first)
        self.assertIsNone(state.settled_through)
        second = _snapshot(
            (_row(report_date=date(2026, 10, 21), country="DE"),),
            date_from=date(2026, 10, 15),
            date_to=date(2026, 10, 21),
            restated_through=restated,
            report_id="rep_2",
        )
        _, state = self._commit(second, LATER)
        self.assertIsNone(state.settled_through)
        self.assertEqual(state.loaded_through, date(2026, 10, 21))
        self.assertEqual(state.loaded_from, date(2026, 10, 8))
        self.assertEqual(state.last_date_from, date(2026, 10, 15))
        now = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
        windows = plan_extract_windows(now_utc=now, state=state)
        self.assertEqual(windows[0].date_from, date(2026, 10, 8))
        self.assertEqual(windows[-1].date_to, date(2026, 10, 21))
        dates: list[date] = []
        for window in windows:
            cursor = window.date_from
            while cursor <= window.date_to:
                dates.append(cursor)
                cursor += timedelta(days=1)
        self.assertEqual(dates[0], date(2026, 10, 8))
        self.assertEqual(len(dates), (date(2026, 10, 21) - date(2026, 10, 8)).days + 1)

    def test_never_settled_partial_chunk_recovers_from_coverage_start(self) -> None:
        restated = date(2026, 10, 8)
        first = _snapshot(
            (_row(report_date=date(2026, 10, 8)),),
            date_from=date(2026, 10, 8),
            date_to=date(2026, 10, 14),
            restated_through=restated,
        )
        _, state = self._commit(first)
        self.assertIsNone(state.settled_through)
        self.assertEqual(state.loaded_from, date(2026, 10, 8))
        self.assertEqual(state.loaded_through, date(2026, 10, 14))
        now = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
        windows = plan_extract_windows(now_utc=now, state=state)
        self.assertEqual(windows[0].date_from, date(2026, 10, 8))
        self.assertEqual(windows[-1].date_to, date(2026, 10, 21))
        self.assertEqual(windows[1].date_from, date(2026, 10, 15))

    def test_legacy_checkpoint_without_loaded_from(self) -> None:
        with connection(self.settings) as conn:
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} (key, value, updated_at) VALUES (%s, %s, %s)"
                ).format(sql.Identifier(TEST_SCHEMA, "extract_state")),
                (
                    "partner_report",
                    Json(
                        {
                            "loaded_through": "2026-10-21",
                            "settled_through": None,
                            "currency": "EUR",
                            "reporting_timezone": MOCK_ACCOUNT_TIMEZONE,
                            "account_id": "acct_5512",
                            "last_report_id": "rep_1",
                            "last_extracted_at": EXTRACTED_AT.isoformat(),
                            "last_data_version": 42,
                            "last_restated_through": "2026-10-08",
                            "last_date_from": "2026-10-15",
                            "last_date_to": "2026-10-21",
                        }
                    ),
                    EXTRACTED_AT,
                ),
            )
            conn.commit()
            state = read_state(conn, schema=TEST_SCHEMA)
        self.assertEqual(state.loaded_from, date(2026, 10, 15))
        self.assertIsNone(state.settled_through)

    def test_pending_checkpoint_log_is_not_commit(self) -> None:
        snapshot = _snapshot(
            (_row(report_date=date(2026, 10, 21)),),
            date_from=date(2026, 10, 19),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        with self.assertLogs("storage.state", level="INFO") as captured:
            self._commit(snapshot)
        text = "\n".join(captured.output).lower()
        self.assertIn("uncommitted", text)
        self.assertNotIn("checkpointed", text)
        self.assertNotIn("successfully committed", text)

    def test_historical_backfill_does_not_advance_incremental_cursor(self) -> None:
        current = _snapshot(
            (_row(report_date=date(2026, 10, 21)),),
            date_from=date(2026, 10, 15),
            date_to=date(2026, 10, 21),
            restated_through=date(2026, 10, 19),
        )
        self._commit(current)
        backfill = _snapshot(
            (_row(report_date=date(2026, 10, 2), country="FR"),),
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 7),
            restated_through=date(2026, 10, 19),
        )
        _, state = self._commit(backfill, LATER)
        self.assertEqual(state.settled_through, date(2026, 10, 18))
        self.assertEqual(state.loaded_through, date(2026, 10, 21))
        self.assertEqual(state.loaded_from, date(2026, 10, 15))
        self.assertEqual(state.next_incremental_start, date(2026, 10, 19))
        with connection(self.settings) as conn:
            self.assertEqual(_count_rows(conn, report_date=date(2026, 10, 2)), 1)
            self.assertEqual(_count_rows(conn, report_date=date(2026, 10, 21)), 1)

    def test_currency_change_is_rejected(self) -> None:
        self._commit(
            _snapshot(
                (_row(),),
                date_from=date(2026, 10, 21),
                date_to=date(2026, 10, 21),
                restated_through=date(2026, 10, 19),
            )
        )
        with connection(self.settings) as conn:
            with self.assertRaises(StateConflictError):
                apply_snapshot(
                    conn,
                    _snapshot(
                        (_row(spend_minor=1),),
                        date_from=date(2026, 10, 21),
                        date_to=date(2026, 10, 21),
                        restated_through=date(2026, 10, 19),
                        currency="USD",
                    ),
                    extracted_at=LATER,
                    schema=TEST_SCHEMA,
                )
            conn.rollback()
            state = read_state(conn, schema=TEST_SCHEMA)
            spend = conn.execute(
                sql.SQL("SELECT spend_minor FROM {}").format(
                    sql.Identifier(TEST_SCHEMA, "partner_report")
                )
            ).fetchone()
        self.assertEqual(state.currency, "EUR")
        self.assertEqual(spend, (101473,))

    def test_timezone_change_is_rejected(self) -> None:
        self._commit(
            _snapshot(
                (_row(),),
                date_from=date(2026, 10, 21),
                date_to=date(2026, 10, 21),
                restated_through=date(2026, 10, 19),
            )
        )
        with connection(self.settings) as conn:
            with self.assertRaises(StateConflictError) as ctx:
                apply_snapshot(
                    conn,
                    _snapshot(
                        (_row(),),
                        date_from=date(2026, 10, 21),
                        date_to=date(2026, 10, 21),
                        restated_through=date(2026, 10, 19),
                        timezone_name="UTC",
                    ),
                    extracted_at=LATER,
                    schema=TEST_SCHEMA,
                )
            self.assertIn("timezone", str(ctx.exception))
            conn.rollback()


@unittest.skipUnless(postgres_available(), "PostgreSQL is not available")
class ExtractionLockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = settings_with_dsn(integration_dsn())

    def test_concurrency_lock_prevents_overlapping_execution(self) -> None:
        first = acquire_extraction_lock(self.settings, lock_id=TEST_LOCK_ID)
        self.assertIsNotNone(first)
        try:
            second = acquire_extraction_lock(self.settings, lock_id=TEST_LOCK_ID)
            self.assertIsNone(second)
            with connection(self.settings) as work:
                work.execute("SELECT 1")
                work.commit()
            still_blocked = acquire_extraction_lock(self.settings, lock_id=TEST_LOCK_ID)
            self.assertIsNone(still_blocked)
        finally:
            first.release()
        third = acquire_extraction_lock(self.settings, lock_id=TEST_LOCK_ID)
        self.assertIsNotNone(third)
        third.release()
