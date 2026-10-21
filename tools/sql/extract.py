"""Pull physical tables out of one sqlglot statement.

CTE names and select aliases are not tables. Scope resolution drops them.
A scheduled-query SELECT has no SQL write target; the transfer-config
destination is applied later, not here.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from estate.identifiers import TableId, parse_table_id
from sql.models import Role, TableRef, UnresolvedRef

_WRITE_STATEMENTS = (
    exp.Insert,
    exp.Create,
    exp.Merge,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
)


def extract_statement(
    statement: exp.Expression,
    statement_index: int,
) -> tuple[list[TableRef], list[UnresolvedRef], list[str]]:
    """Extract reads, writes, and unresolved names from one statement.

    Args:
        statement: A sqlglot tree for a single statement.
        statement_index: Position of that statement in the script, starting at 0.

    Returns:
        References, unresolved names, and extraction messages. Messages are
        empty when scope resolution succeeds.
    """
    if isinstance(statement, exp.Command):
        return [], [_dynamic(statement, statement_index)], []

    messages: list[str] = []
    target = _write_target(statement)
    try:
        reads = _scope_reads(statement)
        reads.extend(_merge_using(statement))
    except (OptimizeError, SqlglotError) as exc:
        messages.append(exc.__class__.__name__ + ": " + _first_line(str(exc)))
        reads = []
    reads.extend(_like_sources(statement))
    if target is not None:
        reads = [table for table in reads if table is not target]

    references: list[TableRef] = []
    unresolved: list[UnresolvedRef] = []
    for table in reads:
        _classify(table, "read", statement_index, references, unresolved)
    if target is not None:
        _classify(target, "write", statement_index, references, unresolved)
    for table in statement.find_all(exp.Table):
        if _is_table_function(table):
            _classify(table, "read", statement_index, references, unresolved)
    return references, unresolved, messages


def _scope_reads(statement: exp.Expression) -> list[exp.Table]:
    tables: list[exp.Table] = []
    for scope in traverse_scope(statement):
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and not _is_table_function(source):
                tables.append(source)
    return tables


def _merge_using(statement: exp.Expression) -> list[exp.Table]:
    if not isinstance(statement, exp.Merge):
        return []
    using = statement.args.get("using")
    if isinstance(using, exp.Table):
        return [] if _is_table_function(using) else [using]
    if using is None:
        return []
    return _scope_reads(using)


def _like_sources(statement: exp.Expression) -> list[exp.Table]:
    return [
        table
        for table in statement.find_all(exp.Table)
        if isinstance(table.parent, exp.LikeProperty) and not _is_table_function(table)
    ]


def _write_target(statement: exp.Expression) -> exp.Table | None:
    if not isinstance(statement, _WRITE_STATEMENTS):
        return None
    node = statement.this
    if isinstance(node, exp.Schema):
        node = node.this
    if isinstance(node, exp.Table) and not _is_table_function(node):
        return node
    return None


def _classify(
    table: exp.Table,
    role: Role,
    statement_index: int,
    references: list[TableRef],
    unresolved: list[UnresolvedRef],
) -> None:
    if _is_table_function(table):
        unresolved.append(
            UnresolvedRef(
                raw=_function_name(table),
                reason="table_function",
                role=role,
                statement_index=statement_index,
            )
        )
        return
    parsed = _table_id(table)
    if parsed is None:
        unresolved.append(
            UnresolvedRef(
                raw=_raw_name(table) or "<unqualified>",
                reason="unqualified",
                role=role,
                statement_index=statement_index,
            )
        )
        return
    references.append(
        TableRef(
            table=parsed,
            raw=_raw_name(table),
            role=role,
            uncertain=False,
            statement_index=statement_index,
        )
    )


def _is_table_function(table: exp.Table) -> bool:
    """True for ``dataset.fn(args)``. UNNEST is not an ``exp.Table``."""
    return isinstance(table.this, exp.Anonymous)


def _function_name(table: exp.Table) -> str:
    inner = table.this.this if isinstance(table.this, exp.Anonymous) else table.this
    if isinstance(inner, exp.Identifier):
        return inner.this
    return (
        inner.sql(dialect="bigquery")
        if isinstance(inner, exp.Expression)
        else str(inner)
    )


def _table_id(table: exp.Table) -> TableId | None:
    """Build a TableId only when both dataset and table are static identifiers."""
    name = table.name
    dataset = table.db
    project = table.catalog or None
    if not name or not dataset or "." in name or "." in dataset:
        return None
    qualified = f"{project}.{dataset}.{name}" if project else f"{dataset}.{name}"
    try:
        return parse_table_id(qualified)
    except ValueError:
        return None


def _raw_name(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def _dynamic(statement: exp.Command, statement_index: int) -> UnresolvedRef:
    keyword = statement.this if isinstance(statement.this, str) else "COMMAND"
    return UnresolvedRef(
        raw=keyword,
        reason="dynamic",
        role="unknown",
        statement_index=statement_index,
    )


def command_text(statement: exp.Command) -> str:
    """Return the command tail as sqlglot stored it, for uncertain name recovery."""
    expression = statement.args.get("expression")
    if isinstance(expression, exp.Literal) and isinstance(expression.this, str):
        return expression.this
    if isinstance(expression, exp.Expression):
        return expression.sql(dialect="bigquery")
    return ""


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""
