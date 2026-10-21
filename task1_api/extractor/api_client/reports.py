"""Report submission and asynchronous status polling."""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import TYPE_CHECKING, Any

import httpx

from config import (
    REPORT_DIMENSIONS,
    REPORT_MAX_SPAN_DAYS,
    REPORT_METRICS,
    REPORT_STATUS_DONE,
    REPORT_STATUSES_IN_PROGRESS,
    REPORTS_PATH,
    report_status_path,
)

from .exceptions import (
    PartnerAPIError,
    ReportNotFoundError,
    ReportStatusError,
    ReportTimeoutError,
)
from .models import Report

if TYPE_CHECKING:
    from .client import PartnerAPIClient

logger = logging.getLogger(__name__)


def request_report(
    client: PartnerAPIClient,
    date_from: str,
    date_to: str,
) -> Report:
    """Submit an asynchronous report for an inclusive date window.

    Args:
        client: Authenticated HTTP client.
        date_from: First report date, ``YYYY-MM-DD``.
        date_to: Last report date, ``YYYY-MM-DD``.

    Returns:
        The accepted job (``report_id`` and initial status).

    Raises:
        PartnerAPIError: If the window is invalid or the request fails.
    """
    _validate_report_window(date_from, date_to)
    payload = {
        "date_from": date_from,
        "date_to": date_to,
        "dimensions": list(REPORT_DIMENSIONS),
        "metrics": list(REPORT_METRICS),
    }
    logger.info(
        "POST %s date_from=%s date_to=%s",
        REPORTS_PATH,
        date_from,
        date_to,
    )
    response = client._authenticated("POST", REPORTS_PATH, json=payload)
    client._ensure_success(response, REPORTS_PATH)
    report = _parse_report(client, response, REPORTS_PATH)
    logger.info(
        "Submitted report_id=%s status=%s",
        report.report_id,
        report.status,
    )
    return report


def request_report_status(client: PartnerAPIClient, report_id: str) -> Report:
    """Fetch the current status of a report job.

    Args:
        client: Authenticated HTTP client.
        report_id: Id returned by :func:`request_report`.

    Returns:
        Current ``report_id``, ``status``, and ``row_count`` when present.

    Raises:
        ReportNotFoundError: If the partner has no such job.
        PartnerAPIError: If the request or payload is invalid.
    """
    path = report_status_path(report_id)
    logger.debug("GET %s", path)
    response = client._authenticated("GET", path)
    if response.status_code == 404:
        raise ReportNotFoundError(report_id)
    client._ensure_success(response, path)
    return _parse_report(client, response, path)


def poll_until_done(client: PartnerAPIClient, report_id: str) -> Report:
    """Poll report status until ``DONE`` or a terminal error.

    Args:
        client: Authenticated HTTP client.
        report_id: Id returned by :func:`request_report`.

    Returns:
        The completed report, including ``row_count``.

    Raises:
        ReportNotFoundError: If the job disappears (no resubmit).
        ReportTimeoutError: If ``DONE`` is not reached in time.
        ReportStatusError: If the job fails or returns an unknown status.
        PartnerAPIError: If a status request fails.
    """
    deadline = time.monotonic() + client._settings.poll_timeout_s
    last_status: str | None = None
    while True:
        report = request_report_status(client, report_id)
        last_status = report.status
        logger.debug(
            "Report report_id=%s status=%s row_count=%s",
            report.report_id,
            report.status,
            report.row_count,
        )
        if report.status == REPORT_STATUS_DONE:
            logger.info(
                "Report report_id=%s status=%s row_count=%s",
                report.report_id,
                report.status,
                report.row_count,
            )
            return report
        if report.status not in REPORT_STATUSES_IN_PROGRESS:
            raise ReportStatusError(report.report_id, report.status)
        if time.monotonic() >= deadline:
            logger.warning(
                "Timed out waiting for report_id=%s last_status=%s",
                report_id,
                last_status,
            )
            raise ReportTimeoutError(report_id, last_status)
        time.sleep(client._settings.poll_interval_s)


def _validate_report_window(date_from: str, date_to: str) -> None:
    """Reject windows the partner would refuse before the request is sent.

    The inclusive span may be at most ``REPORT_MAX_SPAN_DAYS + 1`` days.

    Args:
        date_from: Inclusive start, ``YYYY-MM-DD``.
        date_to: Inclusive end, ``YYYY-MM-DD``.

    Raises:
        PartnerAPIError: ``bad_date`` when a value is not a calendar date,
            ``bad_range`` when ``date_to`` is before ``date_from``, or
            ``range_too_wide`` when the window exceeds seven inclusive days.
    """
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError as exc:
        raise PartnerAPIError(
            "date_from and date_to must be YYYY-MM-DD",
            error="bad_date",
        ) from exc
    if end < start:
        raise PartnerAPIError(
            f"date_to {date_to} is before date_from {date_from}",
            error="bad_range",
        )
    span = (end - start).days
    if span > REPORT_MAX_SPAN_DAYS:
        raise PartnerAPIError(
            f"report window {date_from}..{date_to} exceeds "
            f"{REPORT_MAX_SPAN_DAYS + 1} inclusive days",
            error="range_too_wide",
        )


def _parse_report(
    client: PartnerAPIClient,
    response: httpx.Response,
    path: str,
) -> Report:
    """Read ``report_id``, ``status``, and an optional ``row_count``.

    Args:
        client: Client used to decode the JSON object.
        response: Successful submit or status response.
        path: Request path included in error messages.

    Returns:
        The job. ``row_count`` is required when ``status`` is ``DONE``.

    Raises:
        PartnerAPIError: If the id, status, or a ``DONE`` row count is invalid.
    """
    body = client._json_object(response, path)
    report_id = body.get("report_id")
    status = body.get("status")
    if not isinstance(report_id, str) or not report_id:
        raise PartnerAPIError(
            f"{response.request.method} {path} returned invalid report_id",
            status_code=response.status_code,
        )
    if not isinstance(status, str) or not status:
        raise PartnerAPIError(
            f"{response.request.method} {path} returned invalid status",
            status_code=response.status_code,
        )
    row_count = _parse_row_count(body.get("row_count"), path, response.status_code)
    if status == REPORT_STATUS_DONE and row_count is None:
        raise PartnerAPIError(
            f"GET {path} DONE report missing row_count",
            status_code=response.status_code,
        )
    return Report(report_id=report_id, status=status, row_count=row_count)


def _parse_row_count(
    value: Any,
    path: str,
    status_code: int,
) -> int | None:
    """Return a non-negative row count, or ``None`` when the field is absent.

    Args:
        value: Raw ``row_count`` from the JSON body.
        path: Request path included in the error message.
        status_code: HTTP status attached to a parse error.

    Returns:
        ``None`` when ``value`` is ``None``, otherwise the count.

    Raises:
        PartnerAPIError: If ``value`` is present and not a non-negative
            ``int``. Booleans are rejected.
    """
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PartnerAPIError(
            f"{path} returned invalid row_count",
            status_code=status_code,
        )
    return value
