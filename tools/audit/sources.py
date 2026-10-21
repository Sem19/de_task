"""Catalog gaps and Not found failures, reported as separate facts.

A table missing from table_catalog.csv is not treated as absent from
production. A Not found history row is a recorded failure, not a current
existence check.
"""

from __future__ import annotations

from collections import defaultdict

from estate.models import Inventory, ScheduledQuery
from graph.analyze import missing_producers
from graph.models import Graph

from audit.common import consumers, make_finding, queries_for, table_name
from audit.models import AuditFinding


def source_findings(
    inventory: Inventory,
    graph: Graph,
    by_id: dict[str, ScheduledQuery],
) -> list[AuditFinding]:
    """Split catalog omissions from observed Not found failures.

    Args:
        inventory: Catalog, history, and usage.
        graph: Certain reads define missing producers.
        by_id: Paired queries keyed by config id.

    Returns:
        One catalog finding per missing producer, plus a Not found finding
        when history records that error for a reader of the same table.
    """
    readers: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.relation == "reads" and not edge.uncertain:
            readers[table_name(edge.source)].append(edge.target.removeprefix("query:"))
    failures = _not_found(inventory)
    findings = []
    for table_node in missing_producers(graph):
        table = table_name(table_node)
        config_ids = tuple(sorted(set(readers.get(table, ()))))
        queries = queries_for(by_id, config_ids)
        recorded = tuple((kind, name) for kind, name in consumers(inventory, table))
        consumer_text = ", ".join(f"{kind} {name}" for kind, name in recorded) or "none"
        findings.append(
            make_finding(
                "OP-CATALOG-GAP-" + table.replace(".", "-"),
                "operational",
                "needs_verification",
                2,
                f"{table} is read by scheduled SQL and is absent from the supplied catalog",
                queries,
                (
                    f"Certain SQL reads name {table}. It is absent from table_catalog.csv "
                    f"and from scheduled-query destinations. Consumers recorded on {table}: "
                    f"{consumer_text}. The catalog extract does not prove the table is "
                    "absent from production."
                ),
                "A migration that assumes the table does not exist can skip a source that BigQuery still has.",
                "Confirm the table in BigQuery. Do not delete a destination because the catalog omits this source.",
            )
        )
        messages = [
            (config_id, failures[config_id])
            for config_id in config_ids
            if config_id in failures
        ]
        if not messages:
            continue
        detail = "; ".join(
            f"{config_id} {count} row(s), first error: {message}"
            for config_id, (count, message) in messages
        )
        findings.append(
            make_finding(
                "OP-NOT-FOUND-" + table.replace(".", "-"),
                "operational",
                "confirmed",
                1,
                f"Run history records Not found while reading {table}",
                queries,
                (
                    f"{detail}. The error string is a recorded failure. "
                    "It does not prove the table is still missing."
                ),
                "Those runs did not write a successful output. A dashboard can still point at an older destination table.",
                "Do not disable the job only because these runs failed, and do not delete its destination. Confirm whether the source exists now.",
            )
        )
    return findings


def _not_found(inventory: Inventory) -> dict[str, tuple[int, str]]:
    """Count history rows whose error starts with ``Not found``.

    Args:
        inventory: Loaded estate.

    Returns:
        Config id to ``(row count, first matching error message)``, with
        keys in config-id order.
    """
    counts: dict[str, int] = defaultdict(int)
    first: dict[str, str] = {}
    for row in inventory.history:
        message = row.raw.get("error_message") or ""
        if not message.startswith("Not found"):
            continue
        counts[row.config_id] += 1
        first.setdefault(row.config_id, message)
    return {
        config_id: (counts[config_id], first[config_id]) for config_id in sorted(counts)
    }
