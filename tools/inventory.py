"""Print a concise inventory of the scheduled-query estate.

The report is written to stdout. Load failures are logged and produce exit
code 2. Findings inside a readable estate are part of the report, not errors.

Usage:
    python -m inventory ../task2_estate
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from estate import EstateInputError, format_summary, load_inventory

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Load the estate and print its inventory.

    Args:
        argv: Arguments, excluding the program name. Defaults to sys.argv.

    Returns:
        0 when the estate loaded, 2 when a required input is unreadable.
    """
    _configure_logging()
    parser = argparse.ArgumentParser(
        description="Load and validate the scheduled-query estate."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default="task2_estate",
        type=Path,
        help="directory containing scheduled_queries/ and the estate evidence files",
    )
    args = parser.parse_args(argv)
    try:
        inventory = load_inventory(args.root)
    except EstateInputError as exc:
        logger.error("%s", exc)
        return 2
    print(format_summary(inventory), file=sys.stdout)
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
