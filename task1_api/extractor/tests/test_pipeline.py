"""Orchestration tests for the one-shot extraction pipeline."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from psycopg import sql

from api_client import (
    Account,
    AmbiguousRequestError,
    Report,
    ReportNotFoundError,
    ReportRows,
)
from config import MOCK_ACCOUNT_TIMEZONE
from pipeline import (
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    run_extraction,
)
from storage import (
    ExtractState,
    LoadResult,
    StateConflictError,
    StorageError,
    acquire_extraction_lock,
    connection,
    read_state,
)
from tests.helpers import VALID_SETTINGS
from tests.test_storage_connection import (
    integration_dsn,
    postgres_available,
    settings_with_dsn,
)
from tests.test_storage_schema import _exec_script, _reset_schema, _set_search_path
from tests.test_validation_rows import valid_row

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 21)
ACCOUNT = Account(
    account_id="acct_5512",
    timezone="America/Los_Angeles",
    currency="EUR",
    attribution_window_days=3,
)
WAREHOUSE_DIR = Path(__file__).resolve().parents[2] / "warehouse"
INIT_SQL_PATH = WAREHOUSE_DIR / "init.sql"
TEST_SCHEMA = "pipeline_extract_test"
TEST_LOCK_ID = 91044


def _state(
    *,
    settled_through: date | None,
    loaded_through: date | None = None,
    loaded_from: date | None = None,
    last_date_from: date | None = None,
    last_date_to: date | None = None,
) -> ExtractState:
    """Build a checkpoint with EUR and the mock timezone filled in.

    Args:
        settled_through: Inclusive immutable watermark. ``None`` if unset.
        loaded_through: Inclusive loaded watermark. Defaults to
            ``settled_through``, or today when that is also unset.
        loaded_from: Inclusive coverage start. Defaults to the last window start.
        last_date_from: Last snapshot start. Defaults to the loaded end.
        last_date_to: Last snapshot end. Defaults to the loaded end.

    Returns:
        A frozen :class:`ExtractState`.
    """
    loaded = loaded_through if loaded_through is not None else settled_through or TODAY
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
        last_extracted_at=NOW,
        last_data_version=42,
        last_restated_through=date(2026, 10, 19),
        last_date_from=start,
        last_date_to=end,
    )


def _load_result(snapshot) -> LoadResult:
    """Return load counts that treat every snapshot row as newly inserted."""
    return LoadResult(
        deleted=0,
        inserted=len(snapshot.rows),
        added=len(snapshot.rows),
        removed=0,
        updated=0,
        unchanged=0,
        date_from=snapshot.date_from,
        date_to=snapshot.date_to,
    )


def _payload(
    date_from: str,
    date_to: str,
    *,
    day: str | None = None,
    rows: list[dict] | None = None,
    report_id: str = "rep_1",
    restated_through: str = "2026-10-19",
) -> tuple[Report, ReportRows]:
    """Build a ``DONE`` report and a complete rows body for one window.

    Args:
        date_from: Inclusive start, ``YYYY-MM-DD``.
        date_to: Inclusive end, ``YYYY-MM-DD``.
        day: Date used for the default single row. Defaults to ``date_from``.
        rows: Raw rows. Defaults to one US row.
        report_id: Partner job id.
        restated_through: Oldest mutable date in ``meta``.

    Returns:
        The status payload and the downloaded rows.
    """
    raw = rows if rows is not None else [valid_row(date=day or date_from, country="US")]
    done = Report(report_id=report_id, status="DONE", row_count=len(raw))
    body = ReportRows(
        rows=raw,
        total=len(raw),
        currency="EUR",
        timezone="America/Los_Angeles",
        data_version=42,
        restated_through=restated_through,
        truncated=False,
    )
    return done, body


class FakeLock:
    """Advisory lock that only records whether :meth:`release` ran."""

    def __init__(self) -> None:
        """Start unreleased."""
        self.released = False

    def release(self) -> None:
        """Mark the lock released. Does not talk to PostgreSQL."""
        self.released = True


class FakeConn:
    """Connection that counts commit, rollback, and close calls."""

    def __init__(self) -> None:
        """Start with every counter at zero."""
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def commit(self) -> None:
        """Record one commit."""
        self.commits += 1

    def rollback(self) -> None:
        """Record one rollback."""
        self.rollbacks += 1

    def close(self) -> None:
        """Record that the connection was closed."""
        self.closed = True


class ScriptedClient:
    """In-memory partner API that serves scripted snapshots per window."""

    def __init__(
        self,
        payloads: dict[tuple[str, str], tuple[Report, ReportRows]],
        *,
        account: Account = ACCOUNT,
    ) -> None:
        """Store scripted payloads and start with no calls recorded.

        Args:
            payloads: Map of ``(date_from, date_to)`` to a done report and rows.
            account: Account returned by :meth:`get_account`.
        """
        self.payloads = payloads
        self.account = account
        self.submit_calls: list[tuple[str, str]] = []
        self.closed = False
        self.ambiguous = False
        self.lost_once = False
        self._lost_used = False
        self._n = 0

    def get_account(self) -> Account:
        """Return the scripted account metadata."""
        return self.account

    def submit_report(self, date_from: str, date_to: str) -> Report:
        """Record the window and return ``PENDING``, or raise if marked ambiguous.

        Args:
            date_from: Inclusive start.
            date_to: Inclusive end.

        Returns:
            A new pending report id.

        Raises:
            AmbiguousRequestError: When ``ambiguous`` is set.
        """
        self.submit_calls.append((date_from, date_to))
        if self.ambiguous:
            raise AmbiguousRequestError(
                "POST /v2/reports failed after leaving the client",
                error="ambiguous_post",
            )
        self._n += 1
        return Report(report_id=f"rep_{self._n}", status="PENDING")

    def wait_for_report(self, report_id: str) -> Report:
        """Return ``DONE`` for the last submitted window.

        The first call raises :class:`ReportNotFoundError` when ``lost_once``
        is set.

        Args:
            report_id: Id returned by :meth:`submit_report`.

        Returns:
            A ``DONE`` report whose ``row_count`` matches the scripted payload.
        """
        if self.lost_once and not self._lost_used:
            self._lost_used = True
            raise ReportNotFoundError(report_id)
        date_from, date_to = self.submit_calls[-1]
        done, _rows = self.payloads[(date_from, date_to)]
        return Report(report_id=report_id, status="DONE", row_count=done.row_count)

    def fetch_rows(self, report_id: str) -> ReportRows:
        """Return the scripted rows for the last submitted window."""
        date_from, date_to = self.submit_calls[-1]
        _done, rows = self.payloads[(date_from, date_to)]
        return rows

    def close(self) -> None:
        """Mark the client closed."""
        self.closed = True


class PipelineTest(unittest.TestCase):
    """Orchestration with a fake lock, connection, and scripted API."""

    def setUp(self) -> None:
        self.lock = FakeLock()
        self.conn = FakeConn()
        self.applied: list = []

    def _cm(self, _settings):
        """Yield :attr:`conn` and close it when the context exits."""

        @contextmanager
        def inner():
            """Yield the fake connection, then close it."""
            try:
                yield self.conn
            finally:
                self.conn.close()

        return inner()

    def _apply(self, conn, snapshot, *, extracted_at, schema="public"):
        """Record ``snapshot`` and return a successful uncommitted load."""
        self.applied.append(snapshot)
        return _load_result(snapshot), _state(
            settled_through=date(2026, 10, 18),
            loaded_through=snapshot.date_to,
            last_date_from=snapshot.date_from,
            last_date_to=snapshot.date_to,
        )

    def _run(
        self,
        client: ScriptedClient,
        *,
        read_state_fn=None,
        apply_fn=None,
        **kwargs,
    ):
        """Run extraction with schema checks and the warehouse patched out.

        Args:
            client: Scripted partner API.
            read_state_fn: Replacement for ``read_state``. Defaults to no
                checkpoint.
            apply_fn: Replacement for ``apply_snapshot``. Defaults to
                :meth:`_apply`.
            **kwargs: Extra arguments for :func:`run_extraction`.

        Returns:
            The :class:`ExtractResult`.
        """
        params = {
            "settings": VALID_SETTINGS,
            "now_utc": NOW,
            "extract_start_date": None,
            "client": client,
            "acquire_lock": lambda _s: self.lock,
            "connection_cm": self._cm,
            "migrate": lambda _s: None,
        }
        params.update(kwargs)
        with (
            patch("pipeline.verify_schema"),
            patch(
                "pipeline.read_state",
                read_state_fn
                if read_state_fn is not None
                else (lambda *_a, **_k: None),
            ),
            patch(
                "pipeline.apply_snapshot",
                apply_fn if apply_fn is not None else self._apply,
            ),
        ):
            return run_extraction(**params)

    def test_caught_up_is_success_without_api_submit(self) -> None:
        client = ScriptedClient({})
        result = self._run(
            client,
            read_state_fn=lambda *_a, **_k: _state(
                settled_through=TODAY, loaded_through=TODAY
            ),
        )
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(result.chunks_planned, 0)
        self.assertEqual(client.submit_calls, [])
        self.assertEqual(self.conn.commits, 0)

    def test_first_successful_extraction(self) -> None:
        client = ScriptedClient(
            {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
        )
        result = self._run(client)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(result.chunks_planned, 1)
        self.assertEqual(result.chunks_committed, 1)
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0].date_from, date(2026, 10, 15))
        self.assertTrue(self.lock.released)
        self.assertTrue(client.closed)

    def test_repeated_same_day_execution(self) -> None:
        client = ScriptedClient(
            {
                ("2026-10-19", "2026-10-21"): _payload(
                    "2026-10-19", "2026-10-21", day="2026-10-21"
                )
            }
        )
        result = self._run(
            client,
            read_state_fn=lambda *_a, **_k: _state(
                settled_through=date(2026, 10, 18),
                loaded_through=TODAY,
                last_date_from=date(2026, 10, 15),
            ),
        )
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(client.submit_calls, [("2026-10-19", "2026-10-21")])
        self.assertEqual(self.applied[0].date_from, date(2026, 10, 19))

    def test_recovery_after_multi_day_downtime(self) -> None:
        payloads = {
            ("2026-10-06", "2026-10-12"): _payload("2026-10-06", "2026-10-12"),
            ("2026-10-13", "2026-10-19"): _payload(
                "2026-10-13", "2026-10-19", day="2026-10-13"
            ),
            ("2026-10-20", "2026-10-21"): _payload(
                "2026-10-20", "2026-10-21", day="2026-10-21"
            ),
        }
        client = ScriptedClient(payloads)
        result = self._run(
            client,
            read_state_fn=lambda *_a, **_k: _state(
                settled_through=date(2026, 10, 5),
                loaded_through=date(2026, 10, 5),
            ),
        )
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(result.chunks_committed, 3)
        self.assertEqual(list(client.submit_calls), list(payloads))

    def test_multiple_consecutive_report_chunks(self) -> None:
        payloads = {
            ("2026-10-01", "2026-10-07"): _payload("2026-10-01", "2026-10-07"),
            ("2026-10-08", "2026-10-14"): _payload("2026-10-08", "2026-10-14"),
            ("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21"),
        }
        client = ScriptedClient(payloads)
        result = self._run(client, extract_start_date=date(2026, 10, 1))
        self.assertEqual(result.chunks_committed, 3)
        self.assertEqual(client.submit_calls, list(payloads))
        self.assertEqual(self.conn.commits, 3)

    def test_historical_backfill_override(self) -> None:
        payloads = {
            ("2026-10-01", "2026-10-07"): _payload("2026-10-01", "2026-10-07"),
            ("2026-10-08", "2026-10-14"): _payload("2026-10-08", "2026-10-14"),
            ("2026-10-15", "2026-10-18"): _payload(
                "2026-10-15", "2026-10-18", day="2026-10-15"
            ),
            ("2026-10-19", "2026-10-21"): _payload(
                "2026-10-19", "2026-10-21", day="2026-10-21"
            ),
        }
        client = ScriptedClient(payloads)
        result = self._run(
            client,
            extract_start_date=date(2026, 10, 1),
            read_state_fn=lambda *_a, **_k: _state(
                settled_through=date(2026, 10, 18),
                loaded_through=TODAY,
                last_date_from=date(2026, 10, 15),
            ),
        )
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(client.submit_calls[0], ("2026-10-01", "2026-10-07"))
        self.assertEqual(client.submit_calls[-1], ("2026-10-19", "2026-10-21"))
        self.assertEqual(self.applied[0].date_from, date(2026, 10, 1))
        self.assertEqual(self.applied[-1].date_from, date(2026, 10, 19))

    def test_never_settled_checkpoint(self) -> None:
        client = ScriptedClient(
            {
                ("2026-10-19", "2026-10-21"): _payload(
                    "2026-10-19", "2026-10-21", day="2026-10-19"
                )
            }
        )
        result = self._run(
            client,
            read_state_fn=lambda *_a, **_k: _state(
                settled_through=None,
                loaded_through=TODAY,
                last_date_from=date(2026, 10, 19),
                last_date_to=TODAY,
            ),
        )
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(client.submit_calls, [("2026-10-19", "2026-10-21")])
        self.assertEqual(self.applied[0].date_from, date(2026, 10, 19))

    def test_validation_failure_before_database_write(self) -> None:
        bad = valid_row()
        del bad["spend"]
        client = ScriptedClient(
            {
                ("2026-10-15", "2026-10-21"): _payload(
                    "2026-10-15", "2026-10-21", rows=[bad]
                )
            }
        )
        result = self._run(client)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.conn.commits, 0)
        self.assertTrue(self.lock.released)
        self.assertTrue(client.closed)

    def test_failure_after_one_committed_chunk(self) -> None:
        good = _payload("2026-10-08", "2026-10-14")
        bad_rows = [valid_row(date="2026-01-01")]
        bad = _payload("2026-10-15", "2026-10-21", rows=bad_rows)
        client = ScriptedClient(
            {("2026-10-08", "2026-10-14"): good, ("2026-10-15", "2026-10-21"): bad}
        )
        result = self._run(client, extract_start_date=date(2026, 10, 8))
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.chunks_planned, 2)
        self.assertEqual(result.chunks_committed, 1)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(client.submit_calls[0], ("2026-10-08", "2026-10-14"))
        self.assertEqual(len(client.submit_calls), 2)

    def test_transaction_rollback_on_database_error(self) -> None:
        client = ScriptedClient(
            {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
        )

        def boom(*_a, **_k):
            """Fail the snapshot write before commit."""
            raise StorageError("injected write failure")

        result = self._run(client, apply_fn=boom)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(self.conn.commits, 0)
        self.assertEqual(self.conn.rollbacks, 1)
        self.assertIn("injected write failure", result.error or "")

    def test_successful_atomic_data_state_commit(self) -> None:
        client = ScriptedClient(
            {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
        )
        with self.assertLogs("pipeline", level="INFO") as captured:
            result = self._run(client)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.rollbacks, 0)
        text = "\n".join(captured.output)
        self.assertIn("Committed snapshot", text)
        self.assertIn("loaded_through", text)

    def test_concurrent_run_skipped_by_advisory_lock(self) -> None:
        client = ScriptedClient({})
        result = self._run(client, acquire_lock=lambda _s: None)
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertEqual(client.submit_calls, [])
        self.assertFalse(self.lock.released)

    def test_resource_cleanup_on_failure(self) -> None:
        client = ScriptedClient(
            {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
        )
        result = self._run(
            client, apply_fn=lambda *_a, **_k: (_ for _ in ()).throw(StorageError("x"))
        )
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertTrue(self.lock.released)
        self.assertTrue(client.closed)
        self.assertTrue(self.conn.closed)

    def test_lost_report_id_bounded_resubmission(self) -> None:
        client = ScriptedClient(
            {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
        )
        client.lost_once = True
        result = self._run(client)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(len(client.submit_calls), 2)
        self.assertEqual(self.conn.commits, 1)

    def test_ambiguous_post_is_not_retried(self) -> None:
        client = ScriptedClient(
            {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
        )
        client.ambiguous = True
        result = self._run(client)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(len(client.submit_calls), 1)
        self.assertEqual(self.applied, [])
        self.assertIn("failed after leaving the client", (result.error or "").lower())

    def test_empty_but_complete_snapshot(self) -> None:
        empty = _payload("2026-10-15", "2026-10-21", rows=[])
        client = ScriptedClient({("2026-10-15", "2026-10-21"): empty})
        result = self._run(client)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(len(self.applied[0].rows), 0)
        self.assertEqual(self.conn.commits, 1)

    def test_currency_timezone_conflict(self) -> None:
        client = ScriptedClient(
            {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
        )

        def conflict(*_a, **_k):
            """Reject the snapshot because the account currency changed."""
            raise StateConflictError("account currency changed from 'EUR' to 'USD'")

        result = self._run(client, apply_fn=conflict)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(self.conn.commits, 0)
        self.assertEqual(self.conn.rollbacks, 1)
        self.assertIn("currency", result.error or "")


@unittest.skipUnless(postgres_available(), "PostgreSQL is not available")
class PipelinePostgresTest(unittest.TestCase):
    """One extraction against PostgreSQL with the partner API mocked."""

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

    def test_first_successful_extraction_persists_rows_and_state(self) -> None:
        client = ScriptedClient(
            {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
        )
        result = run_extraction(
            self.settings,
            now_utc=NOW,
            extract_start_date=None,
            client=client,
            acquire_lock=lambda s: acquire_extraction_lock(s, lock_id=TEST_LOCK_ID),
            migrate=lambda _s: None,
            schema=TEST_SCHEMA,
        )
        self.assertEqual(result.status, STATUS_SUCCESS)
        with connection(self.settings) as conn:
            state = read_state(conn, schema=TEST_SCHEMA)
            count = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier(TEST_SCHEMA, "partner_report")
                )
            ).fetchone()
        self.assertIsNotNone(state)
        self.assertEqual(state.loaded_through, TODAY)
        self.assertEqual(state.settled_through, date(2026, 10, 18))
        self.assertEqual(count[0], 1)

    def test_concurrent_lock_skips_without_writes(self) -> None:
        held = acquire_extraction_lock(self.settings, lock_id=TEST_LOCK_ID)
        self.assertIsNotNone(held)
        try:
            client = ScriptedClient(
                {("2026-10-15", "2026-10-21"): _payload("2026-10-15", "2026-10-21")}
            )
            result = run_extraction(
                self.settings,
                now_utc=NOW,
                extract_start_date=None,
                client=client,
                acquire_lock=lambda s: acquire_extraction_lock(s, lock_id=TEST_LOCK_ID),
                migrate=lambda _s: None,
                schema=TEST_SCHEMA,
            )
            self.assertEqual(result.status, STATUS_SKIPPED)
            self.assertEqual(client.submit_calls, [])
        finally:
            held.release()
