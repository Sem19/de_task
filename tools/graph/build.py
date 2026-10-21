"""Build the dependency graph from a loaded inventory and parsed SQL.

Destinations come from transfer configs. SQL write targets are added when the
statement itself writes a table. Dashboard and ad-hoc usage rows become
consumer edges. Scheduled-query usage rows are not edges: the SQL parse is
the source of those reads. The inventory and the SQL text are not modified.
"""

from __future__ import annotations

from estate.identifiers import TableId
from estate.models import Inventory, ScheduledQuery, UsageRecord
from estate.sources import SCHEDULED_QUERY
from graph.models import Edge, Graph, Node, table_id
from graph.models import consumer_id as consumer_node_id
from graph.models import query_id as query_node_id
from sql.models import ParseIssue, SqlParse, UnresolvedRef
from sql.parse import parse_inventory


class _Table:
    __slots__ = ("dataset", "in_catalog", "produced", "projects", "table")

    def __init__(self, table: TableId, *, in_catalog: bool) -> None:
        self.dataset = table.dataset
        self.table = table.table
        self.projects: set[str] = set()
        self.produced = False
        self.in_catalog = in_catalog
        if table.project:
            self.projects.add(table.project)


def build_graph(
    inventory: Inventory,
    parses: tuple[SqlParse, ...] | None = None,
) -> Graph:
    """Connect source tables, queries, destinations, and external consumers.

    Args:
        inventory: Result of ``load_inventory``.
        parses: One result per paired query. Parsed from ``inventory`` when
            omitted. Matched by config_id, not by position.

    Returns:
        Nodes and edges sorted by id. Uncertain reads stay on the graph and
        are marked.

    Raises:
        KeyError: A parse result has no paired query, or a query has no parse.
    """
    by_query = _parses_by_config(inventory, parses)
    catalog = {
        record.table.dataset_table
        for record in inventory.catalog
        if record.table is not None
    }
    tables: dict[str, _Table] = {}
    nodes: list[Node] = []
    edges: dict[tuple[str, str, str], Edge] = {}

    for query in inventory.queries:
        parsed = by_query[query.config_id]
        nodes.append(_query_node(query, parsed))
        _add_writes(query, parsed, tables, catalog, edges)
        _add_reads(query, parsed, tables, catalog, edges)

    for row in inventory.usage:
        _add_consumer(row, tables, catalog, nodes, edges)

    table_nodes = [_freeze_table(item) for item in tables.values()]
    ordered = tuple(sorted((*table_nodes, *nodes), key=lambda item: item.id))
    ordered_edges = tuple(sorted(edges.values(), key=_edge_key))
    return Graph(ordered, ordered_edges)


def _parses_by_config(
    inventory: Inventory,
    parses: tuple[SqlParse, ...] | None,
) -> dict[str, SqlParse]:
    parsed = parses if parses is not None else parse_inventory(inventory)
    by_query = {item.config_id: item for item in parsed if item.config_id}
    missing = [
        query.config_id
        for query in inventory.queries
        if query.config_id not in by_query
    ]
    if missing:
        raise KeyError(f"no SQL parse for {', '.join(missing)}")
    return by_query


def _query_node(query: ScheduledQuery, parsed: SqlParse) -> Node:
    return Node(
        id=query_node_id(query.config_id),
        kind="query",
        table=None,
        projects=(),
        produced=False,
        in_catalog=False,
        config_id=query.config_id,
        parsed=parsed.parsed,
        parse_errors=tuple(_issue_text(issue) for issue in parsed.errors),
        unresolved=tuple(sorted(_unresolved_text(item) for item in parsed.unresolved)),
        enabled=query.enabled,
        write_disposition=query.write_disposition,
        consumer_kind=None,
        consumer_id=None,
    )


def _add_writes(
    query: ScheduledQuery,
    parsed: SqlParse,
    tables: dict[str, _Table],
    catalog: set[str],
    edges: dict[tuple[str, str, str], Edge],
) -> None:
    destinations: list[TableId] = []
    if query.destination is not None:
        destinations.append(query.destination)
    destinations.extend(ref.table for ref in parsed.writes)
    source = query_node_id(query.config_id)
    for destination in destinations:
        _observe(tables, destination, catalog, produced=True)
        _put_edge(
            edges,
            Edge(source, table_id(destination.dataset_table), "writes", False),
        )


def _add_reads(
    query: ScheduledQuery,
    parsed: SqlParse,
    tables: dict[str, _Table],
    catalog: set[str],
    edges: dict[tuple[str, str, str], Edge],
) -> None:
    target = query_node_id(query.config_id)
    for ref in parsed.references:
        if ref.role == "write" and not ref.uncertain:
            continue
        _observe(tables, ref.table, catalog, produced=False)
        _put_edge(
            edges,
            Edge(table_id(ref.table.dataset_table), target, "reads", ref.uncertain),
        )


def _add_consumer(
    row: UsageRecord,
    tables: dict[str, _Table],
    catalog: set[str],
    nodes: list[Node],
    edges: dict[tuple[str, str, str], Edge],
) -> None:
    if row.consumer_kind == SCHEDULED_QUERY or row.table is None:
        return
    node_id = consumer_node_id(row.consumer_kind, row.consumer_id)
    if not any(item.id == node_id for item in nodes):
        nodes.append(
            Node(
                id=node_id,
                kind="consumer",
                table=None,
                projects=(),
                produced=False,
                in_catalog=False,
                config_id=None,
                parsed=None,
                parse_errors=(),
                unresolved=(),
                enabled=None,
                write_disposition=None,
                consumer_kind=row.consumer_kind,
                consumer_id=row.consumer_id,
            )
        )
    _observe(tables, row.table, catalog, produced=False)
    _put_edge(
        edges,
        Edge(
            table_id(row.table.dataset_table),
            node_id,
            "consumes",
            False,
            queries_30d=row.raw.get("queries_30d"),
        ),
    )


def _observe(
    tables: dict[str, _Table],
    table: TableId,
    catalog: set[str],
    *,
    produced: bool,
) -> None:
    key = table.dataset_table
    current = tables.get(key)
    if current is None:
        current = _Table(table, in_catalog=key in catalog)
        tables[key] = current
    elif table.project:
        current.projects.add(table.project)
    if produced:
        current.produced = True


def _freeze_table(item: _Table) -> Node:
    projects = tuple(sorted(item.projects))
    project = projects[0] if len(projects) == 1 else None
    identity = TableId(project, item.dataset, item.table)
    return Node(
        id=table_id(identity.dataset_table),
        kind="table",
        table=identity,
        projects=projects,
        produced=item.produced,
        in_catalog=item.in_catalog,
        config_id=None,
        parsed=None,
        parse_errors=(),
        unresolved=(),
        enabled=None,
        write_disposition=None,
        consumer_kind=None,
        consumer_id=None,
    )


def _put_edge(edges: dict[tuple[str, str, str], Edge], edge: Edge) -> None:
    key = (edge.source, edge.target, edge.relation)
    current = edges.get(key)
    if current is None or (current.uncertain and not edge.uncertain):
        edges[key] = edge


def _issue_text(issue: ParseIssue) -> str:
    if issue.line is None:
        return issue.message
    column = issue.col if issue.col is not None else 0
    return f"{issue.line}:{column}: {issue.message}"


def _unresolved_text(item: UnresolvedRef) -> str:
    return f"{item.reason}:{item.raw}"


def _edge_key(edge: Edge) -> tuple[str, bool, str, str]:
    return (edge.relation, edge.uncertain, edge.source, edge.target)
