"""Format an inventory for the command-line report.

Findings are listed after the counts. The report does not include SQL text.
"""

from __future__ import annotations

from collections import defaultdict

from estate.models import Inventory


def format_summary(inventory: Inventory) -> str:
    """Return a short text inventory.

    Findings are counted, then listed one per line. SQL bodies are not included.

    Args:
        inventory: A loaded estate.

    Returns:
        The report, without a trailing newline.
    """
    enabled = sum(1 for query in inventory.queries if query.enabled is True)
    disabled = sum(1 for query in inventory.queries if query.enabled is False)
    history_linked = sum(1 for row in inventory.history if row.query is not None)
    usage_with_table = sum(1 for row in inventory.usage if row.table is not None)
    usage_linked = sum(1 for row in inventory.usage if row.query is not None)
    lines = [
        f"root: {inventory.root}",
        f"sql_files: {len(inventory.sql_files)}",
        f"transfer_configs: {len(inventory.transfer_configs)}",
        f"mapped_configs: {len(inventory.queries)}",
        f"enabled: {enabled}",
        f"disabled: {disabled}",
        f"run_history_rows: {len(inventory.history)}",
        f"history_rows_linked: {history_linked}",
        f"downstream_usage_rows: {len(inventory.usage)}",
        f"usage_rows_with_table: {usage_with_table}",
        f"usage_scheduled_query_rows_linked: {usage_linked}",
        f"table_catalog_rows: {len(inventory.catalog)}",
        f"schema_tables: {len(inventory.schemas)}",
        f"findings: {len(inventory.findings)}",
    ]
    counts: dict[str, int] = defaultdict(int)
    for finding in inventory.findings:
        counts[finding.kind] += 1
    for kind in sorted(counts):
        lines.append(f"finding.{kind}: {counts[kind]}")
    if inventory.findings:
        lines.append("mappings:")
        for finding in inventory.findings:
            lines.append(f"  {finding.kind}: {finding.message}")
    else:
        lines.append("mappings: consistent")
    return "\n".join(lines)
