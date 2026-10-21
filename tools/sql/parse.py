"""Parse scheduled-query SQL and list physical table references.

sqlglot's BigQuery dialect parses the script. A parse error does not raise:
explicitly qualified backtick names are recovered and marked uncertain.
The original SQL text is never rewritten.
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from estate.identifiers import TableId, parse_table_id
from estate.models import Inventory
from sql.extract import command_text, extract_statement
from sql.models import ParseIssue, Role, SqlParse, TableRef, UnresolvedRef

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_QUALIFIED = re.compile(
    r"`(?P<project>[A-Za-z][A-Za-z0-9_-]*)"
    r"\.(?P<dataset>[A-Za-z_][A-Za-z0-9_]*)"
    r"\.(?P<table>[A-Za-z_][A-Za-z0-9_]*)`"
    r"|`(?P<dataset2>[A-Za-z_][A-Za-z0-9_]*)"
    r"\.(?P<table2>[A-Za-z_][A-Za-z0-9_]*)`"
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def parse_sql(
    sql: str,
    *,
    config_id: str | None = None,
    path: Path | None = None,
) -> SqlParse:
    """Parse one SQL script.

    Args:
        sql: Statement text. Not modified.
        config_id: Scheduled-query id, when the caller has one.
        path: Source file, when the caller has one.

    Returns:
        References, unresolved names, and parse issues. Failure is a result,
        not an exception.
    """
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except ParseError as exc:
        return _failed(sql, exc, config_id, path)

    references: list[TableRef] = []
    unresolved: list[UnresolvedRef] = []
    messages: list[str] = []
    for index, statement in enumerate(statements):
        if statement is None:
            continue
        refs, pending, notes = extract_statement(statement, index)
        references.extend(refs)
        unresolved.extend(pending)
        messages.extend(notes)
        if isinstance(statement, exp.Command):
            references.extend(
                qualified_references(
                    command_text(statement), index, uncertain=True, role="unknown"
                )
            )
    return SqlParse(
        config_id=config_id,
        path=path,
        parsed=True,
        references=_unique_refs(references),
        unresolved=_unique_unresolved(unresolved),
        errors=tuple(ParseIssue(message, None, None) for message in messages),
    )


def parse_inventory(inventory: Inventory) -> tuple[SqlParse, ...]:
    """Parse each paired scheduled query, in config_id order.

    Args:
        inventory: Result of ``load_inventory``. Unpaired SQL files are not
            included; the loader already reports those as findings.

    Returns:
        One result per paired query.
    """
    return tuple(
        parse_sql(query.sql_text, config_id=query.config_id, path=query.sql_path)
        for query in inventory.queries
    )


def format_issue(result: SqlParse, issue: ParseIssue) -> str:
    """Format one issue with config id or path, and line when known.

    Args:
        result: Parse result that owns the issue.
        issue: Error to format. The SQL body is not included.

    Returns:
        A single-line description.
    """
    where = result.config_id or (
        str(result.path) if result.path is not None else "<sql>"
    )
    if issue.line is None:
        return f"{where}: {issue.message}"
    column = issue.col if issue.col is not None else 0
    return f"{where}:{issue.line}:{column}: {issue.message}"


def qualified_references(
    sql: str,
    statement_index: int,
    *,
    uncertain: bool,
    role: Role,
) -> list[TableRef]:
    """Recover backtick-qualified table names from text.

    Comments are ignored. Unqualified names are not guessed. A three-part name
    keeps its project. Results are marked with the caller's role and certainty.

    Args:
        sql: Text to scan. Not modified.
        statement_index: Statement position stored on each reference.
        uncertain: True when the surrounding statement did not parse cleanly.
        role: ``read``, ``write``, or ``unknown``.

    Returns:
        Table references in source order, before global sorting.
    """
    found: list[TableRef] = []
    for match in _QUALIFIED.finditer(_strip_comments(sql)):
        project = match.group("project")
        if project is not None:
            dataset = match.group("dataset")
            table = match.group("table")
            raw = f"{project}.{dataset}.{table}"
        else:
            dataset = match.group("dataset2")
            table = match.group("table2")
            raw = f"{dataset}.{table}"
        try:
            parsed = parse_table_id(raw)
        except ValueError:
            continue
        found.append(
            TableRef(
                table=parsed,
                raw=raw,
                role=role,
                uncertain=uncertain,
                statement_index=statement_index,
            )
        )
    return found


def _failed(
    sql: str, exc: ParseError, config_id: str | None, path: Path | None
) -> SqlParse:
    references = qualified_references(sql, 0, uncertain=True, role="unknown")
    return SqlParse(
        config_id=config_id,
        path=path,
        parsed=False,
        references=_unique_refs(references),
        unresolved=(),
        errors=_issues(exc),
    )


def _issues(exc: ParseError) -> tuple[ParseIssue, ...]:
    issues: list[ParseIssue] = []
    for error in exc.errors or []:
        description = error.get("description")
        message = str(description) if description else "parse error"
        line = error.get("line")
        col = error.get("col")
        issues.append(ParseIssue(message, _as_int(line), _as_int(col)))
    if issues:
        return tuple(issues)
    return (ParseIssue(_ANSI.sub("", _first_line(str(exc))), None, None),)


def _unique_refs(references: list[TableRef]) -> tuple[TableRef, ...]:
    chosen: dict[tuple[object, ...], TableRef] = {}
    for ref in references:
        key = _ref_key(ref)
        chosen.setdefault(key, ref)
    return tuple(sorted(chosen.values(), key=_ref_key))


def _unique_unresolved(items: list[UnresolvedRef]) -> tuple[UnresolvedRef, ...]:
    chosen: dict[tuple[object, ...], UnresolvedRef] = {}
    for item in items:
        key = (item.statement_index, item.role, item.reason, item.raw)
        chosen.setdefault(key, item)
    return tuple(
        sorted(
            chosen.values(),
            key=lambda item: (item.statement_index, item.role, item.reason, item.raw),
        )
    )


def _ref_key(ref: TableRef) -> tuple[object, ...]:
    table: TableId = ref.table
    return (
        ref.statement_index,
        ref.role,
        ref.uncertain,
        table.project or "",
        table.dataset,
        table.table,
    )


def _strip_comments(sql: str) -> str:
    without_blocks = _BLOCK_COMMENT.sub(" ", sql)
    return _LINE_COMMENT.sub("", without_blocks)


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else "parse error"
