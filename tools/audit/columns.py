"""Column references that the upstream scheduled SQL does not project.

Bindings come from the dependency graph and from sqlglot. A star that cannot
be expanded stays unknown and is not reported as a missing column.
"""

from __future__ import annotations

from estate.models import ScheduledQuery
from graph.models import Graph

from audit.common import certain_reads, make_finding, queries_for, writers_by_table
from audit.models import AuditFinding
from audit.sql_ast import (
    NameStatus,
    column_state,
    columns_by_table,
    merge_schemas,
    output_schema,
)

_ABSENT = (
    "Checked-in SQL for every writer omits the column. The published table was not queried, "
    "so a SUCCEEDED history row does not prove the live schema matches the file."
)
_CONFLICT = "The writers do not agree. This is not a claim that the column is missing from the live table."


def column_findings(
    by_id: dict[str, ScheduledQuery],
    graph: Graph,
) -> list[AuditFinding]:
    """Find reader columns the scheduled writers do not all project.

    Args:
        by_id: Paired queries keyed by config id.
        graph: Dependency graph. Only certain reads are compared.

    Returns:
        Zero, one, or two findings: definite omissions, then writer conflicts.
    """
    schemas = _schemas(by_id, graph)
    writers = writers_by_table(graph)
    absent: list[tuple[str, str, str, tuple[str, ...]]] = []
    conflict: list[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]] = []
    for table, reader in certain_reads(graph):
        query = by_id.get(reader)
        writer_ids = writers.get(table, ())
        if query is None or not writer_ids:
            continue
        if any(config_id not in schemas for config_id in writer_ids):
            continue
        referenced = columns_by_table(query.sql_text)
        if referenced is None:
            continue
        merged = merge_schemas([schemas[config_id] for config_id in writer_ids])
        for name in sorted(referenced.get(table, ())):
            state = column_state(merged, name)
            if state == "absent":
                absent.append((reader, name, table, writer_ids))
            elif state == "conflict":
                present, missing = _split_writers(schemas, writer_ids, name)
                conflict.append((reader, name, table, present, missing))
    findings = []
    if absent:
        findings.append(_absent_finding(by_id, absent))
    if conflict:
        findings.append(_conflict_finding(by_id, conflict))
    return findings


def _schemas(by_id: dict[str, ScheduledQuery], graph: Graph) -> dict[str, NameStatus]:
    """Derive each query's output names, expanding a star from its writers.

    A query whose SQL does not parse is omitted. Expansion repeats until a
    pass changes nothing, or one more pass than the number of parsed queries.

    Args:
        by_id: Paired queries keyed by config id.
        graph: Certain write edges name the writers used to expand a star.

    Returns:
        Config id to output-name status. Unparsed queries are absent.
    """
    schemas: dict[str, NameStatus] = {}
    for query in by_id.values():
        direct = output_schema(query.sql_text)
        if direct is not None:
            schemas[query.config_id] = direct
    writers = writers_by_table(graph)
    ordered = tuple(sorted(schemas))
    for _ in range(len(ordered) + 1):
        changed = False
        updated = dict(schemas)

        def resolve(config_id: str, table: str) -> NameStatus | None:
            """Merge other writers of ``table``, skipping this query.

            Args:
                config_id: Query whose star is being expanded.
                table: ``dataset.table`` named by that star.

            Returns:
                The merged writer schema, or ``None`` when no other parsed
                writer is known yet.
            """
            ids = [
                writer
                for writer in writers.get(table, ())
                if writer != config_id and writer in schemas
            ]
            if not ids:
                return None
            return merge_schemas([schemas[writer] for writer in ids])

        for config_id in ordered:
            expanded = output_schema(
                by_id[config_id].sql_text,
                resolve=lambda table, config_id=config_id: resolve(config_id, table),
            )
            if expanded is not None and expanded != schemas[config_id]:
                updated[config_id] = expanded
                changed = True
        schemas = updated
        if not changed:
            break
    return schemas


def _split_writers(
    schemas: dict[str, NameStatus], writer_ids: tuple[str, ...], name: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split writers into those that project ``name`` and those that omit it.

    A writer whose state is neither present nor absent is left out of both
    groups.

    Args:
        schemas: Output status keyed by writer config id.
        writer_ids: Writers of one destination, already known to have a schema.
        name: Column the reader uses.

    Returns:
        Present config ids, then absent config ids, each in writer order.
    """
    present = []
    missing = []
    for config_id in writer_ids:
        state = column_state(schemas[config_id], name)
        if state == "present":
            present.append(config_id)
        elif state == "absent":
            missing.append(config_id)
    return tuple(present), tuple(missing)


def _absent_finding(
    by_id: dict[str, ScheduledQuery],
    pairs: list[tuple[str, str, str, tuple[str, ...]]],
) -> AuditFinding:
    """Build the finding for columns every checked-in writer omits.

    Args:
        by_id: Paired queries keyed by config id.
        pairs: ``(reader, column, table, writer ids)`` for definite omissions.

    Returns:
        ``CR-COLUMN-NOT-PROJECTED`` at needs_verification.
    """
    ids = {reader for reader, _name, _table, writers in pairs} | {
        writer for _reader, _name, _table, writers in pairs for writer in writers
    }
    lines = [
        (
            f"{reader} reads {name} from {table}; "
            f"{', '.join(writers)} "
            f"{'does' if len(writers) == 1 else 'do'} not project {name}."
        )
        for reader, name, table, writers in pairs
    ]
    return make_finding(
        "CR-COLUMN-NOT-PROJECTED",
        "correctness",
        "needs_verification",
        1,
        "Downstream SQL names a column the upstream scheduled SQL does not project",
        queries_for(by_id, ids),
        " ".join(lines) + " " + _ABSENT,
        "Compiling the reader against the writer file would reject the column. The live table may still contain it.",
        "Do not rename the column until the owner confirms which schema is published.",
    )


def _conflict_finding(
    by_id: dict[str, ScheduledQuery],
    pairs: list[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]],
) -> AuditFinding:
    """Build the finding for columns the writers do not all project.

    Args:
        by_id: Paired queries keyed by config id.
        pairs: ``(reader, column, table, present writers, missing writers)``.

    Returns:
        ``CR-COLUMN-WRITER-CONFLICT`` at needs_verification.
    """
    ids = {reader for reader, *_rest in pairs}
    for _reader, _name, _table, present, missing in pairs:
        ids.update(present)
        ids.update(missing)
    lines = []
    for reader, name, table, present, missing in pairs:
        has = ", ".join(present) or "none"
        lacks = ", ".join(missing) or "none"
        lines.append(
            f"{reader} reads {name} from {table}; projected by {has}; omitted by {lacks}."
        )
    return make_finding(
        "CR-COLUMN-WRITER-CONFLICT",
        "correctness",
        "needs_verification",
        2,
        "Scheduled writers of one table do not project the same columns a reader uses",
        queries_for(by_id, ids),
        " ".join(lines) + " " + _CONFLICT,
        "A reader can see a column after one writer and fail after the other writer replaces or appends the table.",
        "Do not drop either writer here. Ask the owner which projection is authoritative.",
    )
