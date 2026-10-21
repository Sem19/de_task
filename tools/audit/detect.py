"""Collect audit findings from the loaded estate, parses, and graph.

Each rule lives in its own module. This module keeps the rules that read
graph structure or history strings directly, and it orders the result.
Rules do not modify SQL and they do not decide that a query should be deleted.
"""

from __future__ import annotations

import re
from collections import defaultdict

import sqlglot
from sqlglot import exp

from audit.columns import column_findings
from audit.common import make_finding, queries_for, sort_key
from audit.lookback import lookback_findings
from audit.models import AuditFinding
from audit.partition import partition_findings
from audit.retirement import retirement_findings
from audit.schedule import schedule_findings
from audit.sources import source_findings
from audit.sql_ast import projection_count
from estate.models import Inventory, ScheduledQuery
from graph.analyze import (
    duplicate_writers,
    parse_failures,
    self_reads,
    strongly_connected,
)
from graph.models import Graph
from sql.models import SqlParse

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_SPACE = re.compile(r"\s+")


def collect_findings(
    inventory: Inventory,
    parses: tuple[SqlParse, ...],
    graph: Graph,
) -> tuple[AuditFinding, ...]:
    """Run every audit rule and return findings in a stable order.

    Args:
        inventory: Loaded estate.
        parses: One parse per paired query.
        graph: Dependency graph for those queries.

    Returns:
        Findings sorted by category, priority, then finding id.
    """
    by_id = {query.config_id: query for query in inventory.queries}
    parsed = {item.config_id: item for item in parses if item.config_id}
    found = [
        *_duplicate_writers(inventory, graph, by_id),
        *_cycles(graph, by_id),
        *_self_reads(graph, by_id),
        *schedule_findings(inventory, graph, by_id),
        *_unparseable(inventory, parsed, graph, by_id),
        *source_findings(inventory, graph, by_id),
        *_resources_exceeded(inventory, by_id),
        *lookback_findings(graph, by_id),
        *column_findings(by_id, graph),
        *_row_number(inventory, by_id),
        *_schema_counts(inventory, by_id),
        *partition_findings(inventory, by_id),
        *_repeated_sql(by_id),
        *retirement_findings(inventory, graph, by_id),
    ]
    return tuple(sorted(found, key=sort_key))


def _duplicate_writers(
    inventory: Inventory,
    graph: Graph,
    by_id: dict[str, ScheduledQuery],
) -> list[AuditFinding]:
    findings = []
    for table_node, writers in duplicate_writers(graph):
        config_ids = tuple(writer.removeprefix("query:") for writer in writers)
        queries = tuple(by_id[config_id] for config_id in config_ids)
        table = table_node.removeprefix("table:")
        detail = "; ".join(
            f"{query.config_id} {query.write_disposition} schedule {query.schedule}"
            for query in queries
        )
        shared = any(
            finding.kind == "shared_destination" and table in finding.message
            for finding in inventory.findings
        )
        findings.append(
            make_finding(
                "OP-DUPLICATE-WRITER-" + "-".join(config_ids),
                "operational",
                "confirmed",
                1,
                f"{table} has more than one scheduled writer",
                queries,
                (
                    f"Graph write edges and transfer configs name the same destination "
                    f"{table}. {detail}. inventory finding shared_destination is present: {shared}."
                ),
                "The jobs can replace or append different column meanings in one table.",
                "Keep both jobs in the registry. Do not drop either writer in this audit. "
                "Ask the owner which definition is authoritative before disabling one.",
            )
        )
    return findings


def _cycles(graph: Graph, by_id: dict[str, ScheduledQuery]) -> list[AuditFinding]:
    findings = []
    for component in strongly_connected(graph):
        config_ids = tuple(
            sorted(
                node.removeprefix("query:")
                for node in component
                if node.startswith("query:")
            )
        )
        if len(config_ids) < 2:
            continue
        tables = tuple(
            sorted(
                node.removeprefix("table:")
                for node in component
                if node.startswith("table:")
            )
        )
        findings.append(
            make_finding(
                "OP-CYCLE-" + "-".join(config_ids),
                "operational",
                "confirmed",
                2,
                "Scheduled queries form a dependency cycle",
                queries_for(by_id, config_ids),
                "Certain read and write edges connect "
                + ", ".join(config_ids)
                + " through "
                + ", ".join(tables)
                + ".",
                "A cycle has no safe first run. Each query can read the other query's previous output.",
                "Do not schedule these as a simple chain. Confirm with the owner which edge is the intended lag.",
            )
        )
    return findings


