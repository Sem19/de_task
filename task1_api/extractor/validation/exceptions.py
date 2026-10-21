"""Exceptions raised while validating partner report rows."""

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
