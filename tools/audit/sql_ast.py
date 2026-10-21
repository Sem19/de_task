"""SQLGlot helpers for projection, column binding, lookback, and partitions.

These functions describe the checked-in statement. They do not execute SQL.
A parse failure returns None or an uncertain result. OptimizeError is recorded
the same way: the query is not given a guessed binding.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Literal, NamedTuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

ColumnState = Literal["present", "absent", "conflict", "unknown"]
_CURRENT = (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentDatetime)
_SUB = (exp.DateSub, exp.TimestampSub, exp.DatetimeSub)
_ADD = (exp.DateAdd, exp.TimestampAdd, exp.DatetimeAdd)
_PSEUDO = {"_PARTITIONTIME", "_PARTITIONDATE"}


class NameStatus(NamedTuple):
    """Output names derived from a select list or a set of writers.

    ``present`` names are produced by every definition. ``absent`` names were
    removed, usually by EXCEPT, and were not selected again. ``closed`` means
    ``present`` is the full output. ``covered`` means every definition is
    closed, so a name outside ``present_union`` is absent and a name in the
    union but not in ``present`` is a writer conflict.
    """

    present: frozenset[str]
    absent: frozenset[str]
    closed: bool
    covered: bool
    present_union: frozenset[str]


class Retention(NamedTuple):
    """Outer-statement lookback.

    ``days`` is set only for a simple lower bound against the current date.
    ``uncertain`` means a date interval is present but is not that pattern.
    """

    days: int | None
    uncertain: bool


class PartitionCoverage(NamedTuple):
    """How partition predicates bind to partitioned sources in one statement."""

    filtered: frozenset[str]
    unfiltered: frozenset[str]
    ambiguous: frozenset[str]


EMPTY_STATUS = NameStatus(frozenset(), frozenset(), False, False, frozenset())


def projection_count(sql: str) -> int | None:
    """Return the outer output width, or None when it is not a fixed list.

    CTE bodies and nested FROM selects are ignored. A star, including
    ``table.*``, makes the width unknown. UNION branches must agree. The last
    statement is the scheduled-query result.

    Args:
        sql: Scheduled-query text.

    Returns:
        The number of outer expressions, or None.
    """
    root = _root(sql)
    if root is None:
        return None
    return _count_output(root)


def output_schema(
    sql: str,
    resolve: Callable[[str], NameStatus | None] | None = None,
) -> NameStatus | None:
    """Derive output names for the last statement.

    Args:
        sql: Scheduled-query text.
        resolve: Maps ``dataset.table`` to a known status. Used to expand one
            unqualified star from a single physical source. None skips expansion.

    Returns:
        The status, or None when the statement does not parse.
    """
    root = _root(sql)
    if root is None:
        return None
    return _schema(root, resolve)


def merge_schemas(schemas: tuple[NameStatus, ...] | list[NameStatus]) -> NameStatus:
    """Combine writer schemas without picking a column that only one writer has.

    Args:
        schemas: One status per writer that parsed.

    Returns:
        A status whose ``present`` names are shared by every writer.
    """
    if not schemas:
        return EMPTY_STATUS
    if len(schemas) == 1:
        return schemas[0]
    present = frozenset.intersection(*(item.present for item in schemas))
    absent = frozenset.intersection(*(item.absent for item in schemas))
    if all(item.closed for item in schemas):
        union = frozenset.union(*(item.present for item in schemas))
        agreed = all(item.present == schemas[0].present for item in schemas)
        return NameStatus(
            present, frozenset(), agreed, True, union if not agreed else present
        )
    covered = all(item.closed or item.covered for item in schemas)
    union = frozenset.union(*(item.present_union for item in schemas))
    return NameStatus(present, absent, False, covered, union)


def column_state(schema: NameStatus, name: str) -> ColumnState:
    """Classify one output name. Unknown means the star was not expanded."""
    if name in schema.present and name not in schema.absent:
        return "present"
    if name in schema.absent:
        return "absent"
    if schema.closed and name not in schema.present:
        return "absent"
    if schema.covered and name in schema.present_union and name not in schema.present:
        return "conflict"
    if schema.covered and name not in schema.present_union:
        return "absent"
    return "unknown"


def columns_by_table(sql: str) -> dict[str, set[str]] | None:
    """Map ``dataset.table`` to column names read from that physical table.

    sqlglot qualify binds unambiguous columns. When qualify cannot resolve a
    name, only columns the SQL already qualifies are kept. Unqualified names
    in a join are not assigned to a table.

    Args:
        sql: Scheduled-query text.

    Returns:
        The map, or None when the SQL does not parse. An empty dict means no
        physical column reference was resolved.
    """
    try:
        statements = [
            tree for tree in sqlglot.parse(sql, read="bigquery") if tree is not None
        ]
    except ParseError:
        return None
    found: dict[str, set[str]] = defaultdict(set)
    for statement in statements:
        try:
            tree = qualify(statement.copy(), dialect="bigquery")
        except OptimizeError:
            tree = statement
        _accumulate_columns(tree, found)
    return dict(found)


def retention_days(sql: str) -> Retention:
    """Return the outer WHERE lookback in days.

    Only ``column >= current_date - INTERVAL n DAY`` (and the same shape with
    TIMESTAMP or DATETIME) counts. An interval in the select list, a date
    spine, an OR, or a bound that exists only inside a CTE is not a retention
    window. Those cases return ``uncertain`` or no day count.

    Args:
        sql: Scheduled-query text.

    Returns:
        The lookback. Parse failure is uncertain and has no day count.
    """
    root = _root(sql)
    if root is None:
        return Retention(None, True)
    branches = _branch_retentions(root)
    outer = [where for where in _outer_wheres(root)]
    nested = [where for where in root.find_all(exp.Where) if where not in outer]
    if any(item.uncertain for item in branches):
        return Retention(None, True)
    counts = {item.days for item in branches}
    nested_date = any(_has_date_shift(where) for where in nested)
    if len(counts) != 1 or nested_date:
        return (
            Retention(None, True)
            if nested_date or len(counts) > 1
            else Retention(None, False)
        )
    return Retention(next(iter(counts)), False)


def partition_coverage(
    sql: str, partitioned: dict[str, str]
) -> PartitionCoverage | None:
    """Bind partition predicates to each partitioned source in the statement.

    A predicate on one alias is not applied to other partitioned sources.
    ``_PARTITIONTIME`` in ORDER BY or the select list is not a filter. An
    unqualified pseudo-column with two matching sources is ambiguous.

    Args:
        sql: Scheduled-query text.
        partitioned: ``dataset.table`` to its catalog partition column.

    Returns:
        Coverage for tables the statement reads, or None when SQL does not parse.
    """
    root = _root(sql)
    if root is None or not partitioned:
        return (
            None
            if root is None
            else PartitionCoverage(frozenset(), frozenset(), frozenset())
        )
    scans: dict[str, set[str]] = defaultdict(set)
    try:
        scopes = tuple(traverse_scope(root))
    except ParseError:
        return None
    seen: set[str] = set()
    for scope in scopes:
        alias_map = _scope_tables(scope)
        tables = {key for key in alias_map.values() if key in partitioned}
        seen.update(tables)
        if not tables:
            continue
        status = {table: "unfiltered" for table in tables}
        for column in _filter_columns(scope):
            _bind_partition(column, alias_map, partitioned, tables, status)
        for table, label in status.items():
            scans[table].add(label)
    for table_node in root.find_all(exp.Table):
        key = _dataset_table(table_node)
        if key in partitioned and key not in seen:
            scans[key].add("unfiltered")
    filtered: set[str] = set()
    unfiltered: set[str] = set()
    ambiguous: set[str] = set()
    for table, labels in scans.items():
        if "unfiltered" in labels:
            unfiltered.add(table)
        elif "ambiguous" in labels:
            ambiguous.add(table)
        elif labels == {"filtered"}:
            filtered.add(table)
    return PartitionCoverage(
        frozenset(filtered), frozenset(unfiltered), frozenset(ambiguous)
    )


def _root(sql: str) -> exp.Expression | None:
    try:
        trees = [
            tree for tree in sqlglot.parse(sql, read="bigquery") if tree is not None
        ]
    except ParseError:
        return None
    if not trees:
        return None
    return _unwrap(trees[-1])


def _unwrap(node: exp.Expression) -> exp.Expression:
    current = node
    while isinstance(current, (exp.Subquery, exp.Paren)):
        current = current.this
    if isinstance(current, exp.Create) and current.expression is not None:
        return _unwrap(current.expression)
    return current


def _count_output(node: exp.Expression) -> int | None:
    if isinstance(node, exp.Union):
        left = _count_output(node.this)
        right = _count_output(node.expression)
        if left is None or right is None or left != right:
            return None
        return left
    if isinstance(node, exp.Select):
        if any(_is_star(expression) for expression in node.expressions):
            return None
        if not node.expressions:
            return None
        return len(node.expressions)
    return None


def _schema(
    node: exp.Expression, resolve: Callable[[str], NameStatus | None] | None
) -> NameStatus:
    if isinstance(node, exp.Union):
        left = _schema(node.this, resolve)
        right = _schema(node.expression, resolve)
        if left.closed and right.closed and len(left.present) == len(right.present):
            return NameStatus(left.present, frozenset(), True, True, left.present)
        return EMPTY_STATUS
    if not isinstance(node, exp.Select):
        return EMPTY_STATUS
    explicit: set[str] = set()
    stars: list[tuple[str | None, frozenset[str], bool]] = []
    unnamed = False
    for expression in node.expressions:
        star = _star_info(expression)
        if star is not None:
            stars.append(star)
            continue
        name = _output_name(expression)
        if name is None:
            unnamed = True
            continue
        explicit.add(name)
    excepted: set[str] = set()
    for _alias, names, _opaque in stars:
        excepted.update(names)
    absent = excepted - explicit
    present = frozenset(explicit)
    if not stars and not unnamed:
        return NameStatus(present, frozenset(), True, True, present)
    if len(stars) != 1 or stars[0][2] or unnamed:
        return NameStatus(present, frozenset(absent), False, False, present)
    source_key = _star_source(node, stars[0][0])
    source = resolve(source_key) if resolve is not None and source_key else None
    if source is None:
        return NameStatus(present, frozenset(absent), False, False, present)
    return _expand(present, frozenset(stars[0][1]), source)


def _expand(
    explicit: frozenset[str], except_names: frozenset[str], source: NameStatus
) -> NameStatus:
    absent = (except_names - explicit) | (source.absent - explicit)
    present = set(explicit) | (set(source.present) - set(except_names))
    if source.closed:
        closed = frozenset(present)
        return NameStatus(closed, frozenset(), True, True, closed)
    if source.covered:
        union = set(explicit) | (set(source.present_union) - set(except_names))
        return NameStatus(
            frozenset(present), frozenset(absent), False, True, frozenset(union)
        )
    return NameStatus(
        frozenset(present), frozenset(absent), False, False, frozenset(present)
    )


def _star_source(select: exp.Select, alias: str | None) -> str | None:
    tables = _select_tables(select)
    if alias:
        for name, key in tables:
            if name == alias:
                return key
        return None
    if select.args.get("joins") or len(tables) != 1:
        return None
    return tables[0][1]


def _select_tables(select: exp.Select) -> list[tuple[str, str]]:
    nodes: list[exp.Expression] = []
    from_ = select.args.get("from_")
    if from_ is not None:
        nodes.append(from_.this)
    for join in select.args.get("joins") or []:
        nodes.append(join.this)
    found = []
    for node in nodes:
        if isinstance(node, exp.Table):
            key = _dataset_table(node)
            if key:
                found.append((node.alias or node.name, key))
    return found


def _dataset_table(table: exp.Table) -> str | None:
    if not table.db or not table.name:
        return None
    return f"{table.db}.{table.name}"


def _is_star(expression: exp.Expression) -> bool:
    return _star_info(expression) is not None


def _star_info(
    expression: exp.Expression,
) -> tuple[str | None, frozenset[str], bool] | None:
    star: exp.Star | None = None
    alias: str | None = None
    if isinstance(expression, exp.Star):
        star = expression
    elif isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star):
        star = expression.this
        alias = expression.table or None
    if star is None:
        return None
    names = frozenset(
        column.name
        for column in star.args.get("except_") or []
        if isinstance(column, exp.Column)
    )
    opaque = bool(star.args.get("replace") or star.args.get("rename"))
    return alias, names, opaque


def _output_name(expression: exp.Expression) -> str | None:
    if isinstance(expression, exp.Alias) and expression.alias:
        return expression.alias
    if isinstance(expression, exp.Column) and not isinstance(expression.this, exp.Star):
        return expression.name or None
    return None


def _accumulate_columns(tree: exp.Expression, found: dict[str, set[str]]) -> None:
    for scope in traverse_scope(tree):
        alias_map = _scope_tables(scope)
        for column in scope.columns:
            name = column.name
            if not name or name.upper() in _PSEUDO or not column.table:
                continue
            key = alias_map.get(column.table)
            if key:
                found[key].add(name)
        expression = scope.expression
        if not isinstance(expression, exp.Select):
            continue
        for item in expression.expressions:
            star = _star_info(item)
            if star is None:
                continue
            alias, names, _opaque = star
            key = alias_map.get(alias) if alias else None
            if key is None and alias is None and len(set(alias_map.values())) == 1:
                if not expression.args.get("joins"):
                    key = next(iter(alias_map.values()))
            if key:
                found[key].update(name for name in names if name.upper() not in _PSEUDO)


def _scope_tables(scope: object) -> dict[str, str]:
    found: dict[str, str] = {}
    sources = getattr(scope, "sources", {})
    for alias, source in sources.items():
        if isinstance(source, exp.Table):
            key = _dataset_table(source)
            if not key:
                continue
            found[str(alias)] = key
            if source.alias:
                found[source.alias] = key
            found[source.name] = key
    return found


def _outer_wheres(node: exp.Expression) -> list[exp.Where]:
    if isinstance(node, exp.Select):
        where = node.args.get("where")
        return [where] if isinstance(where, exp.Where) else []
    if isinstance(node, exp.Union):
        return _outer_wheres(node.this) + _outer_wheres(node.expression)
    return []


def _branch_retentions(node: exp.Expression) -> list[Retention]:
    if isinstance(node, exp.Union):
        return _branch_retentions(node.this) + _branch_retentions(node.expression)
    if isinstance(node, exp.Select):
        where = node.args.get("where")
        if not isinstance(where, exp.Where):
            return [Retention(None, False)]
        return [_where_retention(where)]
    return [Retention(None, True)]


def _where_retention(where: exp.Where) -> Retention:
    if where.find(exp.Or):
        return Retention(None, True)
    days: list[int] = []
    for predicate in _conjuncts(where.this):
        kind = _predicate_days(predicate)
        if kind == "opaque":
            return Retention(None, True)
        if isinstance(kind, int):
            days.append(kind)
    if not days:
        return Retention(None, False)
    return Retention(min(days), False)


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _predicate_days(predicate: exp.Expression) -> int | Literal["opaque", "other"]:
    if not isinstance(predicate, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return "opaque" if _has_date_shift(predicate) else "other"
    left = _exact_lookback(predicate.this)
    right = _exact_lookback(predicate.expression)
    if left == "opaque" or right == "opaque":
        return "opaque"
    if isinstance(left, int) and isinstance(right, int):
        return "opaque"
    if isinstance(right, int) and isinstance(predicate, (exp.GT, exp.GTE)):
        return right
    if isinstance(left, int) and isinstance(predicate, (exp.LT, exp.LTE)):
        return left
    if isinstance(left, int) or isinstance(right, int):
        return "other"
    return "opaque" if _has_date_shift(predicate) else "other"


def _exact_lookback(node: exp.Expression) -> int | Literal["opaque", "no"]:
    current: exp.Expression | None = node
    while isinstance(current, exp.Paren):
        current = current.this
    if isinstance(current, _SUB) and isinstance(current.this, _CURRENT):
        count = _day_literal(current)
        if count is None or count < 0:
            return "opaque"
        return count
    if isinstance(current, _ADD) and isinstance(current.this, _CURRENT):
        count = _day_literal(current)
        if count is None or count >= 0:
            return "opaque"
        return abs(count)
    if _has_date_shift(current):
        return "opaque"
    return "no"


def _day_literal(node: exp.Expression) -> int | None:
    unit = node.args.get("unit")
    unit_name = unit.name.upper() if isinstance(unit, exp.Var) else ""
    if unit_name != "DAY":
        return None
    literal = node.args.get("expression")
    if not isinstance(literal, exp.Literal):
        return None
    raw = str(literal.this)
    sign = 1
    if raw.startswith("-"):
        sign = -1
        raw = raw[1:]
    if not raw.isdigit():
        return None
    return sign * int(raw)


def _has_date_shift(node: exp.Expression) -> bool:
    return (
        node.find(exp.DateSub) is not None
        or node.find(exp.TimestampSub) is not None
        or node.find(exp.DatetimeSub) is not None
        or node.find(exp.DateAdd) is not None
        or node.find(exp.TimestampAdd) is not None
        or node.find(exp.DatetimeAdd) is not None
        or node.find(exp.Interval) is not None
    )


def _filter_columns(scope: object) -> list[exp.Column]:
    expression = getattr(scope, "expression", None)
    if expression is None:
        return []
    nodes: list[exp.Expression] = []
    where = expression.args.get("where")
    if isinstance(where, exp.Expression):
        nodes.append(where)
    for join in expression.args.get("joins") or []:
        on_clause = join.args.get("on")
        if isinstance(on_clause, exp.Expression):
            nodes.append(on_clause)
    columns: list[exp.Column] = []
    for node in nodes:
        columns.extend(column for column in node.find_all(exp.Column))
    return columns


def _bind_partition(
    column: exp.Column,
    alias_map: dict[str, str],
    partitioned: dict[str, str],
    tables: set[str],
    status: dict[str, str],
) -> None:
    if column.table:
        key = alias_map.get(column.table)
        if key in tables and _partition_match(column.name, partitioned[key]):
            if status[key] != "ambiguous":
                status[key] = "filtered"
        return
    matching = [
        table for table in tables if _partition_match(column.name, partitioned[table])
    ]
    if len(matching) == 1 and status[matching[0]] == "unfiltered":
        status[matching[0]] = "filtered"
    elif len(matching) > 1:
        for table in matching:
            if status[table] != "filtered":
                status[table] = "ambiguous"


def _partition_match(column_name: str, partition_column: str) -> bool:
    if partition_column in _PSEUDO:
        return column_name.upper() in _PSEUDO
    return column_name == partition_column