def _self_reads(graph: Graph, by_id: dict[str, ScheduledQuery]) -> list[AuditFinding]:
    findings = []
    for node_id in self_reads(graph):
        config_id = node_id.removeprefix("query:")
        query = by_id[config_id]
        table = query.destination.dataset_table if query.destination else "unknown"
        findings.append(
            make_finding(
                f"OP-SELF-READ-{config_id}",
                "operational",
                "confirmed",
                2,
                f"{config_id} reads its own destination",
                (query,),
                f"A certain read edge and the transfer-config destination are both {table}.",
                "The query depends on rows it wrote on an earlier run. A full rebuild is not the same as the daily job.",
                "Treat the self-read as part of the contract. Do not replace it with a table that only reads upstream.",
            )
        )
    return findings


def _unparseable(
    inventory: Inventory,
    parsed: dict[str, SqlParse],
    graph: Graph,
    by_id: dict[str, ScheduledQuery],
) -> list[AuditFinding]:
    failed = parse_failures(graph)
    if not failed:
        return []
    queries = tuple(by_id[node.config_id] for node in failed if node.config_id in by_id)
    states = _state_counts(inventory)
    lines = []
    for query in queries:
        item = parsed[query.config_id]
        issue = item.errors[0]
        location = f"{issue.line}:{issue.col}" if issue.line is not None else "unknown"
        counts = states.get(query.config_id, {})
        uncertain = (
            ", ".join(ref.raw for ref in item.references if ref.uncertain) or "none"
        )
        lines.append(
            f"{query.config_id} {location} {issue.message}; "
            f"history {counts or 'no rows'}; uncertain refs {uncertain}"
        )
    count = len(queries)
    return [
        make_finding(
            "CR-UNPARSEABLE-SQL",
            "correctness",
            "confirmed",
            1,
            f"sqlglot cannot parse {count} scheduled queries",
            queries,
            " ".join(lines)
            + " Uncertain references are recovered backtick names, not confirmed reads. "
            "SUCCEEDED in run_history is not evidence that the file parses.",
            "A later editor can ship SQL the parser, and possibly BigQuery, rejects or reads differently.",
            "Repair the comment that sits inside the expression only after comparing the file with the last job that BigQuery accepted. Do not treat the recovered table names as lineage.",
        )
    ]


def _resources_exceeded(
    inventory: Inventory, by_id: dict[str, ScheduledQuery]
) -> list[AuditFinding]:
    counts: dict[str, int] = defaultdict(int)
    for row in inventory.history:
        message = row.raw.get("error_message") or ""
        if message.startswith("Resources exceeded"):
            counts[row.config_id] += 1
    if not counts:
        return []
    config_ids = tuple(sorted(counts))
    detail = ", ".join(f"{config_id}:{counts[config_id]}" for config_id in config_ids)
    return [
        make_finding(
            "OP-RESOURCES-EXCEEDED",
            "operational",
            "confirmed",
            2,
            "Run history records Resources exceeded failures",
            queries_for(by_id, config_ids),
            f"run_history.csv error_message starts with 'Resources exceeded'. Counts by config_id: {detail}.",
            "Those runs wrote no successful output for that attempt. The message does not identify which slot or shuffle limit was hit.",
            "Investigate the highest counts in BigQuery. This audit does not change their SQL.",
        )
    ]


