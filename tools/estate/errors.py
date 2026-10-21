"""Errors raised when an estate input cannot be read."""

from __future__ import annotations


class EstateInputError(Exception):
    """A required input is missing, unreadable, or not valid JSON or CSV.

    Source inconsistencies inside a readable estate are findings, not this error.
    """
