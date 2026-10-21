"""Typed results of a BigQuery SQL parse.

A reference is certain only when sqlglot produced a tree and the table is a
physical read or write. Unqualified names, table functions, and dynamic SQL
stay unresolved. Recovered names from a failed parse are uncertain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, NamedTuple

from estate.identifiers import TableId

Role = Literal["read", "write", "unknown"]


class TableRef(NamedTuple):
    """One physical table mention.

    ``raw`` is the qualification sqlglot or the fallback recovered, without
    backticks and without the alias. ``uncertain`` is true when the statement
    did not parse cleanly, or when the name was taken from dynamic SQL.
    """

    table: TableId
    raw: str
    role: Role
    uncertain: bool
    statement_index: int


class UnresolvedRef(NamedTuple):
    """A reference that is not a static ``dataset.table`` name."""

    raw: str
    reason: str
    role: Role
    statement_index: int


class ParseIssue(NamedTuple):
    """A sqlglot or extraction failure, without the SQL body."""

    message: str
    line: int | None
    col: int | None


class SqlParse(NamedTuple):
    """Parse result for one scheduled-query body.

    ``parsed`` means sqlglot accepted the script. References stay in a stable
    order: statement index, role, uncertainty, then table name.
    """

    config_id: str | None
    path: Path | None
    parsed: bool
    references: tuple[TableRef, ...]
    unresolved: tuple[UnresolvedRef, ...]
    errors: tuple[ParseIssue, ...]

    @property
    def reads(self) -> tuple[TableRef, ...]:
        """Certain source tables."""
        return tuple(
            ref for ref in self.references if ref.role == "read" and not ref.uncertain
        )

    @property
    def writes(self) -> tuple[TableRef, ...]:
        """Certain DML or DDL targets."""
        return tuple(
            ref for ref in self.references if ref.role == "write" and not ref.uncertain
        )
