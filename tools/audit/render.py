"""Render AUDIT.txt from audit findings and the estate graph.

The text is deterministic. It separates evidence from recommendations and
does not include a timestamp.
"""

from __future__ import annotations

import textwrap

from audit.common import CATEGORY_ORDER
from audit.schedule import INVERTED_TIMESTAMP_RULE
from audit.models import AuditFinding, Category
from estate.models import Inventory
from graph.analyze import (
    downstream_lineage,
    duplicate_writers,
    migration_scope,
    strongly_connected,
    tables_named,
)
from graph.traverse import upstream
from graph.models import Graph
from identity import resolve_projects
from sql.models import SqlParse

_CATEGORIES: tuple[Category, ...] = ("operational", "correctness", "maintainability")
_WIDTH = 88


def render_audit(
    inventory: Inventory,
    parses: tuple[SqlParse, ...],
    graph: Graph,
    findings: tuple[AuditFinding, ...],
) -> str:
    """Render the audit report.

    Args:
        inventory: Loaded estate.
        parses: Parses for the paired queries.
        graph: Dependency graph.
        findings: Output of ``collect_findings``, already sorted.

    Returns:
        The report, ending with a newline.
    """
    parts = [
        _header(),
        _executive(findings),
        _summary(inventory, parses, graph),
        _dependencies(graph),
        _register(findings),
        _details(findings),
        _retirement(findings),
        _limitations(inventory, graph),
    ]
    return "\n\n".join(parts) + "\n"


def _header() -> str:
    """Return the title block and the confidence legend.

    Returns:
        Lines joined with newlines. No timestamp is included.
    """
    return "\n".join(
        [
            "AUDIT",
            "Scheduled-query estate",
            "Source directory: task2_estate/",
            "Regenerate: python -m audit_report",
            "",
            "Evidence lines are observations from the supplied files.",
            "Impact lines are consequences that still need a person or BigQuery.",
            "Recommendation lines are not actions that have been taken.",
            "confirmed: the files show the fact. It is not a production incident.",
            "indicated: the files support the reading and do not prove the published data.",
            "needs_verification: the next check needs BigQuery or the owner.",
        ]
    )


def _executive(findings: tuple[AuditFinding, ...]) -> str:
    """Return the counts and the highest-priority titles.

    Args:
        findings: Sorted audit findings.

    Returns:
        The executive-summary section.
    """
    counts = {"confirmed": 0, "indicated": 0, "needs_verification": 0}
    for finding in findings:
        counts[finding.confidence] += 1
    lines = [
        "Executive summary",
        (
            f"Findings: {len(findings)}. confirmed: {counts['confirmed']}. "
            f"indicated: {counts['indicated']}. "
            f"needs_verification: {counts['needs_verification']}."
        ),
        "Confirmed entries are observations in the supplied files.",
        "Indicated and needs_verification entries are not proven production defects.",
        "Confirmed overlap is a clock fact. It does not prove a stale read or a missing table.",
        "Highest-priority confirmed findings:",
    ]
    lines.extend(_ranked(findings, "confirmed"))
    lines.append("Highest-priority findings that still need validation:")
    lines.extend(_ranked(findings, "needs_verification"))
    lines.append("Retirement candidates in section 5 are not approved for disabling.")
    return "\n".join(lines)


def _ranked(findings: tuple[AuditFinding, ...], confidence: str) -> list[str]:
    """Return up to five titles at one confidence, highest priority first.

    Args:
        findings: Audit findings.
        confidence: ``confirmed``, ``indicated``, or ``needs_verification``.

    Returns:
        ``P<n> <id>: <title>`` lines, or ``["none"]`` when nothing matches.
    """
    chosen = sorted(
        (finding for finding in findings if finding.confidence == confidence),
        key=lambda finding: (
            finding.priority,
            CATEGORY_ORDER[finding.category],
            finding.finding_id,
        ),
    )
    if not chosen:
        return ["none"]
    return [
        f"P{finding.priority} {finding.finding_id}: {finding.title}"
        for finding in chosen[:5]
    ]


