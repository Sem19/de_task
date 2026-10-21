"""Queries with no recorded consumer.

An uncertain read of the destination excludes the query. A missing usage row
is not enough to recommend retirement: the extract can omit a reader.
"""

from __future__ import annotations

from estate.models import Inventory, ScheduledQuery
from graph.models import Graph

from audit.common import consumers, make_finding, table_name
from audit.models import AuditFinding


def retirement_findings(
    inventory: Inventory,
    graph: Graph,
    by_id: dict[str, ScheduledQuery],
) -> list[AuditFinding]:
    """List enabled and already-disabled queries that nothing in the extract reads.

    Args:
        inventory: Usage rows and history.
        graph: Certain and uncertain read edges both count as readers.
        by_id: Unused. Destinations come from the paired queries on the inventory.

    Returns:
        An enabled-query finding at needs_verification, and a confirmed finding
        for queries that are already disabled.
    """
    del by_id
    readers = {
        table_name(edge.source) for edge in graph.edges if edge.relation == "reads"
    }
    idle = []
    disabled = []
    for query in inventory.queries:
        if query.destination is None:
            continue
        table = query.destination.dataset_table
        if table in readers or consumers(inventory, table):
            continue
        if query.enabled is False:
            disabled.append(query)
        elif query.enabled is True:
            idle.append(query)
    findings = []
    if idle:
        ordered = tuple(sorted(idle, key=lambda query: query.config_id))
        listed = ", ".join(
            f"{query.config_id}->{query.destination.dataset_table}"
            for query in ordered
            if query.destination is not None
        )
        findings.append(
            make_finding(
                "MN-RETIRE-NO-RECORDED-CONSUMER",
                "maintainability",
                "needs_verification",
                3,
                "Enabled queries have no consumer in the supplied graph or usage extract",
                ordered,
                (
                    "Each destination has no certain or uncertain graph read and no "
                    "scheduled_query, dashboard, or adhoc row in downstream_usage.csv. "
                    f"{listed}. The catalog still lists those tables. "
                    "A destination named only by an uncertain reference is excluded."
                ),
                "The usage extract can miss a reader. Disabling the schedule would not delete the table.",
                "Do not disable these queries and do not delete the tables. An owner has to confirm that refresh can stop.",
            )
        )
    if disabled:
        ordered = tuple(disabled)
        history_count = sum(
            row.config_id in {query.config_id for query in ordered}
            for row in inventory.history
        )
        detail = ", ".join(
            f"{query.config_id} state {query.state} disabled {query.disabled} "
            f"destination {query.destination.dataset_table if query.destination else 'unknown'}"
            for query in ordered
        )
        findings.append(
            make_finding(
                "OP-ALREADY-DISABLED",
                "operational",
                "confirmed",
                4,
                "A scheduled query is already disabled and has no recorded consumer",
                ordered,
                (
                    f"{detail}. History rows for these config ids: {history_count}. "
                    "No certain or uncertain read of the destination is in the graph."
                ),
                "The table remains in the catalog. Usage coverage is only the supplied file.",
                "Leave it disabled. Do not drop the table from this audit.",
            )
        )
    return findings
