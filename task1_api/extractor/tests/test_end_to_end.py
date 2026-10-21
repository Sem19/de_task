"""Isolated end-to-end reconciliation, crash recovery, and failure injection."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from psycopg import sql

from api_client import PartnerAPIClient
from config import Settings
from pipeline import STATUS_FAILED, STATUS_SKIPPED, STATUS_SUCCESS, run_extraction
from storage import (
    StateConflictError,
    StorageError,
    acquire_extraction_lock,
    apply_snapshot,
    connection,
    read_state,
)
from tests.helpers import ACCOUNT_BODY, MockAPI, page
from tests.reconcile import (
    StoredRow,
    commit_snapshot,
    fetch_stored_rows,
    fetch_validated_snapshot,
    reconcile,
)
from tests.test_pipeline import ACCOUNT, NOW, TODAY, ScriptedClient, _payload
from tests.test_storage_connection import (
    integration_dsn,
    postgres_available,
    settings_with_dsn,
)
from tests.test_storage_schema import _exec_script, _reset_schema, _set_search_path
from tests.test_validation_rows import valid_row
from validation import ReportValidationError, validate_report
from window import account_today, plan_extract_windows

WAREHOUSE_DIR = Path(__file__).resolve().parents[2] / "warehouse"
INIT_SQL_PATH = WAREHOUSE_DIR / "init.sql"
TEST_SCHEMA = "e2e_reconcile_test"
TEST_LOCK_ID = 61001
EXTRACTED_AT = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


def _public_count(conn) -> int:
    row = conn.execute("SELECT count(*) FROM public.partner_report").fetchone()
    assert row is not None
    return int(row[0])


def _validated(date_from: str, date_to: str, **kwargs):
    done, body = _payload(date_from, date_to, **kwargs)
    return validate_report(done, body, ACCOUNT, date_from, date_to)


def _stored_from_snapshot(snapshot) -> tuple[StoredRow, ...]:
    return tuple(
        StoredRow(
            report_date=row.report_date,
            country=row.country,
            channel=row.channel,
            impressions=row.impressions,
            clicks=row.clicks,
            installs=row.installs,
            signups=row.signups,
            payers=row.payers,
            spend_minor=row.spend_minor,
            revenue_minor=row.revenue_minor,
            currency=snapshot.currency,
            timezone=snapshot.timezone,
            data_version=snapshot.data_version,
        )
        for row in snapshot.rows
    )


def live_api_base() -> str:
    """Return the mock API origin reachable from the host running tests."""
    configured = os.environ.get("E2E_API_BASE") or os.environ.get("API_BASE", "")
    if configured.startswith("http://mock-api"):
        return "http://127.0.0.1:8080"
    if configured.strip():
        return configured.rstrip("/")
    return "http://127.0.0.1:8080"


def mock_api_available() -> bool:
    """Return True when the mock API health endpoint answers."""
    try:
        with httpx.Client(timeout=2.0) as http:
            response = http.get(f"{live_api_base()}/healthz")
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def live_settings() -> Settings:
    """Host settings for the Compose mock API and warehouse."""
    return Settings(
        api_base=live_api_base(),
        client_id=os.environ.get("API_CLIENT_ID", "acme-de-takehome"),
        client_secret=os.environ.get("API_CLIENT_SECRET", "s3cr3t-do-not-commit"),
        pg_dsn=integration_dsn(),
        poll_interval_s=0.5,
        poll_timeout_s=30.0,
    )


class RecoveringAPI(MockAPI):
    """Partner mock that recovers from token expiry and a transient POST 500."""

    def _account(self, request: httpx.Request) -> httpx.Response:
        denied = self._unauthorized(request)
        if denied is not None:
            return denied
        self.account_calls += 1
        if self.account_calls == 1:
            return httpx.Response(401, json={"detail": {"error": "token_expired"}})
        if self.account_calls == 2:
            return httpx.Response(500, json={"error": "upstream"})
        return httpx.Response(200, json=ACCOUNT_BODY)


class ReconcileHelperTest(unittest.TestCase):
    """Deterministic comparison tests; no PostgreSQL or HTTP."""

    def test_exact_match(self) -> None:
        snapshot = _validated("2026-10-15", "2026-10-21")
        result = reconcile(snapshot, _stored_from_snapshot(snapshot))
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.source_rows, 1)
        self.assertEqual(result.stored_rows, 1)
        self.assertEqual(result.missing, ())
        self.assertEqual(result.extra, ())
        self.assertEqual(result.duplicates, ())
        self.assertEqual(result.mismatches, ())

    def test_missing_extra_duplicate_and_mismatch_counts(self) -> None:
        snapshot = _validated(
            "2026-10-15",
            "2026-10-21",
            rows=[
                valid_row(date="2026-10-20", country="US", channel="search"),
                valid_row(date="2026-10-21", country="DE", channel="display"),
            ],
        )
        us, de = _stored_from_snapshot(snapshot)
        extra = StoredRow(
            report_date=date(2026, 10, 19),
            country="GB",
            channel="social",
            impressions=1,
            clicks=1,
            installs=1,
            signups=1,
            payers=1,
            spend_minor=2,
            revenue_minor=3,
            currency="EUR",
            timezone=snapshot.timezone,
            data_version=snapshot.data_version,
        )
        mismatched_de = StoredRow(
            **{
                **de.__dict__,
                "revenue_minor": de.revenue_minor + 50,
            }
        )
        result = reconcile(snapshot, (us, us, mismatched_de, extra))
        self.assertFalse(result.ok)
        self.assertEqual(result.missing, ((date(2026, 10, 20), "US", "search"),))
        self.assertEqual(result.extra, ((date(2026, 10, 19), "GB", "social"),))
        self.assertEqual(result.duplicates, ((date(2026, 10, 20), "US", "search"),))
        self.assertEqual(len(result.mismatches), 1)
        self.assertEqual(result.mismatches[0].field, "revenue_minor")
        self.assertEqual(result.mismatches[0].expected, de.revenue_minor)
        self.assertEqual(result.mismatches[0].actual, de.revenue_minor + 50)

    def test_independent_versions_need_not_match(self) -> None:
        first = _validated(
            "2026-10-19",
            "2026-10-21",
            report_id="rep_a",
            rows=[valid_row(date="2026-10-21", revenue="12600")],
        )
        done, body = _payload(
            "2026-10-19",
            "2026-10-21",
            report_id="rep_b",
            rows=[valid_row(date="2026-10-21", revenue="9900")],
        )
        later = validate_report(
            done,
            replace(body, data_version=99),
            ACCOUNT,
            "2026-10-19",
            "2026-10-21",
        )
        result = reconcile(later, _stored_from_snapshot(first))
        self.assertFalse(result.ok)
        fields = {item.field for item in result.mismatches}
        self.assertIn("revenue_minor", fields)
        self.assertIn("data_version", fields)
        self.assertIsInstance(result.mismatches[0].expected, int)
        self.assertIsInstance(result.mismatches[0].actual, int)

    def test_rejects_float_money(self) -> None:
        snapshot = _validated("2026-10-15", "2026-10-21")
        row = _stored_from_snapshot(snapshot)[0]
        broken = StoredRow(**{**row.__dict__, "spend_minor": 101473.0})  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            reconcile(snapshot, (broken,))


@unittest.skipUnless(postgres_available(), "PostgreSQL is not available")
class IsolatedWarehouseTest(unittest.TestCase):
    """Load and recover in ``e2e_reconcile_test``; never touch ``public`` data."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = settings_with_dsn(integration_dsn())
        cls.init_sql = INIT_SQL_PATH.read_text(encoding="utf-8")

    def setUp(self) -> None:
        with connection(self.settings) as conn:
            self.public_rows_before = _public_count(conn)
            _reset_schema(conn, TEST_SCHEMA)
            _set_search_path(conn, TEST_SCHEMA)
            _exec_script(conn, self.init_sql)
            conn.commit()

    def tearDown(self) -> None:
        with connection(self.settings) as conn:
            public_after = _public_count(conn)
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(TEST_SCHEMA)
                )
            )
            conn.commit()
        self.assertEqual(public_after, self.public_rows_before)

    def _lock(self, settings=None):
        return acquire_extraction_lock(settings or self.settings, lock_id=TEST_LOCK_ID)

    def _load(self, snapshot, extracted_at=EXTRACTED_AT) -> None:
        commit_snapshot(
            self.settings, snapshot, schema=TEST_SCHEMA, extracted_at=extracted_at
        )

    def _stored(self, snapshot=None) -> tuple[StoredRow, ...]:
        with connection(self.settings) as conn:
            if snapshot is None:
                return fetch_stored_rows(conn, schema=TEST_SCHEMA)
            return fetch_stored_rows(
                conn,
                schema=TEST_SCHEMA,
                date_from=snapshot.date_from,
                date_to=snapshot.date_to,
            )

    def _assert_match(self, snapshot) -> None:
        result = reconcile(snapshot, self._stored(snapshot))
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.source_rows, len(snapshot.rows))
        self.assertEqual(result.stored_rows, len(snapshot.rows))
        self.assertEqual(len(result.missing), 0)
        self.assertEqual(len(result.extra), 0)
        self.assertEqual(len(result.duplicates), 0)
        self.assertEqual(len(result.mismatches), 0)

    def _extract(self, client, **kwargs):
        params = {
            "settings": self.settings,
            "now_utc": NOW,
            "extract_start_date": None,
            "client": client,
            "acquire_lock": self._lock,
            "migrate": lambda _s: None,
            "schema": TEST_SCHEMA,
        }
        params.update(kwargs)
        return run_extraction(**params)

    def test_same_snapshot_loaded_twice(self) -> None:
        snapshot = _validated("2026-10-15", "2026-10-21")
        self._load(snapshot)
        self._load(snapshot)
        self._assert_match(snapshot)
        with connection(self.settings) as conn:
            count = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier(TEST_SCHEMA, "partner_report")
                )
            ).fetchone()
        self.assertEqual(count[0], 1)

    def test_historical_revenue_correction(self) -> None:
        original = _validated(
            "2026-10-19",
            "2026-10-21",
            report_id="rep_orig",
            rows=[valid_row(date="2026-10-21", revenue="12600")],
        )
        corrected = _validated(
            "2026-10-19",
            "2026-10-21",
            report_id="rep_fix",
            rows=[valid_row(date="2026-10-21", revenue="9900")],
        )
        self._load(original)
        self._load(corrected)
        self._assert_match(corrected)
        stored = self._stored(corrected)[0]
        self.assertEqual(stored.revenue_minor, 9900)
        self.assertEqual(stored.spend_minor, 101473)
        self.assertIsInstance(stored.revenue_minor, int)
        self.assertNotEqual(stored.revenue_minor, original.rows[0].revenue_minor)

    def test_removed_source_row(self) -> None:
        first = _validated(
            "2026-10-19",
            "2026-10-21",
            rows=[
                valid_row(date="2026-10-21", country="US", channel="search"),
                valid_row(date="2026-10-21", country="GB", channel="social"),
            ],
        )
        second = _validated(
            "2026-10-19",
            "2026-10-21",
            report_id="rep_2",
            rows=[valid_row(date="2026-10-21", country="US", channel="search")],
        )
        self._load(first)
        self._load(second)
        result = reconcile(second, self._stored(second))
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.stored_rows, 1)
        self.assertEqual(self._stored()[0].country, "US")

    def test_empty_authoritative_snapshot(self) -> None:
        seeded = _validated(
            "2026-10-19",
            "2026-10-21",
            rows=[valid_row(date="2026-10-21")],
        )
        empty = _validated("2026-10-19", "2026-10-21", report_id="rep_empty", rows=[])
        self._load(seeded)
        self._load(empty)
        result = reconcile(empty, self._stored(empty))
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.source_rows, 0)
        self.assertEqual(result.stored_rows, 0)

    def test_failure_before_commit_rolls_back_data_and_state(self) -> None:
        seeded = _validated("2026-10-15", "2026-10-21", report_id="rep_seed")
        self._load(seeded)
        later = _validated(
            "2026-10-15",
            "2026-10-21",
            report_id="rep_fail",
            rows=[valid_row(date="2026-10-21", revenue="1")],
        )
        with connection(self.settings) as conn:
            with patch(
                "storage.state._write_state",
                side_effect=StorageError("injected checkpoint failure"),
            ):
                with self.assertRaises(StorageError):
                    apply_snapshot(
                        conn,
                        later,
                        extracted_at=EXTRACTED_AT,
                        schema=TEST_SCHEMA,
                    )
            conn.rollback()
            state = read_state(conn, schema=TEST_SCHEMA)
            stored = self._stored(seeded)
        self.assertEqual(state.last_report_id, "rep_seed")
        result = reconcile(seeded, stored)
        self.assertTrue(result.ok, result.summary())

    def test_failure_after_completed_earlier_chunk(self) -> None:
        good = _payload("2026-10-08", "2026-10-14", day="2026-10-08")
        bad = _payload(
            "2026-10-15",
            "2026-10-21",
            rows=[valid_row(date="2026-01-01")],
        )
        client = ScriptedClient(
            {("2026-10-08", "2026-10-14"): good, ("2026-10-15", "2026-10-21"): bad}
        )
        result = self._extract(client, extract_start_date=date(2026, 10, 8))
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.chunks_committed, 1)
        with connection(self.settings) as conn:
            state = read_state(conn, schema=TEST_SCHEMA)
            stored = fetch_stored_rows(conn, schema=TEST_SCHEMA)
        self.assertEqual(state.loaded_through, date(2026, 10, 14))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].report_date, date(2026, 10, 8))
        first = validate_report(good[0], good[1], ACCOUNT, "2026-10-08", "2026-10-14")
        # ScriptedClient rewrites report_id; compare metrics only via stored grain.
        self.assertEqual(stored[0].spend_minor, first.rows[0].spend_minor)
        self.assertEqual(stored[0].revenue_minor, first.rows[0].revenue_minor)

    def test_recovery_after_missed_execution_intervals(self) -> None:
        baseline = _validated(
            "2026-09-29",
            "2026-10-05",
            report_id="rep_base",
            day="2026-10-05",
        )
        self._load(baseline)
        payloads = {
            ("2026-10-06", "2026-10-12"): _payload(
                "2026-10-06", "2026-10-12", day="2026-10-06"
            ),
            ("2026-10-13", "2026-10-19"): _payload(
                "2026-10-13", "2026-10-19", day="2026-10-13"
            ),
            ("2026-10-20", "2026-10-21"): _payload(
                "2026-10-20", "2026-10-21", day="2026-10-21"
            ),
        }
        client = ScriptedClient(payloads)
        result = self._extract(client)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(result.chunks_committed, 3)
        with connection(self.settings) as conn:
            state = read_state(conn, schema=TEST_SCHEMA)
            stored = fetch_stored_rows(conn, schema=TEST_SCHEMA)
        self.assertEqual(state.loaded_through, TODAY)
        self.assertEqual(state.next_incremental_start, date(2026, 10, 19))
        self.assertEqual(len(stored), 4)
        self.assertEqual(
            {row.report_date for row in stored},
            {
                date(2026, 10, 5),
                date(2026, 10, 6),
                date(2026, 10, 13),
                date(2026, 10, 21),
            },
        )

    def test_truncated_report_is_rejected(self) -> None:
        done, body = _payload("2026-10-15", "2026-10-21")
        truncated_body = replace(body, truncated=True)
        with self.assertRaises(ReportValidationError) as ctx:
            validate_report(done, truncated_body, ACCOUNT, "2026-10-15", "2026-10-21")
        self.assertEqual(ctx.exception.error, "truncated")
        client = ScriptedClient({("2026-10-15", "2026-10-21"): (done, truncated_body)})
        result = self._extract(client)
        self.assertEqual(result.status, STATUS_FAILED)
        with connection(self.settings) as conn:
            stored = fetch_stored_rows(conn, schema=TEST_SCHEMA)
            state = read_state(conn, schema=TEST_SCHEMA)
        self.assertEqual(stored, ())
        self.assertIsNone(state)

    def test_concurrent_execution_protection(self) -> None:
        held = self._lock()
        self.assertIsNotNone(held)
        try:
            client = ScriptedClient(
                {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
            )
            result = self._extract(client)
            self.assertEqual(result.status, STATUS_SKIPPED)
            self.assertEqual(client.submit_calls, [])
        finally:
            held.release()
        with connection(self.settings) as conn:
            stored = fetch_stored_rows(conn, schema=TEST_SCHEMA)
        self.assertEqual(stored, ())

    def test_never_settled_multi_chunk_coverage(self) -> None:
        restated = "2026-10-08"
        payloads = {
            ("2026-10-08", "2026-10-14"): _payload(
                "2026-10-08",
                "2026-10-14",
                day="2026-10-08",
                restated_through=restated,
            ),
            ("2026-10-15", "2026-10-21"): _payload(
                "2026-10-15",
                "2026-10-21",
                day="2026-10-21",
                restated_through=restated,
            ),
        }
        client = ScriptedClient(payloads)
        result = self._extract(client, extract_start_date=date(2026, 10, 8))
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(result.chunks_committed, 2)
        with connection(self.settings) as conn:
            state = read_state(conn, schema=TEST_SCHEMA)
            stored = fetch_stored_rows(conn, schema=TEST_SCHEMA)
        self.assertIsNone(state.settled_through)
        self.assertEqual(state.loaded_from, date(2026, 10, 8))
        self.assertEqual(state.loaded_through, TODAY)
        self.assertEqual(len(stored), 2)
        windows = plan_extract_windows(now_utc=NOW, state=state)
        self.assertEqual(windows[0].date_from, date(2026, 10, 8))
        self.assertEqual(windows[-1].date_to, TODAY)

    def test_currency_conflict(self) -> None:
        seeded = _validated("2026-10-15", "2026-10-21", report_id="rep_eur")
        self._load(seeded)
        done, body = _payload("2026-10-15", "2026-10-21", report_id="rep_usd")
        account = replace(ACCOUNT, currency="USD")
        snapshot = validate_report(
            done, replace(body, currency="USD"), account, "2026-10-15", "2026-10-21"
        )
        with connection(self.settings) as conn:
            with self.assertRaises(StateConflictError):
                apply_snapshot(
                    conn, snapshot, extracted_at=EXTRACTED_AT, schema=TEST_SCHEMA
                )
            conn.rollback()
        self._assert_match(seeded)

    def test_timezone_conflict(self) -> None:
        seeded = _validated("2026-10-15", "2026-10-21", report_id="rep_la")
        self._load(seeded)
        done, body = _payload("2026-10-15", "2026-10-21", report_id="rep_utc")
        account = replace(ACCOUNT, timezone="UTC")
        snapshot = validate_report(
            done, replace(body, timezone="UTC"), account, "2026-10-15", "2026-10-21"
        )
        with connection(self.settings) as conn:
            with self.assertRaises(StateConflictError):
                apply_snapshot(
                    conn, snapshot, extracted_at=EXTRACTED_AT, schema=TEST_SCHEMA
                )
            conn.rollback()
        self._assert_match(seeded)

    def test_token_expiry_and_transient_error_then_load(self) -> None:
        handler = RecoveringAPI()
        handler.row_pages = [
            page([valid_row(date="2026-10-15")], total=1, has_more=False)
        ]
        handler.status_default = {
            "report_id": "rep_1",
            "status": "DONE",
            "row_count": 1,
        }
        settings = Settings(
            api_base="http://partner.test",
            client_id=self.settings.client_id,
            client_secret="s3cr3t-do-not-commit",
            pg_dsn=self.settings.pg_dsn,
            retry_backoff_s=0.01,
            max_attempts=3,
        )
        client = PartnerAPIClient(settings, transport=httpx.MockTransport(handler))
        with patch("api_client.retry.time.sleep"):
            result = self._extract(client, settings=settings)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertGreaterEqual(handler.token_calls, 2)
        self.assertGreaterEqual(handler.account_calls, 3)
        self.assertEqual(handler.submit_calls, 1)
        with connection(self.settings) as conn:
            stored = fetch_stored_rows(conn, schema=TEST_SCHEMA)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].spend_minor, 101473)
        self.assertEqual(stored[0].revenue_minor, 12600)


