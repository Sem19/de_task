"""Shared helpers for audit detectors.

Finding construction lives here so each detector module can stay focused on
one rule. Detectors do not modify SQL.
"""

from __future__ import annotations

from collections import defaultdict

from audit.models import AuditFinding, Category, Confidence
from estate.models import Inventory, ScheduledQuery
from graph.models import Graph

CATEGORY_ORDER = {"operational": 0, "correctness": 1, "maintainability": 2}


def make_finding(
    finding_id: str,
    category: Category,
    confidence: Confidence,
    priority: int,
    title: str,
    queries: tuple[ScheduledQuery, ...],
    evidence: str,
    impact: str,
    recommendation: str,
) -> AuditFinding:
    """Build one finding with config ids and SQL paths in config_id order.

    Args:
        finding_id: Stable id derived from the evidence, not from a counter.
        category: operational, correctness, or maintainability.
        confidence: How far the supplied files support the claim.
        priority: 1 is the highest.
        title: Short statement of the observation.
        queries: Scheduled queries the finding cites.
        evidence: Facts from the supplied files.
        impact: Consequence if the observation holds. Not an action already taken.
        recommendation: What an owner should confirm. Not an automatic change.

    Returns:
        The finding. Config ids and paths follow config_id order.
    """
    ordered = tuple(sorted(queries, key=lambda query: query.config_id))
    return AuditFinding(
        finding_id=finding_id,
        category=category,
        confidence=confidence,
        priority=priority,
        title=title,
        config_ids=tuple(query.config_id for query in ordered),
        sql_paths=tuple(sql_path(query) for query in ordered),
        evidence=evidence,
        impact=impact,
        recommendation=recommendation,
    )


def sort_key(finding: AuditFinding) -> tuple[int, int, str]:
    """Return the register order: category, priority, then finding id.

    Args:
        finding: One audit item.

    Returns:
        ``(category rank, priority, finding_id)``. Operational ranks before
        correctness, which ranks before maintainability.
    """
    return (CATEGORY_ORDER[finding.category], finding.priority, finding.finding_id)


def sql_path(query: ScheduledQuery) -> str:
    """Return the repository-relative SQL path for a paired query.

    Args:
        query: Paired scheduled query. Only the filename is used.

    Returns:
        ``task2_estate/scheduled_queries/<filename>``.
    """
    return f"task2_estate/scheduled_queries/{query.sql_path.name}"


def table_name(node_id: str) -> str:
    """Strip a ``table:`` graph id down to ``dataset.table``.

    Args:
        node_id: Graph node id. A missing prefix is left unchanged.

    Returns:
        The id without a leading ``table:``.
    """
    return node_id.removeprefix("table:")


def query_name(node_id: str) -> str:
    """Strip a ``query:`` graph id down to the config id.

    Args:
        node_id: Graph node id. A missing prefix is left unchanged.

    Returns:
        The id without a leading ``query:``.
    """
    return node_id.removeprefix("query:")


def writers_by_table(graph: Graph) -> dict[str, tuple[str, ...]]:
    """Map ``dataset.table`` to the config ids that write it.

    Uncertain write edges are omitted. Ids are sorted and unique.

    Args:
        graph: Dependency graph.

    Returns:
        Destination ``dataset.table`` to sorted writer config ids.
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.relation == "writes" and not edge.uncertain:
            grouped[table_name(edge.target)].append(query_name(edge.source))
    return {
        table: tuple(sorted(set(config_ids))) for table, config_ids in grouped.items()
    }


def certain_reads(graph: Graph) -> tuple[tuple[str, str], ...]:
    """Return ``(dataset.table, config_id)`` pairs for certain read edges.

    Args:
        graph: Dependency graph. Uncertain reads are omitted.

    Returns:
        Unique pairs, sorted by table then config id.
    """
    pairs = {
        (table_name(edge.source), query_name(edge.target))
        for edge in graph.edges
        if edge.relation == "reads" and not edge.uncertain
    }
    return tuple(sorted(pairs))


def consumers(inventory: Inventory, table: str) -> tuple[tuple[str, str], ...]:
    """Return ``(kind, id)`` usage rows recorded for ``dataset.table``.

    Args:
        inventory: Loaded estate. Rows with an unparsed table are skipped.
        table: Join key, ``dataset.table``.

    Returns:
        Unique consumer pairs, sorted.
    """
    found = {
        (row.consumer_kind, row.consumer_id)
        for row in inventory.usage
        if row.table is not None and row.table.dataset_table == table
    }
    return tuple(sorted(found))


def error_counts(inventory: Inventory, prefix: str) -> dict[str, int]:
    """Count history rows whose error message starts with ``prefix``.

    Args:
        inventory: Loaded estate. A missing error message counts as empty.
        prefix: Literal start of ``error_message``.

    Returns:
        Config id to the number of matching rows. Ids with zero matches
        are omitted.
    """
    counts: dict[str, int] = defaultdict(int)
    for row in inventory.history:
        message = row.raw.get("error_message") or ""
        if message.startswith(prefix):
            counts[row.config_id] += 1
    return dict(counts)


def queries_for(
    by_id: dict[str, ScheduledQuery], config_ids: tuple[str, ...] | set[str]
) -> tuple[ScheduledQuery, ...]:
    """Return paired queries for ids that exist, in config_id order.

    Args:
        by_id: Paired queries keyed by config id.
        config_ids: Ids to look up. Unknown ids are skipped. Duplicates collapse.

    Returns:
        The matching queries, sorted by config id.
    """
    return tuple(
        by_id[config_id] for config_id in sorted(set(config_ids)) if config_id in by_id
    )
