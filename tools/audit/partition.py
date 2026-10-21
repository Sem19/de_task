"""Partition predicates bound per source table.

A filter on one alias does not cover every partitioned table in the query.
Static binding that is ambiguous stays needs_verification. No EXPLAIN is run.
"""

from __future__ import annotations

from estate.models import Inventory, ScheduledQuery

from audit.common import make_finding, queries_for
from audit.models import AuditFinding
from audit.sql_ast import partition_coverage


def partition_findings(
    inventory: Inventory,
    by_id: dict[str, ScheduledQuery],
) -> list[AuditFinding]:
    """Report partitioned sources whose predicate is missing or unbound.

    Args:
        inventory: Catalog partition columns select the tables.
        by_id: Paired queries keyed by config id.

    Returns:
        One grouped finding, or none when the catalog lists no partition column.
    """
    partitioned = {
        record.table.dataset_table: record.raw.get("partition_column", "")
        for record in inventory.catalog
        if record.table is not None and record.raw.get("partition_column", "")
    }
    if not partitioned:
        return []
    missing: list[tuple[str, str]] = []
    ambiguous: list[tuple[str, str]] = []
    skipped: list[str] = []
    for query in inventory.queries:
        coverage = partition_coverage(query.sql_text, partitioned)
        if coverage is None:
            skipped.append(query.config_id)
            continue
        for table in sorted(coverage.unfiltered):
            missing.append((query.config_id, table))
        for table in sorted(coverage.ambiguous):
            ambiguous.append((query.config_id, table))
    if not missing and not ambiguous:
        return []
    config_ids = {config_id for config_id, _table in (*missing, *ambiguous)}
    described = ", ".join(
        f"{name}={column}" for name, column in sorted(partitioned.items())
    )
    parts = [f"Catalog partition columns: {described}."]
    if missing:
        listed = ", ".join(f"{config_id} {table}" for config_id, table in missing)
        parts.append(f"No predicate bound to: {listed}.")
    if ambiguous:
        listed = ", ".join(f"{config_id} {table}" for config_id, table in ambiguous)
        parts.append(
            "Unresolved binding, because an unqualified partition column matches "
            f"more than one source: {listed}."
        )
    if skipped:
        parts.append(
            "SQL that did not parse was not classified: "
            + ", ".join(sorted(skipped))
            + "."
        )
        config_ids.update(skipped)
    parts.append(
        "A predicate on one source is not applied to another. "
        "_PARTITIONTIME in ORDER BY or the select list is not a filter. "
        "No EXPLAIN was run, so pruning is not measured."
    )
    return [
        make_finding(
            "OP-PARTITION-FILTER",
            "operational",
            "needs_verification",
            3,
            "Partition predicates are missing or not bound to each partitioned source",
            queries_for(by_id, config_ids),
            " ".join(parts),
            "A filter on a normal timestamp, or on a different table's pseudo-column, does not prune this source.",
            "Verify partition pruning in BigQuery before rewriting filters. Do not add a predicate only to change the scanned bytes.",
        )
    ]
