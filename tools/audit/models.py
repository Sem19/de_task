"""Findings produced by the estate audit.

A finding records an observation and a recommendation separately. ``confirmed``
means the supplied files show the fact. ``indicated`` means the files support
the reading but do not prove the published data. ``needs_verification`` means
the next step requires BigQuery or an owner.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

Category = Literal["operational", "correctness", "maintainability"]
Confidence = Literal["confirmed", "indicated", "needs_verification"]


class AuditFinding(NamedTuple):
    """One traceable audit item.

    ``finding_id`` is stable for the same evidence. ``config_ids`` and
    ``sql_paths`` are sorted. ``priority`` 1 is the highest.
    """

    finding_id: str
    category: Category
    confidence: Confidence
    priority: int
    title: str
    config_ids: tuple[str, ...]
    sql_paths: tuple[str, ...]
    evidence: str
    impact: str
    recommendation: str
