"""Write COST.txt for the scheduled-query estate.

Usage:
    python -m cost_report
    python -m cost_report ../task2_estate --output ../COST.txt

The file is rendered from ``calculate_estate`` and the scenario calculations.
Numbers are not supplied on the command line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from audit import collect_findings
from cost import calculate_estate
from cost.render import render_cost
from cost.scenarios import build_cost_analysis
from estate import EstateInputError, load_inventory
from graph import build_graph
from graph.analyze import REVENUE_DAILY_TABLE
from sql import parse_inventory

_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "task2_estate"
_DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "COST.txt"


def build_report(root: Path, table_name: str = REVENUE_DAILY_TABLE) -> str:
    """Load the estate, price it, and return the cost report.

    Args:
        root: Directory containing the estate evidence.
        table_name: Exact table name of the lineage route.

    Returns:
        The report, including a trailing newline.

    Raises:
        EstateInputError: A required input is missing or structurally invalid.
    """
    inventory = load_inventory(root)
    parses = parse_inventory(inventory)
    graph = build_graph(inventory, parses)
    findings = collect_findings(inventory, parses, graph)
    estate = calculate_estate(inventory)
    analysis = build_cost_analysis(
        inventory,
        estate,
        graph,
        findings,
        source_name=root.name,
        table_name=table_name,
    )
    return render_cost(analysis)


def main(argv: list[str] | None = None) -> int:
    """Write the cost report.

    Args:
        argv: Arguments, excluding the program name. Defaults to sys.argv.

    Returns:
        0 when the file is written. 2 when a required input cannot be read.
    """
    parser = argparse.ArgumentParser(
        description="Generate COST.txt from the estate evidence and the cost engine."
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=_DEFAULT_ROOT,
        help="estate directory",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="report path (default: COST.txt at the repository root)",
    )
    parser.add_argument(
        "--table",
        default=REVENUE_DAILY_TABLE,
        help="exact table name of the lineage route (default: revenue_daily)",
    )
    args = parser.parse_args(argv)
    try:
        report = build_report(args.root, args.table)
    except EstateInputError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    args.output.write_text(report, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
