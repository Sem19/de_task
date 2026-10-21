"""Shared HTTP client, authentication headers, and connection lifecycle."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import Settings

from .auth import fetch_account, obtain_access_token, refresh_access_token
from .exceptions import PartnerAPIError
from .models import Account, Report, ReportRows
from .pagination import download_rows
from .reports import poll_until_done, request_report, request_report_status
from .retry import (
    SlidingWindowLimiter,
    execute_with_retries,
    is_token_refresh_error,
    partner_error_code,
)

logger = logging.getLogger(__name__)


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
        """Open the shared HTTP client and start with an empty token cache."""
        self._settings = settings
        self._http = httpx.Client(
            base_url=settings.api_base,
            timeout=settings.request_timeout_s,
            transport=transport,
        )
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._v2_limiter = SlidingWindowLimiter(settings.v2_rate_limit_per_min)

    def __enter__(self) -> PartnerAPIClient:
        """Return this client without opening another connection.

        Returns:
            This instance.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the HTTP client.

        Args:
            *exc: Exception type, value, and traceback from the ``with``
                block. They are ignored.

        Returns:
            ``None``, so an exception inside the block is not suppressed.
        """
        self.close()

    @property
    def is_closed(self) -> bool:
        """Return whether the underlying HTTP connection pool has been closed.

        Returns:
            ``True`` after :meth:`close` or after leaving a ``with`` block.
        """
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
        return obtain_access_token(self)

    def get_account(self) -> Account:
        """Fetch account reporting metadata.

        Returns:
            Currency, timezone, and attribution window from the partner.

        Raises:
            PartnerAPIError: If authentication or the account request fails.
        """
        return fetch_account(self)

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
        return request_report(self, date_from, date_to)

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
        return request_report_status(self, report_id)

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
        return poll_until_done(self, report_id)

    def fetch_rows(self, report_id: str) -> ReportRows:
        """Download every page of a completed report.

        Pages with ``offset``, ``limit=1000``, and a stable ``order_by``.
        Offset advances by ``len(page)``, not by the requested limit.

        Args:
            report_id: Id of a job that has reached ``DONE``.

        Returns:
            All raw rows and the partner completeness metadata.

        Raises:
            ReportNotFoundError: If the job is unknown.
            ReportPaginationError: If the report is truncated or pages
                disagree about counts, metadata, or ``has_more``.
            PartnerAPIError: If a page request or payload is invalid.
        """
        return download_rows(self, report_id)

    def _authenticated(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send an authorized request and retry once after a stale token.

        A 401 is retried only when the partner error is ``token_expired`` or
        ``invalid_token``. Other 401 responses are returned unchanged.

        Args:
            method: HTTP method.
            path: Request path relative to the API origin.
            **kwargs: Passed to :meth:`_send`. A caller ``headers`` mapping
                is copied and given an ``Authorization`` bearer value.

        Returns:
            The first response, or the response after one token refresh.
        """
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self.get_token()}"
        response = self._send(method, path, headers=headers, **kwargs)
        if response.status_code != 401:
            return response
        error = partner_error_code(response)
        if not is_token_refresh_error(error):
            return response
        logger.info(
            "Retrying %s %s once after %s",
            method,
            path,
            error,
        )
        headers["Authorization"] = f"Bearer {refresh_access_token(self)}"
        return self._send(method, path, headers=headers, **kwargs)

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one request through retries and the local ``/v2`` limiter.

        Args:
            method: HTTP method.
            path: Request path.
            **kwargs: Passed to ``httpx.Client.request``.

        Returns:
            The response from :func:`execute_with_retries`.

        Raises:
            AmbiguousRequestError: A ``POST /v2/reports`` attempt may have
                created a job.
            RetryExhaustedError: Retryable failures used every attempt.
        """
        return execute_with_retries(
            lambda: self._http.request(method, path, **kwargs),
            method,
            path,
            settings=self._settings,
            limiter=self._v2_limiter,
        )

    def _ensure_success(self, response: httpx.Response, path: str) -> None:
        """Raise when ``response`` is not a successful HTTP status.

        Args:
            response: Response to check.
            path: Path included in the error message.

        Raises:
            PartnerAPIError: If the status is not 2xx. The partner error
                code is attached when the body contains one.
        """
        if response.is_success:
            return
        error = partner_error_code(response)
        status = response.status_code
        suffix = f" {error}" if error else ""
        raise PartnerAPIError(
            f"{response.request.method} {path} failed: HTTP {status}{suffix}",
            status_code=status,
            error=error,
        )

    def _json_object(self, response: httpx.Response, path: str) -> dict[str, Any]:
        """Decode a JSON object body.

        Args:
            response: Response whose body should be a JSON object.
            path: Path included in the error message.

        Returns:
            The decoded object.

        Raises:
            PartnerAPIError: If the body is not JSON or not an object.
        """
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
