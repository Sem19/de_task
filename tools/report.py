"""Human-readable estate analysis.

Counts and findings come from the loader, parser, and graph. The proposed
scope is a graph slice for review. It is not a migration decision, and it
does not drop a competing writer.
"""

from __future__ import annotations

from collections import defaultdict

from estate.identifiers import parse_table_id
from estate.models import Inventory
from graph.analyze import (
    duplicate_writers,
    migration_scope,
    missing_producers,
    self_reads,
    shared_source_siblings,
    strongly_connected,
    tables_named,
)
from graph.models import Graph, Node
from graph.traverse import downstream, upstream
from identity import ProjectResolution, display_table, resolve_projects
from sql.models import SqlParse
from sql.parse import format_issue

_RELATIONS = ("reads", "writes", "consumes")


def format_report(
    inventory: Inventory,
    parses: tuple[SqlParse, ...],
    graph: Graph,
    *,
    target: str,
) -> str:
    """Format one deterministic analysis report.

    Args:
        inventory: Loaded estate.
        parses: Parse results for the paired queries, in any order.
        graph: Dependency graph built from those results.
        target: Table name, ``dataset.table``, or ``project.dataset.table``.

    Returns:
        The report, without a trailing newline. SQL text is omitted.
    """
    resolution = resolve_projects(inventory, graph)
    sections = [
        _inventory(inventory),
        _parse(inventory, parses),
        _identity(resolution),
        _graph_counts(graph),
        _writers(graph, resolution),
        _missing(graph, resolution),
        _cycles(graph, resolution),
        _self_reads(graph, resolution),
        _lineage(graph, resolution, target),
        _scope(graph, resolution, target),
    ]
    return "\n".join(sections)


def _inventory(inventory: Inventory) -> str:
    enabled = sum(query.enabled is True for query in inventory.queries)
    disabled = sum(query.enabled is False for query in inventory.queries)
    counts: dict[str, int] = defaultdict(int)
    for finding in inventory.findings:
        counts[finding.kind] += 1
    lines = [
        f"inventory.root: {inventory.root}",
        f"inventory.sql_files: {len(inventory.sql_files)}",
        f"inventory.transfer_configs: {len(inventory.transfer_configs)}",
        f"inventory.queries: {len(inventory.queries)}",
        f"inventory.enabled: {enabled}",
        f"inventory.disabled: {disabled}",
        f"inventory.history_rows: {len(inventory.history)}",
        f"inventory.usage_rows: {len(inventory.usage)}",
        f"inventory.catalog_rows: {len(inventory.catalog)}",
        f"inventory.schema_tables: {len(inventory.schemas)}",
        f"inventory.findings: {len(inventory.findings)}",
    ]
    for kind in sorted(counts):
        lines.append(f"inventory.finding.{kind}: {counts[kind]}")
    for finding in inventory.findings:
        lines.append(f"inventory.issue: {finding.kind}: {finding.message}")
    return "\n".join(lines)


def _parse(inventory: Inventory, parses: tuple[SqlParse, ...]) -> str:
    by_id = {item.config_id: item for item in parses if item.config_id}
    ordered = tuple(by_id[query.config_id] for query in inventory.queries)
    failed = tuple(item for item in ordered if not item.parsed)
    confirmed = sum(
        1 for item in ordered for ref in item.references if not ref.uncertain
    )
    uncertain = sum(1 for item in ordered for ref in item.references if ref.uncertain)
    unresolved = sum(len(item.unresolved) for item in ordered)
    lines = [
        f"parse.queries: {len(ordered)}",
        f"parse.succeeded: {len(ordered) - len(failed)}",
        f"parse.failed: {len(failed)}",
        f"parse.references.confirmed: {confirmed}",
        f"parse.references.uncertain: {uncertain}",
        f"parse.references.unresolved: {unresolved}",
    ]
    for item in failed:
        for issue in item.errors:
            lines.append(f"parse.error: {format_issue(item, issue)}")
    for item in ordered:
        for ref in item.references:
            if ref.uncertain:
                lines.append(f"parse.uncertain: {item.config_id} {ref.raw} {ref.role}")
        for ref in item.unresolved:
            lines.append(f"parse.unresolved: {item.config_id} {ref.reason}:{ref.raw}")
    return "\n".join(lines)


