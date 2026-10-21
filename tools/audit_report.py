"""Write AUDIT.txt for the scheduled-query estate.

Usage:
    python -m audit_report
    python -m audit_report ../task2_estate --output ../AUDIT.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from audit import collect_findings, render_audit
from estate import EstateInputError, load_inventory
from graph import build_graph
from sql import parse_inventory

_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "task2_estate"
_DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "AUDIT.txt"


def build_report(root: Path) -> str:
    """Load the estate and return the audit text.

    Args:
        root: Directory containing the estate evidence.

    Returns:
        The report, including a trailing newline.

    Raises:
        EstateInputError: A required input is missing or structurally invalid.
    """
    inventory = load_inventory(root)
    parses = parse_inventory(inventory)
    graph = build_graph(inventory, parses)
    findings = collect_findings(inventory, parses, graph)
    return render_audit(inventory, parses, graph, findings)


def main(argv: list[str] | None = None) -> int:
    """Write the audit report.

    Args:
        argv: Arguments, excluding the program name. Defaults to sys.argv.

    Returns:
        0 when the file is written. 2 when a required input cannot be read.
    """
    parser = argparse.ArgumentParser(
        description="Generate AUDIT.txt from the estate evidence."
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
        help="report path (default: AUDIT.txt at the repository root)",
    )
    args = parser.parse_args(argv)
    try:
        report = build_report(args.root)
    except EstateInputError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    args.output.write_text(report, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
