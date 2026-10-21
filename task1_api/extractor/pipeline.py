"""One-shot extraction: lock, plan, load each chunk, commit data with state."""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from api_client import (
    AmbiguousRequestError,
    PartnerAPIClient,
    PartnerAPIError,
    Report,
    ReportNotFoundError,
    ReportRows,
)
from config import REPORT_RESUBMIT_LIMIT, Settings, WAREHOUSE_SCHEMA
from storage import (
    ExtractState,
    ExtractionLock,
    LoadResult,
    SchemaError,
    StateConflictError,
    StorageError,
    acquire_extraction_lock,
    apply_migrations,
    apply_snapshot,
    connection,
    read_state,
    verify_schema,
)
from validation import (
    ReportValidationError,
    RowValidationError,
    ValidatedReport,
    validate_report,
)
from window import (
    ReportWindow,
    WindowError,
    load_extract_start_date,
    plan_extract_windows,
)

logger = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

_MISSING = object()
_FAILURE_TYPES = (
    AmbiguousRequestError,
    PartnerAPIError,
    ReportValidationError,
    RowValidationError,
    StateConflictError,
    SchemaError,
    StorageError,
    WindowError,
)


@dataclass(frozen=True)
class ExtractResult:
    """Outcome of one extractor process.

    Attributes:
        status: ``success``, ``skipped``, or ``failed``.
        run_id: Short id for log correlation.
        chunks_planned: Windows produced by the planner.
        chunks_committed: Windows whose data and checkpoint both committed.
        error: Failure message when ``status`` is ``failed``.
    """

    status: str
    run_id: str
    chunks_planned: int = 0
    chunks_committed: int = 0
    error: str | None = None


def run_extraction(
    settings: Settings,
    *,
    now_utc: datetime | None = None,
    extract_start_date: Any = _MISSING,
    client: PartnerAPIClient | None = None,
    acquire_lock: Callable[[Settings], ExtractionLock | None] = acquire_extraction_lock,
    connection_cm: Callable = connection,
    migrate: Callable[[Settings], None] = apply_migrations,
    schema: str = WAREHOUSE_SCHEMA,
    env: Mapping[str, str] | None = None,
) -> ExtractResult:
    """Run one extraction. Holds the advisory lock for the whole process.

    Args:
        settings: API and warehouse settings.
        now_utc: Clock instant for ``account_today``. Defaults to UTC now.
        extract_start_date: Planner override. Unset reads ``EXTRACT_START_DATE``.
        client: Optional API client. Created and always closed when omitted.
        acquire_lock: Lock factory. Returns ``None`` when another run holds it.
        connection_cm: Load-connection context manager.
        migrate: Warehouse DDL that must not share the loader transaction.
        schema: Warehouse schema.
        env: Environment mapping for ``EXTRACT_START_DATE``.

    Returns:
        Success, skipped (lock busy), or failed. Does not raise for expected
        extraction errors.
    """
    run_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    clock = now_utc if now_utc is not None else datetime.now(timezone.utc)
    start_date = (
        load_extract_start_date(os.environ if env is None else env)
        if extract_start_date is _MISSING
        else extract_start_date
    )
    logger.info("Extractor run_id=%s starting", run_id)
    lock = acquire_lock(settings)
    if lock is None:
        result = ExtractResult(status=STATUS_SKIPPED, run_id=run_id)
        _log_finished(result, started)
        return result
    progress = {"planned": 0, "committed": 0}
    try:
        try:
            result = _run_locked(
                settings,
                run_id=run_id,
                now_utc=clock,
                extract_start_date=start_date,
                client=client,
                connection_cm=connection_cm,
                migrate=migrate,
                schema=schema,
                progress=progress,
            )
        except _FAILURE_TYPES as exc:
            logger.error("Extraction failed run_id=%s error=%s", run_id, exc)
            result = ExtractResult(
                status=STATUS_FAILED,
                run_id=run_id,
                chunks_planned=progress["planned"],
                chunks_committed=progress["committed"],
                error=str(exc),
            )
    finally:
        lock.release()
    _log_finished(result, started)
    return result


