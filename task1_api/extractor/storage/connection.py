"""PostgreSQL connectivity. No global connection object."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse

import psycopg

from config import CONNECT_TIMEOUT_S, ConfigError, Settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when a database connection or statement fails."""


def connect(settings: Settings) -> psycopg.Connection:
    """Open a new PostgreSQL connection.

    Args:
        settings: Runtime settings. ``pg_dsn`` must be non-empty.

    Returns:
        An open psycopg 3 connection. Close it or use it as a context manager.

    Raises:
        ConfigError: If ``pg_dsn`` is missing or blank.
        StorageError: If the server cannot be reached.
    """
    dsn = _require_dsn(settings)
    context = safe_dsn_context(dsn)
    logger.info("Connecting to PostgreSQL %s", context)
    try:
        return psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT_S)
    except psycopg.Error as exc:
        logger.error(
            "Failed to connect to PostgreSQL %s: %s", context, type(exc).__name__
        )
        raise StorageError(f"failed to connect to PostgreSQL ({context})") from exc


@contextmanager
def connection(settings: Settings) -> Iterator[psycopg.Connection]:
    """Yield a PostgreSQL connection and always close it.

    Args:
        settings: Runtime settings including ``pg_dsn``.

    Yields:
        An open psycopg 3 connection.
    """
    conn = connect(settings)
    try:
        yield conn
    finally:
        conn.close()


def safe_dsn_context(dsn: str) -> str:
    """Return host metadata for logs, without password or the full DSN."""
    parsed = urlparse(dsn)
    host = parsed.hostname or "?"
    port = parsed.port or 5432
    dbname = (parsed.path or "").lstrip("/") or "?"
    user = parsed.username or "?"
    return f"user={user} host={host} port={port} dbname={dbname}"


def _require_dsn(settings: Settings) -> str:
    dsn = (settings.pg_dsn or "").strip()
    if not dsn:
        raise ConfigError("missing PostgreSQL DSN (PG_DSN)")
    return dsn
