"""Short WRITE_TRUNCATE windows compared with a reader's outer date filter.

Only an outer WHERE lower bound against the current date is a lookback.
Other INTERVAL expressions are ignored or left uncertain.
"""

from __future__ import annotations

from collections import defaultdict

from estate.models import ScheduledQuery
from graph.models import Graph

from audit.common import make_finding, writers_by_table
from audit.models import AuditFinding
from audit.sql_ast import retention_days


def lookback_findings(
    graph: Graph,
    by_id: dict[str, ScheduledQuery],
) -> list[AuditFinding]:
    """Flag a truncating writer whose outer lookback is shorter than a reader's.

    Args:
        graph: Certain read edges select the readers.
        by_id: Paired queries keyed by config id.

    Returns:
        One finding per writer. Uncertain SQL is not given a day count.
    """
    readers: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.relation == "reads" and not edge.uncertain:
            readers[edge.source].append(edge.target.removeprefix("query:"))
    findings = []
    for table_node, producer_ids in writers_by_table(graph).items():
        for producer in producer_ids:
            query = by_id[producer]
            if query.write_disposition != "WRITE_TRUNCATE":
                continue
            writer = retention_days(query.sql_text)
            if writer.uncertain or writer.days is None or writer.days > 14:
                continue
            longer = _longer_readers(
                by_id, readers.get(f"table:{table_node}", ()), writer.days
            )
            if not longer:
                continue
            involved = (producer, *(item[0] for item in longer))
            table = table_node
            detail = ", ".join(
                f"{config_id} outer WHERE {days} DAY" for config_id, days in longer
            )
            findings.append(
                make_finding(
                    f"CR-SHORT-LOOKBACK-{producer}",
                    "correctness",
                    "indicated",
                    2,
                    f"{producer} truncates {table} on a shorter outer date bound than a reader filters",
                    tuple(by_id[config_id] for config_id in sorted(set(involved))),
                    (
                        f"{producer} is WRITE_TRUNCATE. Its outer WHERE lower bound is "
                        f"{writer.days} DAY. Readers: {detail}. "
                        "Intervals in the select list or only inside a CTE are not used. "
                        "The bound does not prove which business date the table retains."
                    ),
                    "A reader that asks for more history than this predicate writes will see only the writer's window after a truncate.",
                    "Do not lengthen the predicate in this audit. Confirm with the owner whether the short window is intentional.",
                )
            )
    return findings


def _longer_readers(
    by_id: dict[str, ScheduledQuery],
    readers: tuple[str, ...] | list[str],
    writer_days: int,
) -> list[tuple[str, int]]:
    """Return readers whose outer lookback is at least 30 days and longer.

    Uncertain SQL and readers with no day count are skipped.

    Args:
        by_id: Paired queries keyed by config id.
        readers: Config ids that read the writer's table.
        writer_days: The writer's outer lookback, in days.

    Returns:
        ``(config id, reader days)`` for readers that ask for more history.
        Sorted by config id.
    """
    longer = []
    for reader in sorted(set(readers)):
        result = retention_days(by_id[reader].sql_text)
        if result.uncertain or result.days is None:
            continue
        if result.days >= 30 and result.days > writer_days:
            longer.append((reader, result.days))
    return longer
