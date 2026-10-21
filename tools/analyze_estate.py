"""Run the scheduled-query estate analyzer.

Load, parse, and graph the estate, then print a deterministic report. Known
defects in a readable estate are findings in that report, not a failed run.
Missing or invalid inputs are logged and return exit code 2.

The default root is the supplied ``task2_estate`` directory. A table argument
selects the lineage to print. It does not change which writers are kept.

Usage:
    python -m analyze_estate
    python -m analyze_estate ../task2_estate --table reports.revenue_daily
    python -m analyze_estate --output analysis.txt
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from estate import EstateInputError, load_inventory
from graph import build_graph
from report import format_report
from sql import parse_inventory

logger = logging.getLogger(__name__)
_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "task2_estate"


def analyze(root: Path, *, target: str = "revenue_daily") -> str:
    """Load one estate and return its analysis report.

    Args:
        root: Directory containing ``scheduled_queries/`` and the evidence files.
        target: Table name or qualified id whose lineage is included.

    Returns:
        The report text, without a trailing newline.

    Raises:
        EstateInputError: A required input is missing or structurally invalid.
    """
    inventory = load_inventory(root)
    parses = parse_inventory(inventory)
    graph = build_graph(inventory, parses)
    return format_report(inventory, parses, graph, target=target)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, analyze the estate, and write the report.

    Args:
        argv: Arguments, excluding the program name. Defaults to sys.argv.

    Returns:
        0 when the report is written. 2 when a required input cannot be read.
        Invalid arguments exit through argparse with status 2.
    """
    _configure_logging()
    parser = argparse.ArgumentParser(
        description="Analyze the scheduled-query estate. Findings stay in the report."
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=_DEFAULT_ROOT,
        help="estate directory (default: the supplied task2_estate)",
    )
    parser.add_argument(
        "--table",
        default="revenue_daily",
        help="table name or dataset.table whose lineage is printed",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="also write the report to this file",
    )
    args = parser.parse_args(argv)
    try:
        report = analyze(args.root, target=args.table)
    except EstateInputError as exc:
        logger.error("%s", exc)
        return 2
    text = report + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


def _configure_logging() -> None:
    """Send error logs to stderr unless the process already configured logging."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s"
    )


if __name__ == "__main__":
    raise SystemExit(main())
