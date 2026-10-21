"""Unit tests for extraction date-window planning."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from config import MOCK_ACCOUNT_TIMEZONE, REPORT_MAX_SPAN_DAYS
from storage.state import ExtractState
from window import (
    WINDOW_BACKFILL,
    WINDOW_INCREMENTAL,
    WindowError,
    account_today,
    load_extract_start_date,
    parse_extract_start_date,
    plan_extract_windows,
)

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 21)
EXTRACTED_AT = NOW


def _state(
    *,
    settled_through: date | None,
    loaded_through: date | None = None,
    loaded_from: date | None = None,
    last_date_from: date | None = None,
    last_date_to: date | None = None,
) -> ExtractState:
    loaded = loaded_through if loaded_through is not None else settled_through
    if loaded is None:
        loaded = TODAY
    start = last_date_from if last_date_from is not None else loaded
    end = last_date_to if last_date_to is not None else loaded
    return ExtractState(
        loaded_through=loaded,
        loaded_from=loaded_from if loaded_from is not None else start,
        settled_through=settled_through,
        currency="EUR",
        reporting_timezone=MOCK_ACCOUNT_TIMEZONE,
        account_id="acct_5512",
        last_report_id="rep_1",
        last_extracted_at=EXTRACTED_AT,
        last_data_version=42,
        last_restated_through=date(2026, 10, 19),
        last_date_from=start,
        last_date_to=end,
    )


def _assert_gap_free(test: unittest.TestCase, windows) -> None:
    test.assertGreater(len(windows), 0)
    for window in windows:
        test.assertLessEqual(window.inclusive_days, REPORT_MAX_SPAN_DAYS + 1)
        test.assertLessEqual(
            (window.date_to - window.date_from).days, REPORT_MAX_SPAN_DAYS
        )
    for previous, current in zip(windows, windows[1:]):
        test.assertEqual(current.date_from, previous.date_to + timedelta(days=1))
        test.assertGreaterEqual(current.date_from, previous.date_to)


class AccountTodayTest(unittest.TestCase):
    def test_fixed_utc_minus_seven_not_iana_los_angeles(self) -> None:
        self.assertEqual(account_today(NOW), TODAY)

    def test_utc_midnight_crossing_account_date_boundary(self) -> None:
        before = datetime(2026, 10, 22, 6, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 22, 7, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(account_today(before), date(2026, 10, 21))
        self.assertEqual(account_today(after), date(2026, 10, 22))

    def test_aware_non_utc_instant_converts(self) -> None:
        offset = timezone(timedelta(hours=2))
        now = datetime(2026, 10, 21, 17, 0, tzinfo=offset)
        self.assertEqual(account_today(now), TODAY)

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaises(WindowError):
            account_today(datetime(2026, 10, 21, 15, 0))


class ExtractStartDateTest(unittest.TestCase):
    def test_blank_is_unset(self) -> None:
        self.assertIsNone(parse_extract_start_date(None))
        self.assertIsNone(parse_extract_start_date(""))
        self.assertIsNone(parse_extract_start_date("  "))
        self.assertIsNone(load_extract_start_date({}))

    def test_valid_date(self) -> None:
        self.assertEqual(parse_extract_start_date("2026-10-01"), date(2026, 10, 1))
        self.assertEqual(
            load_extract_start_date({"EXTRACT_START_DATE": "2026-10-01"}),
            date(2026, 10, 1),
        )

    def test_invalid_extract_start_date(self) -> None:
        for raw in ("2026-13-01", "2026/10/01", "Oct 1", "2026-10-1", "yesterday"):
            with self.subTest(raw=raw):
                with self.assertRaises(WindowError):
                    parse_extract_start_date(raw)


class PlanExtractWindowsTest(unittest.TestCase):
    def test_initial_seven_day_window(self) -> None:
        windows = plan_extract_windows(now_utc=NOW)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].date_from, date(2026, 10, 15))
        self.assertEqual(windows[0].date_to, TODAY)
        self.assertEqual(windows[0].kind, WINDOW_INCREMENTAL)
        self.assertEqual(windows[0].inclusive_days, 7)

    def test_initial_explicit_start_date(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW, extract_start_date=date(2026, 10, 1)
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 1))
        self.assertEqual(windows[-1].date_to, TODAY)
        _assert_gap_free(self, windows)
        self.assertTrue(all(item.kind == WINDOW_INCREMENTAL for item in windows))

    def test_existing_settled_watermark(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=date(2026, 10, 7),
                loaded_through=date(2026, 10, 7),
            ),
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 8))
        self.assertEqual(windows[-1].date_to, TODAY)
        self.assertTrue(all(item.kind == WINDOW_INCREMENTAL for item in windows))
        _assert_gap_free(self, windows)

    def test_multi_day_downtime(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=date(2026, 10, 5),
                loaded_through=date(2026, 10, 5),
            ),
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 6))
        self.assertEqual(windows[-1].date_to, TODAY)
        covered = (windows[-1].date_to - windows[0].date_from).days + 1
        self.assertEqual(covered, 16)
        _assert_gap_free(self, windows)

    def test_mutable_dates_reprocessed(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=date(2026, 10, 18),
                loaded_through=TODAY,
                last_date_from=date(2026, 10, 15),
                last_date_to=TODAY,
            ),
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].date_from, date(2026, 10, 19))
        self.assertEqual(windows[0].date_to, TODAY)
        self.assertEqual(windows[0].kind, WINDOW_INCREMENTAL)

    def test_historical_backfill_override(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=date(2026, 10, 18),
                loaded_through=TODAY,
                last_date_from=date(2026, 10, 15),
            ),
            extract_start_date=date(2026, 10, 1),
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 1))
        self.assertEqual(windows[-1].date_to, TODAY)
        _assert_gap_free(self, windows)
        backfill = [item for item in windows if item.kind == WINDOW_BACKFILL]
        incremental = [item for item in windows if item.kind == WINDOW_INCREMENTAL]
        self.assertEqual(backfill[0].date_from, date(2026, 10, 1))
        self.assertEqual(backfill[-1].date_to, date(2026, 10, 18))
        self.assertEqual(incremental[0].date_from, date(2026, 10, 19))
        self.assertEqual(incremental[-1].date_to, TODAY)

    def test_backfill_does_not_skip_unprocessed_gap(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=date(2026, 10, 7),
                loaded_through=date(2026, 10, 14),
            ),
            extract_start_date=date(2026, 10, 1),
        )
        dates: list[date] = []
        for window in windows:
            cursor = window.date_from
            while cursor <= window.date_to:
                dates.append(cursor)
                cursor += timedelta(days=1)
        self.assertEqual(dates[0], date(2026, 10, 1))
        self.assertEqual(dates[-1], TODAY)
        self.assertEqual(len(dates), (TODAY - date(2026, 10, 1)).days + 1)
        self.assertEqual(dates, sorted(set(dates)))
        _assert_gap_free(self, windows)

    def test_later_extract_start_does_not_skip_watermark(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(settled_through=date(2026, 10, 7)),
            extract_start_date=date(2026, 10, 10),
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 8))
        _assert_gap_free(self, windows)

    def test_seven_day_chunk_boundaries(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW, extract_start_date=date(2026, 10, 1)
        )
        self.assertEqual(
            [(item.date_from, item.date_to) for item in windows],
            [
                (date(2026, 10, 1), date(2026, 10, 7)),
                (date(2026, 10, 8), date(2026, 10, 14)),
                (date(2026, 10, 15), date(2026, 10, 21)),
            ],
        )
        self.assertTrue(all(item.inclusive_days == 7 for item in windows))

    def test_no_skipped_or_overlapping_chunk_dates(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW, extract_start_date=date(2026, 9, 28)
        )
        _assert_gap_free(self, windows)
        self.assertEqual(windows[0].date_from, date(2026, 9, 28))
        self.assertEqual(windows[-1].date_to, TODAY)

    def test_empty_window(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(settled_through=TODAY, loaded_through=TODAY),
        )
        self.assertEqual(windows, ())

    def test_future_extract_start_is_empty_on_first_run(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW, extract_start_date=date(2026, 10, 22)
        )
        self.assertEqual(windows, ())

    def test_single_day_range(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(settled_through=date(2026, 10, 20), loaded_through=TODAY),
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].date_from, TODAY)
        self.assertEqual(windows[0].date_to, TODAY)
        self.assertEqual(windows[0].inclusive_days, 1)

    def test_multi_month_range(self) -> None:
        now = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        windows = plan_extract_windows(now_utc=now, extract_start_date=date(2026, 1, 1))
        self.assertEqual(windows[0].date_from, date(2026, 1, 1))
        self.assertEqual(windows[-1].date_to, date(2026, 3, 15))
        _assert_gap_free(self, windows)
        days = (date(2026, 3, 15) - date(2026, 1, 1)).days + 1
        self.assertEqual(sum(item.inclusive_days for item in windows), days)

    def test_never_settled_replans_from_coverage_start(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=None,
                loaded_through=TODAY,
                loaded_from=date(2026, 10, 19),
                last_date_from=date(2026, 10, 19),
                last_date_to=TODAY,
            ),
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 19))
        self.assertEqual(windows[-1].date_to, TODAY)
        self.assertEqual(windows[0].kind, WINDOW_INCREMENTAL)

    def test_never_settled_multi_chunk_starts_at_loaded_from(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=None,
                loaded_through=TODAY,
                loaded_from=date(2026, 10, 8),
                last_date_from=date(2026, 10, 15),
                last_date_to=TODAY,
            ),
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 8))
        self.assertEqual(windows[-1].date_to, TODAY)
        _assert_gap_free(self, windows)

    def test_never_settled_partial_run_recovers_remaining_dates(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=None,
                loaded_through=date(2026, 10, 14),
                loaded_from=date(2026, 10, 8),
                last_date_from=date(2026, 10, 8),
                last_date_to=date(2026, 10, 14),
            ),
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 8))
        self.assertEqual(windows[-1].date_to, TODAY)
        _assert_gap_free(self, windows)

    def test_repeated_never_settled_run_has_no_gaps(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=None,
                loaded_through=TODAY,
                loaded_from=date(2026, 10, 8),
                last_date_from=date(2026, 10, 15),
            ),
        )
        dates: list[date] = []
        for window in windows:
            cursor = window.date_from
            while cursor <= window.date_to:
                dates.append(cursor)
                cursor += timedelta(days=1)
        self.assertEqual(dates, [
            date(2026, 10, 8) + timedelta(days=offset)
            for offset in range((TODAY - date(2026, 10, 8)).days + 1)
        ])

    def test_never_settled_historical_backfill_is_gap_free(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=None,
                loaded_through=TODAY,
                loaded_from=date(2026, 10, 8),
                last_date_from=date(2026, 10, 15),
            ),
            extract_start_date=date(2026, 10, 1),
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 1))
        self.assertEqual(windows[0].kind, WINDOW_BACKFILL)
        self.assertEqual(windows[-1].date_to, TODAY)
        _assert_gap_free(self, windows)

    def test_first_window_is_baseline_not_full_history(self) -> None:
        windows = plan_extract_windows(
            now_utc=NOW,
            state=_state(
                settled_through=date(2026, 10, 18),
                loaded_through=TODAY,
                last_date_from=date(2026, 10, 15),
            ),
        )
        self.assertEqual(windows[0].date_from, date(2026, 10, 19))
        self.assertNotEqual(windows[0].date_from, date(2026, 10, 1))
