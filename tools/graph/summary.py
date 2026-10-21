"""Text summary of a dependency graph.

The summary lists counts and structural findings. It does not include SQL.
"""

from __future__ import annotations

from graph.analyze import (
    downstream_lineage,
    duplicate_writers,
    migration_scope,
    missing_producers,
    parse_failures,
    produced_tables,
    self_reads,
    shared_source_siblings,
    source_tables,
    strongly_connected,
    uncertain_reads,
)
from graph.models import Graph


def format_summary(graph: Graph) -> str:
    """Format node counts, structural findings, and the revenue_daily route.

    Args:
        graph: Graph from ``build_graph``.

    Returns:
        A stable multi-line summary. Lists are already sorted by the analyzers.
    """
    scope = migration_scope(graph)
    lineage = downstream_lineage(graph)
    lines = [
        *_counts(graph),
        *_pairs("duplicate_writer", duplicate_writers(graph)),
        _ids("self_read", self_reads(graph)),
        *_components(strongly_connected(graph)),
        _ids("missing_producer", missing_producers(graph)),
        _ids("parse_failure", tuple(node.id for node in parse_failures(graph))),
        _ids(
            "uncertain_read",
            tuple(f"{edge.source}->{edge.target}" for edge in uncertain_reads(graph)),
        ),
        _ids("source_table", source_tables(graph)),
        _ids("produced_table", produced_tables(graph)),
        _ids("revenue_downstream", lineage),
        _ids("revenue_scope", scope),
        _ids("revenue_sibling", shared_source_siblings(graph, scope)),
    ]
    return "\n".join(lines)


def _counts(graph: Graph) -> list[str]:
    kinds = ("table", "query", "consumer")
    relations = ("reads", "writes", "consumes")
    lines = [f"nodes: {len(graph.nodes)}", f"edges: {len(graph.edges)}"]
    for kind in kinds:
        count = sum(node.kind == kind for node in graph.nodes)
        lines.append(f"nodes.{kind}: {count}")
    for relation in relations:
        count = sum(
            edge.relation == relation and not edge.uncertain for edge in graph.edges
        )
        lines.append(f"edges.{relation}: {count}")
    uncertain = sum(edge.uncertain for edge in graph.edges)
    lines.append(f"edges.uncertain: {uncertain}")
    return lines


def _pairs(label: str, rows: tuple[tuple[str, tuple[str, ...]], ...]) -> list[str]:
    if not rows:
        return [f"{label}: none"]
    return [f"{label}: {table} {' '.join(queries)}" for table, queries in rows]


def _components(components: tuple[tuple[str, ...], ...]) -> list[str]:
    if not components:
        return ["component: none"]
    return [f"component: {' '.join(component)}" for component in components]


def _ids(label: str, node_ids: tuple[str, ...]) -> str:
    if not node_ids:
        return f"{label}: none"
    return f"{label}: " + " ".join(node_ids)
