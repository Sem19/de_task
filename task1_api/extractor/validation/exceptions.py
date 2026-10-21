"""Exceptions raised while validating partner report rows and snapshots."""

from __future__ import annotations


class RowValidationError(ValueError):
    """Raised when a report row is malformed or missing required fields.

    Attributes:
        field: API field that failed, or ``"row"`` when the payload is not
            an object.
        index: Zero-based row index when known.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str,
        index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.index = index


class ReportValidationError(ValueError):
    """Raised when a completed report snapshot fails an integrity check.

    Attributes:
        error: Stable machine-readable reason (``truncated``,
            ``duplicate_grain``, ``currency_mismatch``, …).
    """

    def __init__(self, message: str, *, error: str) -> None:
        super().__init__(message)
        self.error = error
