"""Records produced by the estate loader. Raw inputs stay attached for evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from estate.identifiers import TableId


class Finding(NamedTuple):
    kind: str
    message: str
    config_id: str | None
    evidence: dict[str, Any]


class SqlFile(NamedTuple):
    path: Path
    text: str
    config_id: str | None
    dataset: str | None
    table: str | None


class TransferConfig(NamedTuple):
    config_id: str | None
    transfer_name: str | None
    display_name: str | None
    destination_dataset: str | None
    destination_table: str | None
    schedule: str | None
    timezone: str | None
    state: str | None
    disabled: bool | None
    write_disposition: str | None
    params_query: str | None
    meta_sql_file: str | None
    raw: dict[str, Any]


class ScheduledQuery(NamedTuple):
    """One config_id paired with exactly one SQL file and one transfer config.

    ``state`` and ``disabled`` are the source values. ``enabled`` is derived
    and stays None when those two fields disagree. The raw config is the
    object parsed from JSON, not a rewritten copy.
    """

    config_id: str
    sql_path: Path
    sql_text: str
    params_query: str | None
    destination_dataset: str | None
    destination_table: str | None
    destination: TableId | None
    schedule: str | None
    timezone: str | None
    state: str | None
    disabled: bool | None
    enabled: bool | None
    write_disposition: str | None
    transfer_name: str | None
    meta_sql_file: str | None
    filename_dataset: str | None
    filename_table: str | None
    raw_config: dict[str, Any]


class HistoryRecord(NamedTuple):
    raw: dict[str, str]
    config_id: str
    destination: TableId | None
    query: ScheduledQuery | None


class UsageRecord(NamedTuple):
    raw: dict[str, str]
    table: TableId | None
    consumer_kind: str
    consumer_id: str
    query: ScheduledQuery | None
    writer_config_ids: tuple[str, ...]


class CatalogRecord(NamedTuple):
    raw: dict[str, str]
    table: TableId | None
    writer_config_ids: tuple[str, ...]


class SchemaColumn(NamedTuple):
    name: str
    type_name: str
    raw: dict[str, Any]


class SchemaTable(NamedTuple):
    table: TableId
    columns: tuple[SchemaColumn, ...]
    in_catalog: bool


class Inventory(NamedTuple):
    """Loaded estate.

    History, usage, and catalog keep source-file order. ``queries`` is sorted
    by config_id. ``schemas`` is sorted by dataset.table. ``findings`` is
    sorted by kind, config_id, then message. Nothing here is written back
    to the source files.
    """

    root: Path
    sql_files: tuple[SqlFile, ...]
    transfer_configs: tuple[TransferConfig, ...]
    queries: tuple[ScheduledQuery, ...]
    history: tuple[HistoryRecord, ...]
    usage: tuple[UsageRecord, ...]
    catalog: tuple[CatalogRecord, ...]
    schemas: tuple[SchemaTable, ...]
    findings: tuple[Finding, ...]
