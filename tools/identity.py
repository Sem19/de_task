"""Project labels for table identities.

The graph joins tables on ``dataset.table``. A project seen on a reference
stays on that node. This module does not rewrite those nodes. It only decides
whether one configuration project may be shown for tables the source left
unqualified. Two different project labels are never combined.
"""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

from estate.models import Inventory
from graph.models import Graph, Node

_CONFIG_PROJECT = re.compile(r"\Aprojects/([^/]+)/")
Status = Literal["unambiguous", "unresolved", "conflict"]


class ProjectResolution(NamedTuple):
    """Whether unqualified tables may display one project.

    ``project`` is set only when every observed SQL project and every transfer
    config project are the same single label. A conflict means one
    ``dataset.table`` was seen under more than one project.
    """

    status: Status
    project: str | None
    sql_projects: tuple[str, ...]
    config_projects: tuple[str, ...]
    conflicts: tuple[str, ...]


def resolve_projects(inventory: Inventory, graph: Graph) -> ProjectResolution:
    """Compare SQL projects with transfer-config projects.

    Args:
        inventory: Loaded estate. Config projects come from resource names.
        graph: Dependency graph. SQL projects come from table nodes.

    Returns:
        A resolution. ``unambiguous`` is the only status that names a project.
    """
    config = _config_projects(inventory)
    sql = _sql_projects(graph)
    conflicts = _conflicts(graph)
    if conflicts:
        return ProjectResolution("conflict", None, sql, config, conflicts)
    labels = set(sql) | set(config)
    if len(labels) == 1:
        return ProjectResolution("unambiguous", next(iter(labels)), sql, config, ())
    return ProjectResolution("unresolved", None, sql, config, ())


def display_table(node: Node, resolution: ProjectResolution) -> str:
    """Format a table without inventing a project.

    A project stored on the node is shown as ``project.dataset.table``. A
    single configuration project is shown only when resolution is unambiguous,
    and the label says it came from configuration. Anything else stays
    unqualified and is marked unresolved.

    Args:
        node: A table node.
        resolution: Result of ``resolve_projects``.

    Returns:
        A display name. Query and consumer nodes are not accepted.

    Raises:
        ValueError: ``node`` is not a table.
    """
    if node.kind != "table" or node.table is None:
        raise ValueError(f"not a table node: {node.id}")
    identity = node.table
    if len(node.projects) > 1:
        labels = " ".join(node.projects)
        return f"{identity.dataset_table} [projects {labels}; unresolved]"
    if identity.project:
        return f"{identity.project}.{identity.dataset_table}"
    if resolution.status == "unambiguous" and resolution.project:
        return f"{resolution.project}.{identity.dataset_table} [project from configuration]"
    return f"{identity.dataset_table} [project unresolved]"


def _config_projects(inventory: Inventory) -> tuple[str, ...]:
    found: set[str] = set()
    names = [config.transfer_name for config in inventory.transfer_configs]
    names.extend(query.transfer_name for query in inventory.queries)
    for name in names:
        if not name:
            continue
        match = _CONFIG_PROJECT.match(name)
        if match:
            found.add(match.group(1))
    return tuple(sorted(found))


def _sql_projects(graph: Graph) -> tuple[str, ...]:
    found: set[str] = set()
    for node in graph.nodes:
        found.update(node.projects)
    return tuple(sorted(found))


def _conflicts(graph: Graph) -> tuple[str, ...]:
    names = [
        node.table.dataset_table
        for node in graph.nodes
        if node.kind == "table" and node.table is not None and len(node.projects) > 1
    ]
    return tuple(sorted(names))
