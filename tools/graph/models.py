"""Nodes and edges of the scheduled-query dependency graph.

Edges point downstream: a source table is read by a query, the query writes
its transfer-config destination, and a documented consumer reads a table.
Uncertain edges are recovered names, not established reads. Node ids use
``dataset.table`` so a project-qualified reference matches one that has no
project. Observed projects stay on the table node.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from estate.identifiers import TableId

Kind = Literal["table", "query", "consumer"]
Relation = Literal["reads", "writes", "consumes"]


def table_id(dataset_table: str) -> str:
    """Node id for a ``dataset.table`` identity."""
    return f"table:{dataset_table}"


def query_id(config_id: str) -> str:
    """Node id for a scheduled query."""
    return f"query:{config_id}"


def consumer_id(kind: str, consumer: str) -> str:
    """Node id for a dashboard or ad-hoc consumer."""
    return f"consumer:{kind}:{consumer}"


class Node(NamedTuple):
    """One table, scheduled query, or external consumer.

    Table fields are empty on queries and consumers. ``produced`` is true when
    a scheduled query writes the table. ``in_catalog`` is true when the estate
    catalog lists it. ``projects`` holds every project seen for that
    ``dataset.table``, sorted. ``parsed`` is None for nodes that have no SQL.
    """

    id: str
    kind: Kind
    table: TableId | None
    projects: tuple[str, ...]
    produced: bool
    in_catalog: bool
    config_id: str | None
    parsed: bool | None
    parse_errors: tuple[str, ...]
    unresolved: tuple[str, ...]
    enabled: bool | None
    write_disposition: str | None
    consumer_kind: str | None
    consumer_id: str | None


class Edge(NamedTuple):
    """A downstream link.

    ``uncertain`` is true when the table name was recovered from SQL that did
    not parse. Those edges are stored and are not used as lineage.
    ``queries_30d`` is set only for a documented consumer.
    """

    source: str
    target: str
    relation: Relation
    uncertain: bool
    queries_30d: str | None = None


class Graph(NamedTuple):
    """Deterministic dependency graph. Nodes and edges are sorted by id."""

    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    def node(self, node_id: str) -> Node:
        """Return one node.

        Raises:
            KeyError: ``node_id`` is not in the graph.
        """
        for item in self.nodes:
            if item.id == node_id:
                return item
        raise KeyError(f"unknown graph node: {node_id}")
