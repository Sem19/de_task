"""Upstream and downstream walks over certain lineage edges.

An edge points from a dependency toward its consumer. Uncertain reads are
omitted unless the caller asks for them. Results are sorted node ids and do
not include the start node.
"""

from __future__ import annotations

import networkx as nx

from graph.models import Graph


def upstream(
    graph: Graph,
    node_id: str,
    *,
    include_uncertain: bool = False,
) -> tuple[str, ...]:
    """Return ancestors of ``node_id``.

    Walking upstream from a table follows its writers and the tables those
    writers read. It does not follow other readers of a shared source.

    Args:
        graph: Dependency graph.
        node_id: Node to start from. Not included in the result.
        include_uncertain: Follow recovered reads as well as certain ones.

    Returns:
        Ancestor ids, sorted.

    Raises:
        KeyError: ``node_id`` is not in the graph.
    """
    return _walk(graph, node_id, forward=False, include_uncertain=include_uncertain)


def downstream(
    graph: Graph,
    node_id: str,
    *,
    include_uncertain: bool = False,
) -> tuple[str, ...]:
    """Return descendants of ``node_id``.

    Args:
        graph: Dependency graph.
        node_id: Node to start from. Not included in the result.
        include_uncertain: Follow recovered reads as well as certain ones.

    Returns:
        Descendant ids, sorted.

    Raises:
        KeyError: ``node_id`` is not in the graph.
    """
    return _walk(graph, node_id, forward=True, include_uncertain=include_uncertain)


def dependency_digraph(graph: Graph, *, include_uncertain: bool = False) -> nx.DiGraph:
    """Return a directed graph of lineage edges.

    Args:
        graph: Dependency graph.
        include_uncertain: Keep recovered reads. By default they are omitted.

    Returns:
        A networkx graph with the same node ids. Used by component analysis.
    """
    digraph: nx.DiGraph = nx.DiGraph()
    digraph.add_nodes_from(node.id for node in graph.nodes)
    for edge in graph.edges:
        if edge.uncertain and not include_uncertain:
            continue
        digraph.add_edge(edge.source, edge.target)
    return digraph


def _walk(
    graph: Graph,
    node_id: str,
    *,
    forward: bool,
    include_uncertain: bool,
) -> tuple[str, ...]:
    """Walk ancestors or descendants and return them sorted.

    Args:
        graph: Dependency graph.
        node_id: Start node. It is not included.
        forward: ``True`` walks descendants. ``False`` walks ancestors.
        include_uncertain: Follow recovered reads.

    Returns:
        Sorted node ids, excluding ``node_id``.

    Raises:
        KeyError: ``node_id`` is not in the graph.
    """
    digraph = dependency_digraph(graph, include_uncertain=include_uncertain)
    if node_id not in digraph:
        raise KeyError(f"unknown graph node: {node_id}")
    found = (
        nx.descendants(digraph, node_id) if forward else nx.ancestors(digraph, node_id)
    )
    return tuple(sorted(found))
