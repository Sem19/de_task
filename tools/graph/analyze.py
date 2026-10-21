"""Lineage checks and the revenue_daily route.

The route is discovered from the table name ``revenue_daily``. Query ids and
edges are not hardcoded. A sibling query that only shares an upstream source
is outside the route, because the walk never leaves the ancestors of the
target or the descendants of the target.
"""

from __future__ import annotations

from collections import defaultdict

import networkx as nx

from graph.models import Edge, Graph, Node
from graph.traverse import dependency_digraph, downstream, upstream

REVENUE_DAILY_TABLE = "revenue_daily"


def duplicate_writers(graph: Graph) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return tables written by more than one scheduled query.

    Each row is ``(table node id, query node ids)``. Competing writers of the
    same destination are all listed. Ids are sorted.
    """
    writers: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.relation == "writes" and not edge.uncertain:
            writers[edge.target].append(edge.source)
    rows = [
        (table, tuple(sorted(queries)))
        for table, queries in writers.items()
        if len(queries) > 1
    ]
    return tuple(sorted(rows))


def self_reads(graph: Graph) -> tuple[str, ...]:
    """Return query node ids that read one of their own destinations."""
    written: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        if edge.relation == "writes" and not edge.uncertain:
            written[edge.source].add(edge.target)
    found = {
        edge.target
        for edge in graph.edges
        if edge.relation == "reads"
        and not edge.uncertain
        and edge.source in written.get(edge.target, ())
    }
    return tuple(sorted(found))


def missing_producers(graph: Graph) -> tuple[str, ...]:
    """Return tables read by SQL, absent from the catalog, and written by nobody.

    Catalogued tables with no scheduled-query writer are external sources, not
    missing producers. Uncertain names are omitted here and reported separately.
    """
    nodes = _nodes(graph)
    read = {
        edge.source
        for edge in graph.edges
        if edge.relation == "reads" and not edge.uncertain
    }
    missing = [
        node_id
        for node_id in read
        if not nodes[node_id].produced and not nodes[node_id].in_catalog
    ]
    return tuple(sorted(missing))


def uncertain_reads(graph: Graph) -> tuple[Edge, ...]:
    """Return recovered read edges. Lineage walks skip these by default."""
    return tuple(
        edge for edge in graph.edges if edge.relation == "reads" and edge.uncertain
    )


def parse_failures(graph: Graph) -> tuple[Node, ...]:
    """Return query nodes whose SQL did not parse."""
    return tuple(
        node for node in graph.nodes if node.kind == "query" and node.parsed is False
    )


def strongly_connected(graph: Graph) -> tuple[tuple[str, ...], ...]:
    """Return strongly connected components that contain more than one node.

    Certain edges only. A self-read is a component of the query and its
    destination. A cycle between queries contains at least two query nodes.
    Each component is sorted, and components are sorted by their ids.
    """
    digraph = dependency_digraph(graph)
    components = [
        tuple(sorted(component))
        for component in nx.strongly_connected_components(digraph)
        if len(component) > 1
    ]
    return tuple(sorted(components))


def source_tables(graph: Graph) -> tuple[str, ...]:
    """Return tables that are read and are not written by a scheduled query."""
    read = {edge.source for edge in graph.edges if edge.relation == "reads"}
    return tuple(
        sorted(
            node.id
            for node in graph.nodes
            if node.id in read and node.kind == "table" and not node.produced
        )
    )


def produced_tables(graph: Graph) -> tuple[str, ...]:
    """Return tables written by at least one scheduled query."""
    return tuple(
        sorted(
            node.id for node in graph.nodes if node.kind == "table" and node.produced
        )
    )


def tables_named(graph: Graph, table_name: str) -> tuple[str, ...]:
    """Return table node ids whose table name matches exactly.

    ``revenue_daily_eur`` does not match ``revenue_daily``.
    """
    return tuple(
        sorted(
            node.id
            for node in graph.nodes
            if node.kind == "table"
            and node.table is not None
            and node.table.table == table_name
        )
    )


def downstream_lineage(
    graph: Graph,
    table_name: str = REVENUE_DAILY_TABLE,
) -> tuple[str, ...]:
    """Return the target tables and every certain descendant.

    Descendants include later scheduled queries, the tables they write, and
    dashboard or ad-hoc consumers. Upstream producers are not included.
    """
    return _closure(graph, table_name, direction="downstream")


def migration_scope(
    graph: Graph,
    table_name: str = REVENUE_DAILY_TABLE,
) -> tuple[str, ...]:
    """Return the scheduled-query route that produces and continues past the target.

    The route is the ancestors of each exact ``table_name`` table, the table
    itself, and scheduled-query descendants. Ancestors include every writer of
    the target. They do not include other queries that merely read the same
    source. Dashboard and ad-hoc consumers stay on the downstream lineage and
    are not migration nodes.
    """
    selected: set[str] = set()
    nodes = _nodes(graph)
    for target in tables_named(graph, table_name):
        selected.add(target)
        selected.update(upstream(graph, target))
        for node_id in downstream(graph, target):
            if nodes[node_id].kind != "consumer":
                selected.add(node_id)
    return tuple(sorted(selected))


def shared_source_siblings(graph: Graph, scope: tuple[str, ...]) -> tuple[str, ...]:
    """Return queries that read a scope table but are outside the scope.

    These share upstream data with the route and are not part of it.
    """
    chosen = set(scope)
    readers: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.relation == "reads" and not edge.uncertain:
            readers[edge.source].append(edge.target)
    siblings = {
        reader
        for node_id in scope
        for reader in readers.get(node_id, ())
        if reader not in chosen
    }
    return tuple(sorted(siblings))


def _closure(graph: Graph, table_name: str, *, direction: str) -> tuple[str, ...]:
    selected: set[str] = set()
    walk = downstream if direction == "downstream" else upstream
    for target in tables_named(graph, table_name):
        selected.add(target)
        selected.update(walk(graph, target))
    return tuple(sorted(selected))


def _nodes(graph: Graph) -> dict[str, Node]:
    return {node.id: node for node in graph.nodes}
