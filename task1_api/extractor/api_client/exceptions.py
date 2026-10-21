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
        """Store the message, HTTP status, and partner error code.

        Args:
            message: Text returned by ``str(exc)``.
            status_code: HTTP status. ``None`` for a transport failure.
            error: Partner error code, when the body had one.
        """
        super().__init__(message)
        self.status_code = status_code
        self.error = error


class ReportNotFoundError(PartnerAPIError):
    """Raised when the partner has no job for ``report_id``.

    Orchestration must decide whether to submit a new report. This client
    does not resubmit.

    Attributes:
        report_id: Job id the partner did not know.
        status_code: Always ``404``.
        error: Always ``unknown_report``.
    """

    def __init__(self, report_id: str) -> None:
        """Store ``report_id`` as a 404 ``unknown_report`` error.

        Args:
            report_id: Job id from submit or from a later status or rows call.
        """
        super().__init__(
            f"report {report_id!r} was not found",
            status_code=404,
            error="unknown_report",
        )
        self.report_id = report_id


class ReportTimeoutError(PartnerAPIError):
    """Raised when polling does not reach ``DONE`` before the deadline.

    Attributes:
        report_id: Job that was still pending or running.
        last_status: Last status observed, or ``None`` if none was read.
        error: Always ``report_timeout``.
    """

    def __init__(self, report_id: str, last_status: str | None) -> None:
        """Store ``report_id`` and the last status seen before the deadline.

        Args:
            report_id: Job id that did not finish.
            last_status: Last partner status, or ``None``.
        """
        super().__init__(
            f"timed out waiting for report {report_id!r} "
            f"(last status={last_status})",
            error="report_timeout",
        )
        self.report_id = report_id
        self.last_status = last_status


class ReportStatusError(PartnerAPIError):
    """Raised when a report reaches a failed or unknown status.

    Attributes:
        report_id: Job id.
        status: Partner status that stopped polling.
        error: ``report_failed`` for ``FAILED``, ``ERROR``, and
            ``CANCELLED``. Any other non-running status is ``unknown_status``.
    """

    def __init__(self, report_id: str, status: str) -> None:
        """Store ``report_id`` and map ``status`` to a stable error code.

        Args:
            report_id: Job id.
            status: Terminal partner status other than ``DONE``.
        """
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


class RetryExhaustedError(PartnerAPIError):
    """Raised when retryable HTTP or transport errors keep failing.

    Attributes:
        attempts: Number of tries that were made.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error: str | None = None,
        attempts: int,
    ) -> None:
        """Store the HTTP status, partner error, and number of attempts.

        Args:
            message: Text returned by ``str(exc)``.
            status_code: Last HTTP status, or ``None`` for a transport failure.
            error: Last partner error code, when one was read.
            attempts: How many tries were made. Required.
        """
        super().__init__(message, status_code=status_code, error=error)
        self.attempts = attempts


class AmbiguousRequestError(PartnerAPIError):
    """Raised when a POST /v2/reports call may have created a job.

    The client does not retry in this case, to avoid duplicate report jobs.
    """
