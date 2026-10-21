"""Session-level PostgreSQL advisory lock for one extractor run.

The lock is acquired on a dedicated connection with autocommit enabled so
commits on the load connection cannot release it. It is held until
:meth:`ExtractionLock.release` or process disconnect.
"""

from __future__ import annotations

import logging

import psycopg

from config import EXTRACT_LOCK_CLASS, EXTRACT_LOCK_ID, Settings

from .connection import connect

logger = logging.getLogger(__name__)


class ExtractionLock:
    """Held session advisory lock. Close to release."""

    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        lock_class: int,
        lock_id: int,
    ) -> None:
        self._conn = conn
        self._lock_class = lock_class
        self._lock_id = lock_id
        self._released = False

    def release(self) -> None:
        """Unlock and close the dedicated connection."""
        if self._released:
            return
        self._released = True
        try:
            self._conn.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (self._lock_class, self._lock_id),
            )
        finally:
            self._conn.close()
            logger.info(
                "Released extractor advisory lock class=%s id=%s",
                self._lock_class,
                self._lock_id,
            )

    def __enter__(self) -> ExtractionLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def acquire_extraction_lock(
    settings: Settings,
    *,
    lock_class: int = EXTRACT_LOCK_CLASS,
    lock_id: int = EXTRACT_LOCK_ID,
) -> ExtractionLock | None:
    """Try to acquire the extractor lock on a dedicated connection.

    Args:
        settings: Runtime settings including ``pg_dsn``.
        lock_class: First advisory-lock key.
        lock_id: Second advisory-lock key.

    Returns:
        A held lock, or ``None`` if another extractor already holds it.
    """
    conn = connect(settings)
    conn.autocommit = True
    try:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            (lock_class, lock_id),
        ).fetchone()
        acquired = bool(row and row[0])
    except Exception:
        conn.close()
        raise
    if not acquired:
        conn.close()
        logger.info(
            "extract_skipped extractor advisory lock is held class=%s id=%s",
            lock_class,
            lock_id,
        )
        return None
    logger.info(
        "Acquired extractor advisory lock class=%s id=%s",
        lock_class,
        lock_id,
    )
    return ExtractionLock(conn, lock_class=lock_class, lock_id=lock_id)