def _summary(
    inventory: Inventory,
    parses: tuple[SqlParse, ...],
    graph: Graph,
) -> str:
    """Return section 1, the estate and graph counts.

    Args:
        inventory: Loaded estate.
        parses: One parse per paired query.
        graph: Dependency graph.

    Returns:
        The estate-summary section.
    """
    failed = sum(not item.parsed for item in parses)
    enabled = sum(query.enabled is True for query in inventory.queries)
    disabled = sum(query.enabled is False for query in inventory.queries)
    lines = [
        "1. Estate summary",
        f"Queries considered: {len(inventory.queries)} paired scheduled queries.",
        f"SQL files: {len(inventory.sql_files)}. Transfer configs: {len(inventory.transfer_configs)}.",
        f"Enabled: {enabled}. Disabled: {disabled}.",
        f"Run history rows: {len(inventory.history)}. Usage rows: {len(inventory.usage)}.",
        f"Catalog rows: {len(inventory.catalog)}. Schema tables: {len(inventory.schemas)}.",
        f"Inventory findings: {len(inventory.findings)}.",
        f"SQL parse succeeded: {len(parses) - failed}. SQL parse failed: {failed}.",
        f"Graph nodes: {len(graph.nodes)}. Graph edges: {len(graph.edges)}.",
        (
            "Graph edges by relation and confidence: "
            + ", ".join(_edge_bits(graph))
            + "."
        ),
    ]
    return "\n".join(lines)


def _dependencies(graph: Graph) -> str:
    """Return section 2, the revenue_daily slice and structural findings.

    Args:
        graph: Dependency graph.

    Returns:
        The dependency-overview section.
    """
    targets = tables_named(graph, "revenue_daily")
    upstream_ids = sorted(
        {node for target in targets for node in upstream(graph, target)}
    )
    downstream_ids = sorted(
        {
            node
            for target in targets
            for node in downstream_lineage(graph, "revenue_daily")
        }
    )
    scope = migration_scope(graph)
    cycles = [
        component
        for component in strongly_connected(graph)
        if sum(node.startswith("query:") for node in component) >= 2
    ]
    writers = duplicate_writers(graph)
    lines = [
        "2. Dependency overview",
        "Edges point from a source table to a query, then to that query's transfer-config destination.",
        "Dashboard and ad-hoc usage rows are consumers. They do not add SQL edges.",
        f"Tables named revenue_daily: {', '.join(targets) or 'none'}.",
        "Upstream of that table: " + _short(upstream_ids) + ".",
        "Downstream lineage, including external consumers: "
        + _short(downstream_ids)
        + ".",
        (
            "The proposed migration scope is a graph slice, not a decision in this audit: "
            + _short(scope)
            + "."
        ),
        "Duplicate writers: "
        + (
            _short(
                tuple(f"{table} ({' '.join(queries)})" for table, queries in writers)
            )
            or "none"
        )
        + ".",
        f"Query cycles: {len(cycles)}.",
    ]
    return "\n".join(lines)


def _register(findings: tuple[AuditFinding, ...]) -> str:
    """Return section 3, findings grouped by category.

    Args:
        findings: Findings already sorted by category, priority, and id.

    Returns:
        The problem-register section.
    """
    lines = [
        "3. Problem register",
        "Sorted by category, then priority, then finding id.",
    ]
    counts: dict[str, int] = {category: 0 for category in _CATEGORIES}
    for finding in findings:
        counts[finding.category] += 1
    for category in _CATEGORIES:
        lines.append(f"{category}: {counts[category]}")
    lines.append(f"total: {len(findings)}")
    for category in _CATEGORIES:
        lines.append("")
        lines.append(category.upper())
        for finding in findings:
            if finding.category != category:
                continue
            lines.append(
                f"P{finding.priority} {finding.finding_id} [{finding.confidence}] {finding.title}"
            )
    return "\n".join(lines)


def _details(findings: tuple[AuditFinding, ...]) -> str:
    """Return section 4, one block per finding.

    Args:
        findings: Findings in register order.

    Returns:
        The findings section. Blocks are separated by a blank line.
    """
    blocks = ["4. Findings"]
    for finding in findings:
        blocks.append(_one(finding))
    return "\n\n".join(blocks)


def _one(finding: AuditFinding) -> str:
    """Format one finding as labeled lines.

    Args:
        finding: Audit item. Long text is wrapped to the report width.

    Returns:
        The finding block, without a trailing blank line.
    """
    paths = ", ".join(finding.sql_paths)
    configs = ", ".join(finding.config_ids)
    return "\n".join(
        [
            f"{finding.finding_id}",
            f"Category: {finding.category}",
            f"Priority: {finding.priority}",
            f"Confidence: {finding.confidence}",
            f"Title: {finding.title}",
            f"Config IDs: {configs}",
            "SQL files: " + _wrap(paths),
            "Evidence: " + _wrap(finding.evidence),
            "Impact: " + _wrap(finding.impact),
            "Recommendation: " + _wrap(finding.recommendation),
        ]
    )


