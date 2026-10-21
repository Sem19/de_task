"""Orchestrate estate loading.

File IO, per-file parsing, and cross-file checks live in io, sources, and
relate. This module only orders those steps. It does not parse SQL or build
a dependency graph.
"""

from __future__ import annotations

from pathlib import Path

from estate.models import Finding, Inventory
from estate.relate import (
    history_findings,
    pair_queries,
    schema_findings,
    usage_findings,
    writers_by_table,
)
from estate.sources import (
    load_catalog,
    load_history,
    load_schemas,
    load_sql_files,
    load_transfer_configs,
    load_usage,
)
from estate.summary import format_summary

__all__ = ["format_summary", "load_inventory"]


def load_inventory(root: Path) -> Inventory:
    """Load estate inputs and relate them by config_id and dataset.table.

    Missing files, undecodable text, and malformed JSON or CSV raise
    EstateInputError. Disagreements inside a readable estate, such as a SQL
    file that differs from params.query, are findings. Source files are not
    modified.

    History, usage, and catalog rows keep the order of their source files.
    Queries are ordered by config_id. Findings are ordered by kind, config_id,
    then message.

    Args:
        root: Directory containing ``scheduled_queries/`` and the evidence files.

    Returns:
        The loaded inventory.

    Raises:
        EstateInputError: A required input is missing or structurally invalid.
    """
    root = Path(root)
    sql_files, sql_findings = load_sql_files(root / "scheduled_queries")
    configs, config_findings = load_transfer_configs(root / "transfer_configs.json")
    queries, map_findings = pair_queries(root, sql_files, configs)
    by_id = {query.config_id: query for query in queries}
    writers = writers_by_table(queries)

    history = load_history(root / "run_history.csv", by_id)
    usage = load_usage(root / "downstream_usage.csv", by_id, writers)
    catalog, catalog_findings = load_catalog(root / "table_catalog.csv", writers)
    schemas, schema_load_findings = load_schemas(root / "schemas.json", catalog)

    findings = (
        *sql_findings,
        *config_findings,
        *map_findings,
        *catalog_findings,
        *schema_load_findings,
        *history_findings(history, catalog),
        *usage_findings(usage, catalog),
        *schema_findings(schemas),
    )
    return Inventory(
        root=root,
        sql_files=tuple(sql_files),
        transfer_configs=tuple(configs),
        queries=tuple(queries),
        history=tuple(history),
        usage=tuple(usage),
        catalog=tuple(catalog),
        schemas=tuple(schemas),
        findings=tuple(sorted(findings, key=_finding_order)),
    )


def _finding_order(finding: Finding) -> tuple[str, str, str]:
    return (finding.kind, finding.config_id or "", finding.message)
