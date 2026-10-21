"""HTTP client for the partner Traffic & Revenue API."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from config import (
    ACCOUNT_PATH,
    GRANT_TYPE,
    OAUTH_TOKEN_PATH,
    REPORT_DIMENSIONS,
    REPORT_MAX_SPAN_DAYS,
    REPORT_METRICS,
    REPORT_STATUS_DONE,
    REPORT_STATUSES_FAILED,
    REPORT_STATUSES_IN_PROGRESS,
    REPORTS_PATH,
    Settings,
    report_status_path,
)

logger = logging.getLogger(__name__)


class PartnerAPIError(Exception):
    """Raised when the partner API returns an error or an unexpected payload.

    Attributes:
        status_code: HTTP status, or ``None`` for transport failures.
        error: Partner error code when present in the response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error


class ReportNotFoundError(PartnerAPIError):
    """Raised when the partner has no job for ``report_id``.

    Orchestration must decide whether to submit a new report. This client
    does not resubmit.
    """

    def __init__(self, report_id: str) -> None:
        super().__init__(
            f"report {report_id!r} was not found",
            status_code=404,
            error="unknown_report",
        )
        self.report_id = report_id


class ReportTimeoutError(PartnerAPIError):
    """Raised when polling does not reach ``DONE`` before the deadline."""

    def __init__(self, report_id: str, last_status: str | None) -> None:
        super().__init__(
            f"timed out waiting for report {report_id!r} "
            f"(last status={last_status})",
            error="report_timeout",
        )
        self.report_id = report_id
        self.last_status = last_status


class ReportStatusError(PartnerAPIError):
    """Raised when a report reaches a failed or unknown status."""

    def __init__(self, report_id: str, status: str) -> None:
        error = (
            "report_failed" if status in REPORT_STATUSES_FAILED else "unknown_status"
        )
        super().__init__(
            f"report {report_id!r} ended with status {status!r}",
            error=error,
        )
        self.report_id = report_id
        self.status = status


@dataclass(frozen=True)
class Account:
    """Account metadata from ``GET /v2/account``.

    Attributes:
        account_id: Partner account identifier.
        timezone: Reporting timezone label returned by the API.
        currency: Reporting currency code returned by the API.
        attribution_window_days: Number of trailing dates that still restate.
    """

    account_id: str
    timezone: str
    currency: str
    attribution_window_days: int


@dataclass(frozen=True)
class Report:
    """A submitted or polled report job.

    Attributes:
        report_id: Partner job identifier.
        status: Partner status string (``PENDING``, ``RUNNING``, ``DONE``, …).
        row_count: Total rows once the job is ``DONE``; otherwise ``None``.
    """

    report_id: str
    status: str
    row_count: int | None = None