def _retirement(findings: tuple[AuditFinding, ...]) -> str:
    """Return section 5, retirement candidates and already-disabled queries.

    Args:
        findings: Audit findings. Only ``MN-RETIRE`` and
            ``OP-ALREADY-DISABLED`` ids are listed.

    Returns:
        The retirement section.
    """
    chosen = [
        finding
        for finding in findings
        if finding.finding_id.startswith("MN-RETIRE")
        or finding.finding_id.startswith("OP-ALREADY-DISABLED")
    ]
    lines = [
        "5. Retirement candidates",
        "A candidate has no certain or uncertain read of its destination and no",
        "dashboard, ad-hoc, or scheduled_query row in downstream_usage.csv.",
        "The usage file covers only the supplied extract.",
        "These candidates cannot be disabled automatically.",
        "The extract can miss a reader. SQL that did not parse can hide a reference.",
        "The catalog can still contain the tables.",
        "Disabling a schedule does not delete a table, and this audit does not recommend deletion.",
        "Production validation means an owner confirms the table can stop being refreshed.",
    ]
    if not chosen:
        lines.append("Candidates: none.")
        return "\n".join(lines)
    for finding in chosen:
        lines.append(
            f"{finding.finding_id} [{finding.confidence}] {', '.join(finding.config_ids)}"
        )
    lines.append("A Not found failure does not by itself add or remove a candidate.")
    lines.append(
        "A destination with any usage row, including a dashboard, is not a candidate."
    )
    return "\n".join(lines)


def _limitations(inventory: Inventory, graph: Graph) -> str:
    """Return section 6, what the supplied files do not prove.

    Args:
        inventory: Loaded estate, used for project resolution.
        graph: Dependency graph, including uncertain edges.

    Returns:
        The limitations section.
    """
    resolution = resolve_projects(inventory, graph)
    uncertain = sum(edge.uncertain for edge in graph.edges)
    sql = " ".join(resolution.sql_projects) or "none"
    config = " ".join(resolution.config_projects) or "none"
    return "\n".join(
        [
            "6. Limitations",
            f"Uncertain read edges: {uncertain}. They are recovered backtick names from SQL that did not parse.",
            "Those names are labeled uncertain in the findings and are not used as confirmed lineage.",
            f"SQL projects observed on table nodes: {sql}.",
            f"Transfer-config resource projects: {config}.",
            f"Project resolution: {resolution.status}.",
            "The graph joins tables on dataset.table. It does not copy the config project number onto the SQL project.",
            "No table in this estate was seen under two SQL projects. A conflict would be reported and not given a single project.",
            "run_history SUCCEEDED means the recorded job succeeded. It does not mean the checked-in SQL parses.",
            "Catalog column counts are not column names. A missing catalog row is not proof the table is absent from production.",
            "Schedule pairs use the timestamp offset. Config timeZone is not applied on top of that offset.",
            "A later run on the same calendar date is not treated as the predecessor of an earlier reader.",
            INVERTED_TIMESTAMP_RULE,
            "Lookback days come from current-timestamp predicates, not from every INTERVAL expression.",
            "Partition predicates are judged per source. No execution plan was requested.",
            "No query was executed. No production incident is inferred beyond the history states and error strings.",
            "An unresolved star is not reported as a missing column.",
        ]
    )


def _edge_bits(graph: Graph) -> list[str]:
    """Count edges by relation and by whether the read was recovered.

    Args:
        graph: Dependency graph.

    Returns:
        ``"<relation> <confirmed|uncertain> <count>"`` for reads, writes,
        and consumes, in that order.
    """
    bits = []
    for relation in ("reads", "writes", "consumes"):
        for uncertain, label in ((False, "confirmed"), (True, "uncertain")):
            count = sum(
                edge.relation == relation and edge.uncertain is uncertain
                for edge in graph.edges
            )
            bits.append(f"{relation} {label} {count}")
    return bits


def _short(values: tuple[str, ...] | list[str]) -> str:
    """Join values with spaces, or return ``none`` when the sequence is empty.

    Args:
        values: Already ordered ids or labels.

    Returns:
        A single line fragment.
    """
    if not values:
        return "none"
    return " ".join(values)


def _wrap(text: str) -> str:
    """Collapse whitespace and wrap text to the report width.

    Args:
        text: Evidence, impact, or recommendation prose.

    Returns:
        Wrapped text. Continuation lines are indented two spaces.
    """
    packed = " ".join(text.split())
    return textwrap.fill(packed, width=_WIDTH, subsequent_indent="  ")