def _row_number(
    inventory: Inventory, by_id: dict[str, ScheduledQuery]
) -> list[AuditFinding]:
    hits = []
    for query in inventory.queries:
        if "ROW_NUMBER" not in query.sql_text.upper():
            continue
        try:
            trees = sqlglot.parse(query.sql_text, read="bigquery")
        except sqlglot.errors.ParseError:
            continue
        for tree in trees:
            if tree is None:
                continue
            for window in tree.find_all(exp.Window):
                name = (
                    window.this.sql(dialect="bigquery")
                    if window.this is not None
                    else ""
                )
                if "ROW_NUMBER" in name.upper() and not window.args.get("order"):
                    hits.append(query.config_id)
                    break
    if not hits:
        return []
    config_ids = tuple(sorted(set(hits)))
    return [
        make_finding(
            "CR-ROW-NUMBER-NO-ORDER",
            "correctness",
            "confirmed",
            2,
            "ROW_NUMBER has no ORDER BY",
            queries_for(by_id, config_ids),
            "sqlglot windows for "
            + ", ".join(config_ids)
            + " use ROW_NUMBER and have an empty order clause.",
            "Which source row survives the dedupe is not determined by the SQL.",
            "Do not add an ORDER BY until the owner names the sort. The current statement is what history ran.",
        )
    ]


def _schema_counts(
    inventory: Inventory, by_id: dict[str, ScheduledQuery]
) -> list[AuditFinding]:
    catalog = {
        record.table.dataset_table: int(record.raw["columns"])
        for record in inventory.catalog
        if record.table is not None and record.raw.get("columns", "").isdigit()
    }
    mismatches = []
    for query in inventory.queries:
        if query.destination is None:
            continue
        key = query.destination.dataset_table
        if key not in catalog:
            continue
        projected = projection_count(query.sql_text)
        if projected is None or projected == catalog[key]:
            continue
        mismatches.append((query.config_id, key, projected, catalog[key]))
    if not mismatches:
        return []
    config_ids = tuple(item[0] for item in mismatches)
    examples = "; ".join(
        f"{config_id} {table} sql {sql_count} catalog {catalog_count}"
        for config_id, table, sql_count, catalog_count in mismatches[:4]
    )
    return [
        make_finding(
            "CR-SCHEMA-COUNT",
            "correctness",
            "indicated",
            2,
            "Outermost SELECT lists do not match catalog column counts",
            queries_for(by_id, config_ids),
            (
                f"{len(mismatches)} destinations differ. The count is the outermost "
                "SELECT list. Inner CTE lists, stars, and disagreeing UNION branches "
                f"are excluded. Examples, in config_id order: {examples}. "
                "table_catalog.columns is a snapshot count, not a column-by-column schema."
            ),
            "A consumer written against the catalog can name a column the current SQL no longer selects, or the reverse.",
            "Do not alter projections to match the catalog. Reconcile the published schema before migration.",
        )
    ]


def _repeated_sql(by_id: dict[str, ScheduledQuery]) -> list[AuditFinding]:
    groups: dict[str, list[str]] = defaultdict(list)
    for query in by_id.values():
        groups[_normalize_sql(query.sql_text)].append(query.config_id)
    findings = []
    for config_ids in groups.values():
        if len(config_ids) < 2:
            continue
        ordered = tuple(sorted(config_ids))
        queries = tuple(by_id[config_id] for config_id in ordered)
        destinations = ", ".join(
            f"{query.config_id}->{query.destination.dataset_table if query.destination else 'unknown'}"
            for query in queries
        )
        findings.append(
            make_finding(
                "MN-REPEATED-SQL-" + "-".join(ordered),
                "maintainability",
                "confirmed",
                3,
                "Scheduled queries share the same SQL text",
                queries,
                f"After comment and whitespace removal the SQL matches. Destinations: {destinations}.",
                "A change to one copy will not change the others. Identical text can still write different tables.",
                "Do not delete a copy because the text matches. Check consumers of each destination first.",
            )
        )
    return findings


def _state_counts(inventory: Inventory) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in inventory.history:
        counts[row.config_id][row.raw.get("state", "")] += 1
    return {config_id: dict(states) for config_id, states in counts.items()}


def _normalize_sql(sql: str) -> str:
    text = _COMMENT_BLOCK.sub(" ", sql)
    text = _COMMENT_LINE.sub("", text)
    return _SPACE.sub(" ", text).strip()