def _run_locked(
    settings: Settings,
    *,
    run_id: str,
    now_utc: datetime,
    extract_start_date: Any,
    client: PartnerAPIClient | None,
    connection_cm: Callable,
    migrate: Callable[[Settings], None],
    schema: str,
    progress: dict[str, int],
) -> ExtractResult:
    """Migrate, verify the schema, and commit every planned window.

    The caller already holds the advisory lock. The API client is closed
    before this function returns, including when a later step fails.

    Args:
        settings: API and warehouse settings.
        run_id: Short id included in logs.
        now_utc: Clock instant for ``account_today``.
        extract_start_date: Planner baseline or backfill floor. May be
            ``None``.
        client: Optional API client. Created when omitted.
        connection_cm: Load-connection context manager.
        migrate: Warehouse DDL that must not share the loader transaction.
        schema: Warehouse schema.
        progress: Mutable ``planned`` and ``committed`` counts. Updated in
            place so a failure can still report them.

    Returns:
        A success result. Expected extraction errors propagate to the caller.
    """
    migrate(settings)
    api = client or PartnerAPIClient(settings)
    try:
        with connection_cm(settings) as conn:
            verify_schema(conn, schema=schema)
            account = api.get_account()
            state = read_state(conn, schema=schema)
            windows = plan_extract_windows(
                now_utc=now_utc,
                state=state,
                extract_start_date=extract_start_date,
            )
            progress["planned"] = len(windows)
            logger.info(
                "Extractor run_id=%s account_id=%s windows=%s kinds=%s",
                run_id,
                account.account_id,
                progress["planned"],
                ",".join(item.kind for item in windows) or "-",
            )
            if not windows:
                return ExtractResult(
                    status=STATUS_SUCCESS, run_id=run_id, chunks_planned=0
                )
            for window in windows:
                _process_chunk(api, conn, account, window, run_id=run_id, schema=schema)
                progress["committed"] += 1
            return ExtractResult(
                status=STATUS_SUCCESS,
                run_id=run_id,
                chunks_planned=progress["planned"],
                chunks_committed=progress["committed"],
            )
    finally:
        api.close()


def _process_chunk(
    client: PartnerAPIClient,
    conn: Any,
    account: Any,
    window: ReportWindow,
    *,
    run_id: str,
    schema: str,
) -> None:
    """Fetch, validate, and commit one window.

    Args:
        client: Authenticated API client.
        conn: Open load connection. The commit happens in this function.
        account: Account metadata used by validation.
        window: Inclusive date chunk and its kind.
        run_id: Short id included in logs.
        schema: Warehouse schema.

    Raises:
        PartnerAPIError: If the report cannot be fetched.
        ReportValidationError: If the snapshot fails an integrity check.
        RowValidationError: If a row fails normalization.
        StateConflictError: If currency or timezone changed.
        StorageError: If the slice replace or checkpoint write fails.
    """
    logger.info(
        "Processing chunk run_id=%s kind=%s window=%s..%s",
        run_id,
        window.kind,
        window.date_from.isoformat(),
        window.date_to.isoformat(),
    )
    snapshot = _fetch_validated(client, account, window)
    logger.info(
        "Validated snapshot run_id=%s report_id=%s rows=%s kind=%s",
        run_id,
        snapshot.report_id,
        len(snapshot.rows),
        window.kind,
    )
    _commit_snapshot(conn, snapshot, run_id=run_id, kind=window.kind, schema=schema)


def _fetch_validated(
    client: PartnerAPIClient, account: Any, window: ReportWindow
) -> ValidatedReport:
    """Submit one window, download it, and validate the snapshot.

    Args:
        client: Authenticated API client.
        account: Account metadata used by validation.
        window: Inclusive date chunk.

    Returns:
        The normalized snapshot. Nothing is written to PostgreSQL.

    Raises:
        PartnerAPIError: If submit, poll, or download fails.
        ReportValidationError: If the snapshot is incomplete or inconsistent.
        RowValidationError: If any row fails normalization.
    """
    date_from = window.date_from.isoformat()
    date_to = window.date_to.isoformat()
    done, rows = _complete_report(
        client, date_from, date_to, resubmits_left=REPORT_RESUBMIT_LIMIT
    )
    logger.info(
        "Fetched rows report_id=%s count=%s window=%s..%s",
        done.report_id,
        len(rows.rows),
        date_from,
        date_to,
    )
    return validate_report(done, rows, account, date_from, date_to)