def _identity(resolution: ProjectResolution) -> str:
    sql = " ".join(resolution.sql_projects) or "none"
    config = " ".join(resolution.config_projects) or "none"
    conflicts = " ".join(resolution.conflicts) or "none"
    project = resolution.project or "none"
    return "\n".join(
        [
            f"identity.sql_projects: {sql}",
            f"identity.config_projects: {config}",
            f"identity.resolution: {resolution.status}",
            f"identity.project: {project}",
            f"identity.conflicts: {conflicts}",
        ]
    )


def _graph_counts(graph: Graph) -> str:
    lines = [
        f"graph.nodes: {len(graph.nodes)}",
        f"graph.nodes.table: {_kind(graph, 'table')}",
        f"graph.nodes.query: {_kind(graph, 'query')}",
        f"graph.nodes.consumer: {_kind(graph, 'consumer')}",
        f"graph.edges: {len(graph.edges)}",
    ]
    confirmed = 0
    uncertain = 0
    for relation in _RELATIONS:
        certain_count = _edges(graph, relation, uncertain=False)
        uncertain_count = _edges(graph, relation, uncertain=True)
        confirmed += certain_count
        uncertain += uncertain_count
        lines.append(f"graph.edges.{relation}.confirmed: {certain_count}")
        lines.append(f"graph.edges.{relation}.uncertain: {uncertain_count}")
    lines.append(f"graph.edges.confirmed: {confirmed}")
    lines.append(f"graph.edges.uncertain: {uncertain}")
    return "\n".join(lines)


def _writers(graph: Graph, resolution: ProjectResolution) -> str:
    nodes = _nodes(graph)
    rows = duplicate_writers(graph)
    if not rows:
        return "duplicate_writer: none"
    lines = []
    for table_node_id, writers in rows:
        table = display_table(nodes[table_node_id], resolution)
        parts = [_writer_label(nodes[writer]) for writer in writers]
        lines.append(f"duplicate_writer: {table} {' '.join(parts)}")
    return "\n".join(lines)


def _missing(graph: Graph, resolution: ProjectResolution) -> str:
    nodes = _nodes(graph)
    names = tuple(
        display_table(nodes[node_id], resolution)
        for node_id in missing_producers(graph)
    )
    return _list("missing_producer", names)


def _cycles(graph: Graph, resolution: ProjectResolution) -> str:
    components = strongly_connected(graph)
    if not components:
        return "cycle: none"
    nodes = _nodes(graph)
    lines = []
    for component in components:
        labels = " ".join(
            _node_label(nodes[node_id], resolution) for node_id in component
        )
        lines.append(f"cycle: {labels}")
    return "\n".join(lines)


def _self_reads(graph: Graph, resolution: ProjectResolution) -> str:
    nodes = _nodes(graph)
    lines = []
    for node_id in self_reads(graph):
        query = nodes[node_id]
        tables = [
            display_table(nodes[edge.source], resolution)
            for edge in graph.edges
            if edge.relation == "reads"
            and not edge.uncertain
            and edge.target == node_id
            and any(
                other.relation == "writes"
                and not other.uncertain
                and other.source == node_id
                and other.target == edge.source
                for other in graph.edges
            )
        ]
        lines.append(f"self_read: {query.config_id} {' '.join(tables)}")
    return "\n".join(lines) if lines else "self_read: none"


