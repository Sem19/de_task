"""Turn each evidence file into records.

This module understands one file at a time. Cross-file disagreements are
findings produced by relate.py. A blank string is treated as missing so it
shows up as incomplete metadata instead of an empty identifier.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from tools.estate import kinds
from tools.estate.errors import EstateInputError
from tools.estate.identifiers import (
    config_id_from_transfer_name,
    parse_sql_filename,
    parse_table_id,
    try_parse_table_id,
)
from tools.estate.io import load_csv, load_json_object, read_text
from tools.estate.models import (
    CatalogRecord,
    Finding,
    HistoryRecord,
    ScheduledQuery,
    SchemaColumn,
    SchemaTable,
    SqlFile,
    TransferConfig,
    UsageRecord,
)

HISTORY_COLUMNS = (
    "run_id",
    "config_id",
    "destination",
    "schedule_time",
    "start_time",
    "end_time",
    "state",
    "error_message",
    "total_bytes_processed",
    "rows_written",
)
USAGE_COLUMNS = ("table", "consumer_kind", "consumer_id", "queries_30d")
CATALOG_COLUMNS = (
    "table",
    "type",
    "row_count",
    "size_bytes",
    "partition_column",
    "clustering",
    "created",
    "last_modified",
    "columns",
)

SCHEDULED_QUERY = "scheduled_query"

_CONFIG_FIELDS = (
    "name",
    "destinationDatasetId",
    "destination_table_name_template",
    "schedule",
    "timeZone",
    "state",
    "disabled",
    "write_disposition",
    "query",
)


def load_sql_files(directory: Path) -> tuple[list[SqlFile], list[Finding]]:
    """Load ``*.sql`` files directly inside ``directory``, ordered by filename.

    Args:
        directory: ``scheduled_queries`` directory.

    Returns:
        Parsed files and a finding for every filename that does not match
        ``<config_id>__<dataset>__<table>.sql``.

    Raises:
        EstateInputError: The directory is missing, or a file is not UTF-8.
    """
    if not directory.is_dir():
        raise EstateInputError(f"missing directory: {directory}")
    files: list[SqlFile] = []
    findings: list[Finding] = []
    paths = sorted(directory.glob("*.sql"), key=lambda path: path.name)
    for path in paths:
        text = read_text(path)
        try:
            parsed = parse_sql_filename(path.name)
        except ValueError as exc:
            files.append(SqlFile(path, text, None, None, None))
            findings.append(
                Finding(
                    kinds.UNPARSED_SQL_FILENAME, str(exc), None, {"sql_path": str(path)}
                )
            )
            continue
        files.append(
            SqlFile(path, text, parsed.config_id, parsed.dataset, parsed.table)
        )
    return files, findings


def load_transfer_configs(path: Path) -> tuple[list[TransferConfig], list[Finding]]:
    """Load ``transferConfigs`` in file order.

    Args:
        path: ``transfer_configs.json``.

    Returns:
        Config records and one incomplete_config finding per config that lacks
        a field required to map it.

    Raises:
        EstateInputError: The file is unreadable, or ``transferConfigs`` is not
            a list of objects.
    """
    payload = load_json_object(path)
    raw_configs = payload.get("transferConfigs")
    if not isinstance(raw_configs, list):
        raise EstateInputError(f"{path}: transferConfigs must be a list")
    configs: list[TransferConfig] = []
    findings: list[Finding] = []
    for index, raw in enumerate(raw_configs):
        if not isinstance(raw, dict):
            raise EstateInputError(
                f"{path}: transferConfigs[{index}] must be an object"
            )
        config = transfer_config(raw)
        configs.append(config)
        missing = incomplete_fields(config)
        if missing:
            findings.append(
                Finding(
                    kinds.INCOMPLETE_CONFIG,
                    f"{config.config_id or index}: missing {', '.join(missing)}",
                    config.config_id,
                    {"fields": missing, "transfer_name": config.transfer_name},
                )
            )
    return configs, findings


def transfer_config(raw: dict[str, Any]) -> TransferConfig:
    """Copy mapping fields off one transfer-config object. ``raw`` is kept as-is."""
    name = raw.get("name")
    transfer_name = name if isinstance(name, str) else None
    config_id: str | None = None
    if isinstance(name, str):
        try:
            config_id = config_id_from_transfer_name(name)
        except ValueError:
            config_id = None
    params = raw.get("params")
    params = params if isinstance(params, dict) else {}
    options = raw.get("scheduleOptionsV2")
    options = options if isinstance(options, dict) else {}
    disabled = raw.get("disabled")
    return TransferConfig(
        config_id=config_id,
        transfer_name=transfer_name,
        display_name=_text(raw.get("displayName")),
        destination_dataset=_text(raw.get("destinationDatasetId")),
        destination_table=_text(params.get("destination_table_name_template")),
        schedule=_text(raw.get("schedule")),
        timezone=_text(options.get("timeZone")),
        state=_text(raw.get("state")),
        disabled=disabled if isinstance(disabled, bool) else None,
        write_disposition=_text(params.get("write_disposition")),
        params_query=_text(params.get("query")),
        meta_sql_file=_text(raw.get("_meta_sql_file")),
        raw=raw,
    )


def incomplete_fields(config: TransferConfig) -> list[str]:
    """Return the mapping fields that are absent, in a stable order."""
    values = {
        "name": config.config_id,
        "destinationDatasetId": config.destination_dataset,
        "destination_table_name_template": config.destination_table,
        "schedule": config.schedule,
        "timeZone": config.timezone,
        "state": config.state,
        "disabled": config.disabled,
        "write_disposition": config.write_disposition,
        "query": config.params_query,
    }
    return [field for field in _CONFIG_FIELDS if values[field] is None]


def load_history(path: Path, by_id: dict[str, ScheduledQuery]) -> list[HistoryRecord]:
    """Load run history in file order and attach a mapped query when the id matches.

    Args:
        path: ``run_history.csv``.
        by_id: Queries keyed by config_id. Unknown ids stay unlinked.

    Returns:
        History records. Byte and row counts remain strings.
    """
    rows: list[HistoryRecord] = []
    for raw in load_csv(path, HISTORY_COLUMNS):
        config_id = raw["config_id"]
        rows.append(
            HistoryRecord(
                raw=raw,
                config_id=config_id,
                destination=try_parse_table_id(raw["destination"]),
                query=by_id.get(config_id),
            )
        )
    return rows


def load_usage(
    path: Path,
    by_id: dict[str, ScheduledQuery],
    writers: dict[str, tuple[str, ...]],
) -> list[UsageRecord]:
    """Load downstream usage in file order.

    A scheduled_query consumer is linked by consumer_id. Dashboard and ad-hoc
    rows are linked only through the table and its writer config ids.

    Args:
        path: ``downstream_usage.csv``.
        by_id: Queries keyed by config_id.
        writers: Writer config ids keyed by ``dataset.table``.
    """
    rows: list[UsageRecord] = []
    for raw in load_csv(path, USAGE_COLUMNS):
        table = try_parse_table_id(raw["table"])
        kind = raw["consumer_kind"]
        consumer_id = raw["consumer_id"]
        query = by_id.get(consumer_id) if kind == SCHEDULED_QUERY else None
        writer_ids = writers.get(table.dataset_table, ()) if table is not None else ()
        rows.append(UsageRecord(raw, table, kind, consumer_id, query, writer_ids))
    return rows


def load_catalog(
    path: Path,
    writers: dict[str, tuple[str, ...]],
) -> tuple[list[CatalogRecord], list[Finding]]:
    """Load the table catalog in file order.

    Args:
        path: ``table_catalog.csv``.
        writers: Writer config ids keyed by ``dataset.table``.

    Returns:
        Catalog records and a finding for every table cell that is not an identifier.
    """
    records: list[CatalogRecord] = []
    unparsed: dict[str, int] = defaultdict(int)
    for raw in load_csv(path, CATALOG_COLUMNS):
        table = try_parse_table_id(raw["table"])
        if table is None:
            unparsed[raw["table"]] += 1
        writer_ids = writers.get(table.dataset_table, ()) if table is not None else ()
        records.append(CatalogRecord(raw, table, writer_ids))
    findings = [
        Finding(
            kinds.UNPARSED_TABLE_ID,
            f"table_catalog table {table!r} is not a table identifier ({kinds.count_label(count, 'row')})",
            None,
            {"table": table, "row_count": count},
        )
        for table, count in sorted(unparsed.items())
    ]
    return records, findings


def load_schemas(
    path: Path, catalog: list[CatalogRecord]
) -> tuple[list[SchemaTable], list[Finding]]:
    """Load ``schemas.json``. Tables are ordered by ``dataset.table``.

    A schema key joins to the catalog on ``dataset.table``. A project on the
    key is kept and does not by itself make the table miss the catalog.

    Args:
        path: ``schemas.json``.
        catalog: Catalog records already loaded.

    Returns:
        Schema tables and a finding for every key that is not a table id.

    Raises:
        EstateInputError: A schema entry is not a list of columns with string
            name and type.
    """
    payload = load_json_object(path)
    catalog_tables = {
        record.table.dataset_table for record in catalog if record.table is not None
    }
    tables: list[SchemaTable] = []
    findings: list[Finding] = []
    for key, columns in payload.items():
        try:
            table = parse_table_id(key)
        except ValueError as exc:
            findings.append(
                Finding(kinds.UNPARSED_TABLE_ID, str(exc), None, {"schema_key": key})
            )
            continue
        parsed_columns = _schema_columns(path, key, columns)
        tables.append(
            SchemaTable(table, parsed_columns, table.dataset_table in catalog_tables)
        )
    tables.sort(key=lambda item: item.table.dataset_table)
    return tables, findings


def _schema_columns(path: Path, key: str, columns: object) -> tuple[SchemaColumn, ...]:
    if not isinstance(columns, list):
        raise EstateInputError(f"{path}: schema for {key} must be a list")
    parsed: list[SchemaColumn] = []
    for index, column in enumerate(columns):
        if (
            not isinstance(column, dict)
            or not isinstance(column.get("name"), str)
            or not isinstance(column.get("type"), str)
        ):
            raise EstateInputError(
                f"{path}: schema for {key} column {index} must have string name and type"
            )
        parsed.append(SchemaColumn(column["name"], column["type"], column))
    return tuple(parsed)


def _text(value: object) -> str | None:
    if isinstance(value, str) and value != "":
        return value
    return None