def _complete_report(
    client: PartnerAPIClient,
    date_from: str,
    date_to: str,
    *,
    resubmits_left: int,
) -> tuple[Report, ReportRows]:
    """Submit a report and return it once its rows have been downloaded.

    One bounded resubmit covers a ``GET`` 404 after the mock restarts and
    forgets the job id. A later missing job propagates.

    Args:
        client: Authenticated API client.
        date_from: Inclusive start, ``YYYY-MM-DD``.
        date_to: Inclusive end, ``YYYY-MM-DD``.
        resubmits_left: Extra submits still allowed after
            :class:`ReportNotFoundError`.

    Returns:
        The ``DONE`` report and its downloaded rows.

    Raises:
        ReportNotFoundError: If the job is missing and no resubmits remain.
        PartnerAPIError: If submit, poll, or download fails for another reason.
    """
    submitted = client.submit_report(date_from, date_to)
    logger.info(
        "Submitted report_id=%s window=%s..%s",
        submitted.report_id,
        date_from,
        date_to,
    )
    try:
        done = client.wait_for_report(submitted.report_id)
        rows = client.fetch_rows(done.report_id)
        return done, rows
    except ReportNotFoundError:
        if resubmits_left <= 0:
            raise
        logger.warning(
            "Report id lost report_id=%s; bounded resubmit remaining=%s",
            submitted.report_id,
            resubmits_left,
        )
        return _complete_report(
            client, date_from, date_to, resubmits_left=resubmits_left - 1
        )


def _commit_snapshot(
    conn: Any,
    snapshot: ValidatedReport,
    *,
    run_id: str,
    kind: str,
    schema: str,
) -> tuple[LoadResult, ExtractState]:
    """Write the slice and checkpoint, then commit both.

    Any exception rolls the connection back and is re-raised, so a failed
    chunk leaves ``partner_report`` and ``extract_state`` unchanged.

    Args:
        conn: Open load connection. Must not autocommit.
        snapshot: Integrity-checked report.
        run_id: Short id included in the commit log.
        kind: Window kind, ``incremental`` or ``backfill``.
        schema: Warehouse schema.

    Returns:
        Load counts and the checkpoint that was committed.

    Raises:
        StateConflictError: If currency or timezone changed.
        StorageError: If the replace or checkpoint write fails.
    """
    extracted_at = datetime.now(timezone.utc)
    try:
        load_result, pending = apply_snapshot(
            conn, snapshot, extracted_at=extracted_at, schema=schema
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info(
        "Committed snapshot run_id=%s report_id=%s kind=%s window=%s..%s "
        "deleted=%s inserted=%s added=%s removed=%s updated=%s unchanged=%s "
        "loaded_through=%s settled_through=%s next_start=%s",
        run_id,
        snapshot.report_id,
        kind,
        snapshot.date_from.isoformat(),
        snapshot.date_to.isoformat(),
        load_result.deleted,
        load_result.inserted,
        load_result.added,
        load_result.removed,
        load_result.updated,
        load_result.unchanged,
        pending.loaded_through,
        pending.settled_through,
        pending.next_incremental_start,
    )
    return load_result, pending


def _log_finished(result: ExtractResult, started: float) -> None:
    """Log the run status and elapsed time.

    Args:
        result: Outcome already decided by the caller.
        started: ``time.monotonic`` value captured when the run began.
    """
    logger.info(
        "Extractor finished run_id=%s result=%s duration_s=%.3f "
        "chunks_committed=%s chunks_planned=%s",
        result.run_id,
        result.status,
        time.monotonic() - started,
        result.chunks_committed,
        result.chunks_planned,
    )
