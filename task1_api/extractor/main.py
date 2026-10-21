"""One-shot partner report extractor.

Run with: docker compose run --rm extractor
"""

from __future__ import annotations

import logging
import os
import sys

from config import ConfigError, Settings
from pipeline import STATUS_FAILED, ExtractResult, run_extraction
from window import WindowError

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Configure process-wide logging from ``LOG_LEVEL``.

    Unknown level names fall back to ``INFO``. The format is time, level,
    logger name, and message. Credentials are not attached to the logger.
    """
    raw = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = getattr(logging, raw, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def exit_code(result: ExtractResult) -> int:
    """Return the process code for one extraction result.

    Args:
        result: Outcome from :func:`run_extraction`.

    Returns:
        ``1`` when ``status`` is ``failed``. ``success`` and ``skipped``
        both return ``0``.
    """
    return 1 if result.status == STATUS_FAILED else 0


def main() -> int:
    """Load settings, run one extraction, and return a process exit code.

    Configuration and window errors are logged and become exit code ``1``.
    Any other unexpected exception is logged with a traceback and also
    returns ``1``. Expected extraction failures stay inside
    :class:`ExtractResult` and use :func:`exit_code`.

    Returns:
        ``0`` on success or a lock-busy skip, ``1`` on failure.
    """
    configure_logging()
    try:
        settings = Settings.from_env()
        result = run_extraction(settings)
    except (ConfigError, WindowError) as exc:
        logger.error("Extractor configuration failed: %s", exc)
        return 1
    except Exception:
        logger.exception("Extractor crashed")
        return 1
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
