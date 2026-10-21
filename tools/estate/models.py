"""Records produced by the estate loader. Raw inputs stay attached for evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from estate.identifiers import TableId


class Finding(NamedTuple):
    """One inconsistency inside an estate that could still be read.

    Attributes:
        kind: Stable name from ``estate.kinds``.
        message: One-line description for the inventory report.
        config_id: Config the finding cites, or ``None`` when it is about a table.
        evidence: Source values that support the message. Not rewritten.
    """

    kind: str
    message: str
    config_id: str | None
    evidence: dict[str, Any]


class SqlFile(NamedTuple):
    """One file from ``scheduled_queries/``.

    Attributes:
        path: Absolute or estate-relative path as stored by the loader.
        text: File text with original newlines.
        config_id: Id parsed from the filename, or ``None`` when the name
            does not match ``<config_id>__<dataset>__<table>.sql``.
        dataset: Dataset from the filename, or ``None`` when the name did not parse.
        table: Table from the filename, or ``None`` when the name did not parse.
    """

    path: Path
    text: str
    config_id: str | None
    dataset: str | None
    table: str | None


class TransferConfig(NamedTuple):
    """Fields copied from one transfer-config object. ``raw`` is the source object.

    Attributes:
        config_id: Last path segment of ``name``, or ``None`` when ``name``
            is missing or has no segment.
        transfer_name: The ``name`` string, or ``None``.
        display_name: ``displayName``, or ``None`` when blank.
        destination_dataset: ``destinationDatasetId``, or ``None`` when blank.
        destination_table: ``params.destination_table_name_template``.
        schedule: Schedule string, or ``None``.
        timezone: ``scheduleOptionsV2.timeZone``, or ``None``.
        state: ``ENABLED`` or ``DISABLED`` as stored, or ``None``.
        disabled: The ``disabled`` boolean, or ``None`` when it is not a bool.
        write_disposition: ``params.write_disposition``, or ``None``.
        params_query: ``params.query``, or ``None``.
        meta_sql_file: ``_meta_sql_file``, or ``None``.
        raw: The JSON object, unchanged.
    """

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

    Attributes:
        config_id: Shared id.
        sql_path: Path of the SQL file.
        sql_text: File text, unchanged.
        params_query: ``params.query``, or ``None``.
        destination_dataset: Config destination dataset, or ``None``.
        destination_table: Config destination table, or ``None``.
        destination: Parsed table id, or ``None`` when a part is missing or
            the pair is not an identifier.
        schedule: Schedule string, or ``None``.
        timezone: Config timezone, or ``None``.
        state: ``state`` as stored, or ``None``.
        disabled: ``disabled`` when it is a boolean, or ``None``.
        enabled: ``True`` or ``False`` only when ``state`` and ``disabled`` agree.
        write_disposition: ``params.write_disposition``, or ``None``.
        transfer_name: Resource name, or ``None``.
        meta_sql_file: ``_meta_sql_file``, or ``None``.
        filename_dataset: Dataset from the parsed filename.
        filename_table: Table from the parsed filename.
        raw_config: The JSON object, unchanged.
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
    """One ``run_history.csv`` row, in file order.

    Attributes:
        raw: Original cells, including counts kept as strings.
        config_id: ``config_id`` cell.
        destination: Parsed destination, or ``None`` when the cell is not a table id.
        query: Paired query with the same config id, or ``None``.
    """

    raw: dict[str, str]
    config_id: str
    destination: TableId | None
    query: ScheduledQuery | None


class UsageRecord(NamedTuple):
    """One ``downstream_usage.csv`` row, in file order.

    Attributes:
        raw: Original cells.
        table: Parsed table, or ``None`` when the cell is not a table id.
        consumer_kind: ``scheduled_query``, ``dashboard``, or ``adhoc``.
        consumer_id: Consumer name from the file.
        query: Paired query when the consumer is a scheduled query with that
            config id. Other kinds stay ``None``.
        writer_config_ids: Config ids that declare this ``dataset.table``, sorted.
    """

    raw: dict[str, str]
    table: TableId | None
    consumer_kind: str
    consumer_id: str
    query: ScheduledQuery | None
    writer_config_ids: tuple[str, ...]


class CatalogRecord(NamedTuple):
    """One ``table_catalog.csv`` row, in file order.

    Attributes:
        raw: Original cells.
        table: Parsed table, or ``None`` when the cell is not a table id.
        writer_config_ids: Config ids that declare this ``dataset.table``, sorted.
    """

    raw: dict[str, str]
    table: TableId | None
    writer_config_ids: tuple[str, ...]


class SchemaColumn(NamedTuple):
    """One column from ``schemas.json``.

    Attributes:
        name: Column name.
        type_name: BigQuery type string from the file.
        raw: The column object, unchanged.
    """

    name: str
    type_name: str
    raw: dict[str, Any]


class SchemaTable(NamedTuple):
    """One table entry from ``schemas.json``.

    Attributes:
        table: Parsed schema key. A project on the key is kept.
        columns: Columns in file order.
        in_catalog: Whether ``dataset.table`` appears in the catalog.
    """

    table: TableId
    columns: tuple[SchemaColumn, ...]
    in_catalog: bool


class Inventory(NamedTuple):
    """Loaded estate.

    History, usage, and catalog keep source-file order. ``queries`` is sorted
    by config_id. ``schemas`` is sorted by dataset.table. ``findings`` is
    sorted by kind, config_id, then message. Nothing here is written back
    to the source files.

    Attributes:
        root: Estate directory that was loaded.
        sql_files: SQL files in filename order, including names that did not parse.
        transfer_configs: Transfer configs in file order.
        queries: Paired queries, sorted by config id.
        history: Run-history rows in file order.
        usage: Downstream-usage rows in file order.
        catalog: Catalog rows in file order.
        schemas: Schema tables, sorted by ``dataset.table``.
        findings: Source findings, sorted by kind, config id, then message.
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