class PartnerAPIClient:
    """Client for OAuth, account metadata, and asynchronous reports.

    Reuses a single ``httpx.Client``. Obtain an instance, use it, then call
    :meth:`close` or use the client as a context manager.

    Args:
        settings: API origin, credentials, and timeouts.
        transport: Optional httpx transport. Used by tests to mock HTTP.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._http = httpx.Client(
            base_url=settings.api_base,
            timeout=settings.request_timeout_s,
            transport=transport,
        )
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def __enter__(self) -> PartnerAPIClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def is_closed(self) -> bool:
        """Whether the underlying HTTP connection pool has been closed."""
        return self._http.is_closed

    def close(self) -> None:
        """Close the shared ``httpx.Client``."""
        self._http.close()

    def get_token(self) -> str:
        """Return a valid bearer token, refreshing when it is close to expiry.

        Returns:
            Cached or newly issued access token.

        Raises:
            PartnerAPIError: If the token request fails or the body is invalid.
        """
        if self._access_token and self._token_is_fresh():
            return self._access_token
        return self._fetch_token()

    def get_account(self) -> Account:
        """Fetch account reporting metadata.

        Returns:
            Currency, timezone, and attribution window from the partner.

        Raises:
            PartnerAPIError: If authentication or the account request fails.
        """
        logger.info("GET %s", ACCOUNT_PATH)
        response = self._authenticated("GET", ACCOUNT_PATH)
        self._ensure_success(response, ACCOUNT_PATH)
        return self._parse_account(response)

    def submit_report(self, date_from: str, date_to: str) -> Report:
        """Submit an asynchronous report for an inclusive date window.

        Args:
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
        response = self._authenticated("POST", REPORTS_PATH, json=payload)
        self._ensure_success(response, REPORTS_PATH)
        report = _parse_report(response, REPORTS_PATH)
        logger.info(
            "Submitted report_id=%s status=%s",
            report.report_id,
            report.status,
        )
        return report

    def get_report(self, report_id: str) -> Report:
        """Fetch the current status of a report job.

        Args:
            report_id: Id returned by :meth:`submit_report`.

        Returns:
            Current ``report_id``, ``status``, and ``row_count`` when present.

        Raises:
            ReportNotFoundError: If the partner has no such job.
            PartnerAPIError: If the request or payload is invalid.
        """
        path = report_status_path(report_id)
        logger.info("GET %s", path)
        response = self._authenticated("GET", path)
        if response.status_code == 404:
            raise ReportNotFoundError(report_id)
        self._ensure_success(response, path)
        return _parse_report(response, path)

    def wait_for_report(self, report_id: str) -> Report:
        """Poll report status until ``DONE`` or a terminal error.

        Args:
            report_id: Id returned by :meth:`submit_report`.

        Returns:
            The completed report, including ``row_count``.

        Raises:
            ReportNotFoundError: If the job disappears (no resubmit).
            ReportTimeoutError: If ``DONE`` is not reached in time.
            ReportStatusError: If the job fails or returns an unknown status.
            PartnerAPIError: If a status request fails.
        """
        deadline = time.monotonic() + self._settings.poll_timeout_s
        last_status: str | None = None
        while True:
            report = self.get_report(report_id)
            last_status = report.status
            logger.info(
                "Report report_id=%s status=%s row_count=%s",
                report.report_id,
                report.status,
                report.row_count,
            )
            if report.status == REPORT_STATUS_DONE:
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
            time.sleep(self._settings.poll_interval_s)

    def _authenticated(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self.get_token()}"
        return self._send(method, path, headers=headers, **kwargs)

    def _token_is_fresh(self) -> bool:
        refresh_at = self._token_expires_at - self._settings.token_refresh_margin_s
        return time.monotonic() < refresh_at

    def _fetch_token(self) -> str:
        logger.info("POST %s", OAUTH_TOKEN_PATH)
        response = self._send(
            "POST",
            OAUTH_TOKEN_PATH,
            json={
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "grant_type": GRANT_TYPE,
            },
        )
        self._ensure_success(response, OAUTH_TOKEN_PATH)
        token, expires_in = self._parse_token(response)
        self._access_token = token
        self._token_expires_at = time.monotonic() + expires_in
        logger.info("Obtained access token expires_in=%s", expires_in)
        return token

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise PartnerAPIError(f"{method} {path} failed: {exc}") from exc

    def _ensure_success(self, response: httpx.Response, path: str) -> None:
        if response.is_success:
            return
        error = _error_code(response)
        status = response.status_code
        suffix = f" {error}" if error else ""
        raise PartnerAPIError(
            f"{response.request.method} {path} failed: HTTP {status}{suffix}",
            status_code=status,
            error=error,
        )

    def _parse_token(self, response: httpx.Response) -> tuple[str, int]:
        body = _json_object(response, OAUTH_TOKEN_PATH)
        token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not isinstance(token, str) or not token:
            raise PartnerAPIError(
                "POST /oauth/token returned no access_token",
                status_code=response.status_code,
            )
        if not isinstance(expires_in, int) or isinstance(expires_in, bool):
            raise PartnerAPIError(
                "POST /oauth/token returned invalid expires_in",
                status_code=response.status_code,
            )
        return token, expires_in

    def _parse_account(self, response: httpx.Response) -> Account:
        body = _json_object(response, ACCOUNT_PATH)
        try:
            account_id = body["account_id"]
            timezone = body["timezone"]
            currency = body["currency"]
            window = body["attribution_window_days"]
        except KeyError as exc:
            raise PartnerAPIError(
                f"GET {ACCOUNT_PATH} missing field {exc.args[0]!r}",
                status_code=response.status_code,
            ) from exc
        if not isinstance(account_id, str) or not account_id:
            raise PartnerAPIError("GET /v2/account returned invalid account_id")
        if not isinstance(timezone, str) or not timezone:
            raise PartnerAPIError("GET /v2/account returned invalid timezone")
        if not isinstance(currency, str) or not currency:
            raise PartnerAPIError("GET /v2/account returned invalid currency")
        if not isinstance(window, int) or isinstance(window, bool) or window < 0:
            raise PartnerAPIError(
                "GET /v2/account returned invalid attribution_window_days",
            )
        logger.info(
            "Fetched account_id=%s currency=%s timezone=%s attribution_window_days=%s",
            account_id,
            currency,
            timezone,
            window,
        )
        return Account(
            account_id=account_id,
            timezone=timezone,
            currency=currency,
            attribution_window_days=window,
        )


def _json_object(response: httpx.Response, path: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PartnerAPIError(
            f"{response.request.method} {path} returned non-JSON body",
            status_code=response.status_code,
        ) from exc
    if not isinstance(payload, dict):
        raise PartnerAPIError(
            f"{response.request.method} {path} returned a non-object JSON body",
            status_code=response.status_code,
        )
    return payload


def _error_code(response: httpx.Response) -> str | None:
    """Read the partner error code from either response envelope."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail", payload)
    if isinstance(detail, dict):
        error = detail.get("error")
        return str(error) if error is not None else None
    if isinstance(detail, str) and detail:
        return detail
    error = payload.get("error")
    return str(error) if error is not None else None


def _validate_report_window(date_from: str, date_to: str) -> None:
    """Reject windows the partner would refuse as ``range_too_wide`` or invalid."""
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


def _parse_report(response: httpx.Response, path: str) -> Report:
    body = _json_object(response, path)
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
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PartnerAPIError(
            f"{path} returned invalid row_count",
            status_code=status_code,
        )
    return value