@unittest.skipUnless(postgres_available(), "PostgreSQL is not available")
@unittest.skipUnless(mock_api_available(), "mock API is not available")
class LiveApiReconcileTest(unittest.TestCase):
    """Fetch a real mock-API snapshot and compare PostgreSQL to that report_id."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = live_settings()
        cls.init_sql = INIT_SQL_PATH.read_text(encoding="utf-8")

    def setUp(self) -> None:
        with connection(self.settings) as conn:
            self.public_rows_before = _public_count(conn)
            _reset_schema(conn, TEST_SCHEMA)
            _set_search_path(conn, TEST_SCHEMA)
            _exec_script(conn, self.init_sql)
            conn.commit()

    def tearDown(self) -> None:
        with connection(self.settings) as conn:
            public_after = _public_count(conn)
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(TEST_SCHEMA)
                )
            )
            conn.commit()
        self.assertEqual(public_after, self.public_rows_before)

    def test_live_snapshot_matches_postgres(self) -> None:
        now_utc = datetime.now(timezone.utc)
        today = account_today(now_utc)
        windows = plan_extract_windows(now_utc=now_utc, state=None)
        self.assertEqual(len(windows), 1)
        window = windows[0]
        with PartnerAPIClient(self.settings) as client:
            snapshot = fetch_validated_snapshot(
                client,
                window.date_from.isoformat(),
                window.date_to.isoformat(),
            )
        commit_snapshot(self.settings, snapshot, schema=TEST_SCHEMA)
        with connection(self.settings) as conn:
            stored = fetch_stored_rows(
                conn,
                schema=TEST_SCHEMA,
                date_from=snapshot.date_from,
                date_to=snapshot.date_to,
            )
            public_state = read_state(conn, schema="public")
            public_rows = _public_count(conn)
        result = reconcile(snapshot, stored)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.source_rows, len(snapshot.rows))
        self.assertEqual(result.stored_rows, len(stored))
        self.assertEqual(len(result.missing), 0)
        self.assertEqual(len(result.extra), 0)
        self.assertEqual(len(result.duplicates), 0)
        self.assertEqual(len(result.mismatches), 0)
        self.assertEqual(snapshot.date_to, today)
        self.assertEqual(snapshot.currency, "EUR")
        self.assertEqual(snapshot.timezone, "America/Los_Angeles")
        if stored:
            self.assertIsInstance(stored[0].spend_minor, int)
            self.assertIsInstance(stored[0].revenue_minor, int)
        if public_state is not None:
            self.assertNotEqual(public_state.last_report_id, snapshot.report_id)
        self.assertEqual(public_rows, self.public_rows_before)
        # Stash identifiers for the Stage 6.1 report via the assertion message.
        self.assertIsNotNone(
            snapshot.report_id,
            result.summary(),
        )
        spend_minor = sum(row.spend_minor for row in stored)
        revenue_minor = sum(row.revenue_minor for row in stored)
        self.assertEqual(spend_minor, sum(row.spend_minor for row in snapshot.rows))
        self.assertEqual(revenue_minor, sum(row.revenue_minor for row in snapshot.rows))
        print(
            f"{result.summary()} spend_minor_sum={spend_minor} "
            f"revenue_minor_sum={revenue_minor}",
            flush=True,
        )
