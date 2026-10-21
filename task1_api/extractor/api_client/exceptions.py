"""Exceptions raised by the partner API client."""

from __future__ import annotations

from config import REPORT_STATUSES_FAILED


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


class ReportPaginationError(PartnerAPIError):
    """Raised when a report page is truncated or pagination is inconsistent."""
