"""Observed overlap between a producer execution and the reader it feeds.

Each reader execution is paired with the latest successful producer
schedule_time at or before that reader's schedule_time. Runs are not grouped
by calendar day. Hourly jobs are not compared with a later run on the same
date. Overlap is a clock fact, not proof that the reader scanned stale rows.
"""

from __future__ import annotations

import bisect
from datetime import datetime

from estate.models import Inventory, ScheduledQuery
from graph.analyze import strongly_connected
from graph.models import Graph

from audit.common import make_finding, table_name
from audit.models import AuditFinding


def schedule_findings(
    inventory: Inventory,
    graph: Graph,
    by_id: dict[str, ScheduledQuery],
) -> list[AuditFinding]:
    """Report matched executions whose producer was still running.

    Args:
        inventory: Run history. Timestamps without an offset are skipped.
        graph: Certain read edges choose the producer and reader pairs.
        by_id: Paired queries, used for schedule text and timezone.

    Returns:
        One finding per producer/reader pair that overlaps often enough.
    """
    slots = _slots(inventory)
    writers = _writers(graph)
    findings = []
    seen: set[tuple[str, str]] = set()
    for edge in graph.edges:
        if edge.relation != "reads" or edge.uncertain:
            continue
        reader = edge.target.removeprefix("query:")
        for producer in writers.get(edge.source, ()):
            if producer == reader or (producer, reader) in seen:
                continue
            if _in_cycle(graph, producer, reader):
                continue
            seen.add((producer, reader))
            bad, shared = _paired_overlap(slots, producer, reader)
            if shared < 14 or bad / shared < 0.3:
                continue
            hourly = _hourly(by_id[producer].schedule) or _hourly(
                by_id[reader].schedule
            )
            priority = 1 if bad == shared and not hourly else 2
            table = table_name(edge.source)
            findings.append(
                make_finding(
                    f"OP-SCHEDULE-OVERLAP-{producer}-{reader}",
                    "operational",
                    "confirmed",
                    priority,
                    f"Matched executions overlap: {producer} ends after {reader} starts",
                    tuple(by_id[config_id] for config_id in sorted((producer, reader))),
                    _evidence(by_id, producer, reader, table, bad, shared, hourly),
                    "The reader may have started before that producer execution finished. This does not prove the reader scanned a stale snapshot.",
                    "Record the overlap. Do not change either schedule until the owner accepts or rejects the lag.",
                )
            )
    return findings


def _evidence(
    by_id: dict[str, ScheduledQuery],
    producer: str,
    reader: str,
    table: str,
    bad: int,
    shared: int,
    hourly: bool,
) -> str:
    left = by_id[producer]
    right = by_id[reader]
    cadence = (
        " At least one schedule is hourly, so a later run on the same calendar day is not paired with an earlier reader."
        if hourly
        else ""
    )
    return (
        f"Paired {bad} of {shared} reader executions with the latest successful "
        f"{producer} schedule_time at or before the reader schedule_time. "
        f"{reader} reads {table}, which {producer} writes. "
        f"Producer schedule {left.schedule} timezone {left.timezone}. "
        f"Reader schedule {right.schedule} timezone {right.timezone}. "
        "History timestamps are compared as offset-aware instants. "
        "The config timezone is not used to rewrite those offsets. "
        "Timestamps without an offset are ignored. "
        "This is observed execution overlap, not proof of stale data. "
        "A run whose end_time is earlier than its start_time is omitted." + cadence
    )


def _slots(
    inventory: Inventory,
) -> dict[str, list[tuple[datetime, datetime, datetime]]]:
    grouped: dict[str, dict[datetime, tuple[datetime, datetime]]] = {}
    for row in inventory.history:
        if row.raw.get("state") != "SUCCEEDED":
            continue
        scheduled = _aware(row.raw.get("schedule_time", ""))
        started = _aware(row.raw.get("start_time", ""))
        ended = _aware(row.raw.get("end_time", ""))
        if scheduled is None or started is None or ended is None:
            continue
        current = grouped.setdefault(row.config_id, {}).get(scheduled)
        if current is None:
            grouped[row.config_id][scheduled] = (started, ended)
            continue
        grouped[row.config_id][scheduled] = (
            min(current[0], started),
            max(current[1], ended),
        )
    return {
        config_id: sorted(
            (scheduled, started, ended) for scheduled, (started, ended) in slots.items()
        )
        for config_id, slots in grouped.items()
    }


def _paired_overlap(
    slots: dict[str, list[tuple[datetime, datetime, datetime]]],
    producer: str,
    reader: str,
) -> tuple[int, int]:
    left = slots.get(producer, [])
    right = slots.get(reader, [])
    times = [item[0] for item in left]
    late = 0
    shared = 0
    for scheduled, started, _ended in right:
        index = bisect.bisect_right(times, scheduled) - 1
        if index < 0:
            continue
        shared += 1
        _pscheduled, _pstarted, producer_end = left[index]
        if producer_end > started:
            late += 1
    return late, shared


def _aware(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _hourly(schedule: str | None) -> bool:
    return "hour" in (schedule or "").lower()


def _writers(graph: Graph) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.relation == "writes" and not edge.uncertain:
            grouped.setdefault(edge.target, []).append(
                edge.source.removeprefix("query:")
            )
    return {
        table: tuple(sorted(set(config_ids))) for table, config_ids in grouped.items()
    }


def _in_cycle(graph: Graph, left: str, right: str) -> bool:
    for component in strongly_connected(graph):
        ids = {
            node.removeprefix("query:")
            for node in component
            if node.startswith("query:")
        }
        if left in ids and right in ids:
            return True
    return False