def _lineage(graph: Graph, resolution: ProjectResolution, target: str) -> str:
    targets = _targets(graph, target)
    if not targets:
        return f"lineage.target: none\nlineage.requested: {target}"
    nodes = _nodes(graph)
    blocks = [f"lineage.requested: {target}"]
    for node_id in targets:
        node = nodes[node_id]
        blocks.append(f"lineage.target: {display_table(node, resolution)}")
        blocks.append("lineage.writers: " + _writer_list(graph, nodes, node_id))
        blocks.append(
            _list(
                "lineage.upstream",
                tuple(
                    _node_label(nodes[item], resolution)
                    for item in upstream(graph, node_id)
                ),
            )
        )
        descendants = downstream(graph, node_id)
        blocks.append(
            _list(
                "lineage.downstream",
                tuple(
                    _node_label(nodes[item], resolution)
                    for item in descendants
                    if nodes[item].kind != "consumer"
                ),
            )
        )
        blocks.append(
            _list(
                "lineage.consumers",
                tuple(
                    _node_label(nodes[item], resolution)
                    for item in descendants
                    if nodes[item].kind == "consumer"
                ),
            )
        )
    return "\n".join(blocks)


def _scope(graph: Graph, resolution: ProjectResolution, target: str) -> str:
    table_name = _scope_name(graph, target)
    selected = migration_scope(graph, table_name) if table_name else ()
    nodes = _nodes(graph)
    queries = tuple(
        nodes[node_id].config_id or ""
        for node_id in selected
        if nodes[node_id].kind == "query"
    )
    tables = tuple(
        display_table(nodes[node_id], resolution)
        for node_id in selected
        if nodes[node_id].kind == "table"
    )
    excluded = shared_source_siblings(graph, selected) if selected else ()
    return "\n".join(
        [
            "proposed_scope: graph slice only; not a migration decision",
            _list("proposed_scope.queries", queries),
            _list("proposed_scope.tables", tables),
            f"proposed_scope.excluded_siblings: {len(excluded)}",
        ]
    )


def _targets(graph: Graph, target: str) -> tuple[str, ...]:
    text = target.strip().strip("`")
    if "." not in text and ":" not in text:
        return tables_named(graph, text)
    try:
        parsed = parse_table_id(text)
    except ValueError:
        return ()
    found = []
    for node in graph.nodes:
        if node.kind != "table" or node.table is None:
            continue
        if node.table.dataset_table != parsed.dataset_table:
            continue
        if parsed.project and node.projects and parsed.project not in node.projects:
            continue
        found.append(node.id)
    return tuple(sorted(found))


def _scope_name(graph: Graph, target: str) -> str | None:
    targets = _targets(graph, target)
    if not targets:
        return None
    nodes = _nodes(graph)
    names = {
        nodes[node_id].table.table
        for node_id in targets
        if nodes[node_id].table is not None
    }
    if len(names) != 1:
        return None
    return next(iter(names))


def _writer_list(graph: Graph, nodes: dict[str, Node], table_node: str) -> str:
    writers = [
        _writer_label(nodes[edge.source])
        for edge in graph.edges
        if edge.relation == "writes"
        and not edge.uncertain
        and edge.target == table_node
    ]
    return " ".join(writers) if writers else "none"


def _writer_label(node: Node) -> str:
    return f"{node.config_id} {node.write_disposition or 'unknown'}"


def _node_label(node: Node, resolution: ProjectResolution) -> str:
    if node.kind == "query":
        return f"query {node.config_id}"
    if node.kind == "consumer":
        return f"consumer {node.consumer_kind} {node.consumer_id}"
    return f"table {display_table(node, resolution)}"


def _kind(graph: Graph, kind: str) -> int:
    return sum(node.kind == kind for node in graph.nodes)


def _edges(graph: Graph, relation: str, *, uncertain: bool) -> int:
    return sum(
        edge.relation == relation and edge.uncertain is uncertain
        for edge in graph.edges
    )


def _nodes(graph: Graph) -> dict[str, Node]:
    return {node.id: node for node in graph.nodes}


def _list(label: str, values: tuple[str, ...]) -> str:
    if not values:
        return f"{label}: none"
    return f"{label}: " + " ".join(values)
