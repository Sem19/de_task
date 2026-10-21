"""Relate SQL files, transfer configs, runs, usage, and the catalog.

Relationships use config_id and ``dataset.table``. A project on one side of a
join does not prevent a match. Nothing in this module rewrites source fields.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from estate import kinds
from estate.identifiers import TableId, try_parse_table_id
from estate.models import (
    CatalogRecord,
    Finding,
    HistoryRecord,
    ScheduledQuery,
    SchemaTable,
    SqlFile,
    TransferConfig,
    UsageRecord,
)
from estate.sources import SCHEDULED_QUERY


def pair_queries(
    root: Path,
    sql_files: list[SqlFile],
    configs: list[TransferConfig],
) -> tuple[list[ScheduledQuery], list[Finding]]:
    """Pair each config_id that has exactly one SQL file and one transfer config.

    Duplicate ids are reported and left unpaired rather than choosing a file.
    Queries are returned sorted by config_id.

    Args:
        root: Estate root, used to resolve ``_meta_sql_file``.
        sql_files: SQL files, including any whose names did not parse.
        configs: Transfer configs in file order.

    Returns:
        Paired queries and the mapping findings.
    """
    sql_by_id: dict[str, list[SqlFile]] = defaultdict(list)
    for sql_file in sql_files:
        if sql_file.config_id is not None:
            sql_by_id[sql_file.config_id].append(sql_file)

    config_by_id: dict[str, list[TransferConfig]] = defaultdict(list)
    findings: list[Finding] = []
    for config in configs:
        if config.config_id is None:
            findings.append(
                Finding(
                    kinds.MISSING_CONFIG_ID,
                    f"transfer config has no config_id: {config.transfer_name!r}",
                    None,
                    {"transfer_name": config.transfer_name},
                )
            )
            continue
        config_by_id[config.config_id].append(config)

    queries: list[ScheduledQuery] = []
    for config_id in sorted(set(sql_by_id) | set(config_by_id)):
        sql_group = sql_by_id.get(config_id, [])
        config_group = config_by_id.get(config_id, [])
        findings.extend(_pairing_findings(config_id, sql_group, config_group))
        if len(sql_group) == 1 and len(config_group) == 1:
            queries.append(
                _scheduled_query(
                    root, config_id, sql_group[0], config_group[0], findings
                )
            )
    queries.sort(key=lambda query: query.config_id)
    findings.extend(_shared_destinations(queries))
    return queries, findings


def writers_by_table(queries: list[ScheduledQuery]) -> dict[str, tuple[str, ...]]:
    """Map ``dataset.table`` to the config ids that declare it.

    Args:
        queries: Paired queries. A query with no destination is omitted.

    Returns:
        Destination to config ids sorted by id.
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    for query in queries:
        if query.destination is not None:
            grouped[query.destination.dataset_table].append(query.config_id)
    return {table: tuple(sorted(config_ids)) for table, config_ids in grouped.items()}


def history_findings(
    history: list[HistoryRecord], catalog: list[CatalogRecord]
) -> list[Finding]:
    """Report history rows whose config or destination does not line up.

    Rows keep their original destination string in ``raw``. Comparison with
    the config uses ``dataset.table``.

    Args:
        history: History rows in file order.
        catalog: Catalog rows. Only parsed tables count as present.

    Returns:
        Findings for unparsed destinations, destinations absent from the
        catalog, unknown config ids, and destinations that differ from the
        paired query.
    """
    catalog_tables = _catalog_tables(catalog)
    unknown: dict[str, list[HistoryRecord]] = defaultdict(list)
    mismatched: dict[str, list[HistoryRecord]] = defaultdict(list)
    absent: dict[str, int] = defaultdict(int)
    unparsed: dict[str, int] = defaultdict(int)
    for row in history:
        if row.destination is None:
            unparsed[row.raw["destination"]] += 1
            continue
        if row.destination.dataset_table not in catalog_tables:
            absent[row.raw["destination"]] += 1
        if row.query is None:
            unknown[row.config_id].append(row)
            continue
        expected = row.query.destination
        if (
            expected is not None
            and row.destination.dataset_table != expected.dataset_table
        ):
            mismatched[row.config_id].append(row)
    return [
        *_unparsed_destinations("run_history destination", unparsed),
        *_absent_history_tables(absent),
        *_unknown_history(unknown),
        *_mismatched_history(mismatched),
    ]


def usage_findings(
    usage: list[UsageRecord], catalog: list[CatalogRecord]
) -> list[Finding]:
    """Report usage rows that cite an unknown query or a table outside the catalog.

    Args:
        usage: Usage rows in file order.
        catalog: Catalog rows. Only parsed tables count as present.

    Returns:
        Findings for unparsed table cells, unmapped scheduled-query consumers,
        and tables absent from the catalog.
    """
    catalog_tables = _catalog_tables(catalog)
    unknown: dict[str, int] = defaultdict(int)
    missing_tables: dict[str, list[UsageRecord]] = defaultdict(list)
    unparsed: dict[str, int] = defaultdict(int)
    for row in usage:
        if row.table is None:
            unparsed[row.raw["table"]] += 1
            continue
        if row.consumer_kind == SCHEDULED_QUERY and row.query is None:
            unknown[row.consumer_id] += 1
        if row.table.dataset_table not in catalog_tables:
            missing_tables[row.raw["table"]].append(row)
    findings = _unparsed_tables("downstream_usage table", unparsed)
    for consumer_id, count in sorted(unknown.items()):
        findings.append(
            Finding(
                kinds.USAGE_UNKNOWN_CONFIG,
                (
                    f"{consumer_id}: {kinds.count_label(count, 'downstream-usage row')} "
                    "with an unmapped scheduled query"
                ),
                consumer_id,
                {"row_count": count},
            )
        )
    for table, rows in sorted(missing_tables.items()):
        findings.append(
            Finding(
                kinds.USAGE_TABLE_NOT_IN_CATALOG,
                (
                    f"{table} is read in downstream_usage and is absent from the table catalog "
                    f"({kinds.count_label(len(rows), 'row')})"
                ),
                None,
                {"table": table, "rows": [dict(row.raw) for row in rows]},
            )
        )
    return findings


def schema_findings(schemas: list[SchemaTable]) -> list[Finding]:
    """Report schema entries whose ``dataset.table`` is not in the catalog.

    Args:
        schemas: Schema tables. ``in_catalog`` is already computed.

    Returns:
        One finding per schema table that missed the catalog, in input order.
    """
    return [
        Finding(
            kinds.SCHEMA_NOT_IN_CATALOG,
            f"{table.table.dataset_table} is in schemas.json and absent from the table catalog",
            None,
            {"table": table.table.dataset_table},
        )
        for table in schemas
        if not table.in_catalog
    ]


def _pairing_findings(
    config_id: str,
    sql_group: list[SqlFile],
    config_group: list[TransferConfig],
) -> list[Finding]:
    """Report a config id that is duplicated or present on only one side.

    Args:
        config_id: Id from a filename, a transfer config, or both.
        sql_group: SQL files with that id.
        config_group: Transfer configs with that id.

    Returns:
        Duplicate, missing-SQL, and missing-config findings. An id with one
        file and one config returns an empty list.
    """
    findings: list[Finding] = []
    if len(sql_group) > 1 or len(config_group) > 1:
        findings.append(
            Finding(
                kinds.DUPLICATE_CONFIG_ID,
                f"{config_id}: {len(sql_group)} SQL files and {len(config_group)} transfer configs",
                config_id,
                {
                    "sql_paths": [str(item.path) for item in sql_group],
                    "transfer_names": [item.transfer_name for item in config_group],
                },
            )
        )
    if not sql_group:
        findings.append(
            Finding(
                kinds.MISSING_SQL,
                f"{config_id}: transfer config has no SQL file",
                config_id,
                {"transfer_names": [item.transfer_name for item in config_group]},
            )
        )
    if not config_group:
        findings.append(
            Finding(
                kinds.MISSING_CONFIG,
                f"{config_id}: SQL file has no transfer config",
                config_id,
                {"sql_paths": [str(item.path) for item in sql_group]},
            )
        )
    return findings


def _scheduled_query(
    root: Path,
    config_id: str,
    sql_file: SqlFile,
    config: TransferConfig,
    findings: list[Finding],
) -> ScheduledQuery:
    """Build one paired query and append findings for disagreements.

    SQL is compared exactly, including comments and whitespace. Neither side
    is reformatted. ``enabled`` is derived and the source state fields are kept.

    Args:
        root: Estate root, used to resolve ``_meta_sql_file``.
        config_id: Shared id.
        sql_file: The one SQL file.
        config: The one transfer config.
        findings: Mutable list. Disagreements are appended.

    Returns:
        The paired query. Findings are not returned separately.
    """
    destination = _destination(config)
    for finding in (
        _sql_text_finding(config_id, sql_file, config),
        _destination_name_finding(config_id, sql_file, config),
        _unparsed_destination_finding(config_id, config, destination),
        _meta_file_finding(root, config_id, sql_file, config),
        _enabled_finding(config_id, config),
    ):
        if finding is not None:
            findings.append(finding)
    return ScheduledQuery(
        config_id=config_id,
        sql_path=sql_file.path,
        sql_text=sql_file.text,
        params_query=config.params_query,
        destination_dataset=config.destination_dataset,
        destination_table=config.destination_table,
        destination=destination,
        schedule=config.schedule,
        timezone=config.timezone,
        state=config.state,
        disabled=config.disabled,
        enabled=_enabled(config.state, config.disabled),
        write_disposition=config.write_disposition,
        transfer_name=config.transfer_name,
        meta_sql_file=config.meta_sql_file,
        filename_dataset=sql_file.dataset,
        filename_table=sql_file.table,
        raw_config=config.raw,
    )


def _sql_text_finding(
    config_id: str, sql_file: SqlFile, config: TransferConfig
) -> Finding | None:
    """Return a finding when the file text differs from ``params.query``.

    Args:
        config_id: Shared id.
        sql_file: Checked-in SQL. Compared exactly, including whitespace.
        config: Transfer config. A missing ``params.query`` is not a mismatch.

    Returns:
        A ``sql_text_mismatch`` finding, or ``None`` when the texts match
        or the config has no query text.
    """
    if config.params_query is None or sql_file.text == config.params_query:
        return None
    return Finding(
        kinds.SQL_TEXT_MISMATCH,
        f"{config_id}: SQL file differs from params.query",
        config_id,
        {
            "sql_path": str(sql_file.path),
            "file_text": sql_file.text,
            "params_query": config.params_query,
        },
    )


def _destination_name_finding(
    config_id: str,
    sql_file: SqlFile,
    config: TransferConfig,
) -> Finding | None:
    """Return a finding when the filename destination differs from the config.

    A filename that did not parse has ``None`` parts. Those are not equal to
    a set config destination, so the pair is reported.

    Args:
        config_id: Shared id.
        sql_file: Filename dataset and table. Either part may be ``None``.
        config: Transfer-config destination. A missing part skips the check.

    Returns:
        A ``destination_mismatch`` finding, or ``None``.
    """
    if config.destination_dataset is None or config.destination_table is None:
        return None
    if (
        sql_file.dataset == config.destination_dataset
        and sql_file.table == config.destination_table
    ):
        return None
    return Finding(
        kinds.DESTINATION_MISMATCH,
        (
            f"{config_id}: filename destination "
            f"{sql_file.dataset}.{sql_file.table} differs from config "
            f"{config.destination_dataset}.{config.destination_table}"
        ),
        config_id,
        {
            "filename_dataset": sql_file.dataset,
            "filename_table": sql_file.table,
            "destination_dataset": config.destination_dataset,
            "destination_table": config.destination_table,
        },
    )


def _unparsed_destination_finding(
    config_id: str,
    config: TransferConfig,
    destination: TableId | None,
) -> Finding | None:
    """Return a finding when both destination parts are set but do not parse.

    Args:
        config_id: Shared id.
        config: Source of the two destination fields.
        destination: Result of parsing those fields. ``None`` with both fields
            present is the failure.

    Returns:
        An ``unparsed_table_id`` finding, or ``None`` when the destination
        parsed or a part is missing.
    """
    if destination is not None:
        return None
    if config.destination_dataset is None or config.destination_table is None:
        return None
    text = f"{config.destination_dataset}.{config.destination_table}"
    return Finding(
        kinds.UNPARSED_TABLE_ID,
        f"{config_id}: destination {text} is not a table identifier",
        config_id,
        {
            "destination_dataset": config.destination_dataset,
            "destination_table": config.destination_table,
        },
    )


def _meta_file_finding(
    root: Path,
    config_id: str,
    sql_file: SqlFile,
    config: TransferConfig,
) -> Finding | None:
    """Return a finding when ``_meta_sql_file`` does not name this SQL file.

    Args:
        root: Estate root.
        config_id: Shared id.
        sql_file: The paired file.
        config: Transfer config. A missing meta path is a mismatch.

    Returns:
        A ``meta_sql_file_mismatch`` finding, or ``None`` when the path matches.
    """
    if _meta_sql_matches(root, sql_file.path, config.meta_sql_file):
        return None
    return Finding(
        kinds.META_SQL_FILE_MISMATCH,
        f"{config_id}: _meta_sql_file {config.meta_sql_file!r} does not match {sql_file.path.name}",
        config_id,
        {"meta_sql_file": config.meta_sql_file, "sql_path": str(sql_file.path)},
    )


def _enabled_finding(config_id: str, config: TransferConfig) -> Finding | None:
    """Return a finding when ``state`` and ``disabled`` disagree.

    A missing field is not a disagreement.

    Args:
        config_id: Shared id.
        config: Transfer config.

    Returns:
        An ``enabled_state_mismatch`` finding, or ``None``.
    """
    if _enabled(config.state, config.disabled) is not None:
        return None
    if config.state is None or config.disabled is None:
        return None
    return Finding(
        kinds.ENABLED_STATE_MISMATCH,
        f"{config_id}: state {config.state!r} disagrees with disabled {config.disabled!r}",
        config_id,
        {"state": config.state, "disabled": config.disabled},
    )


def _shared_destinations(queries: list[ScheduledQuery]) -> list[Finding]:
    """Report destinations declared by more than one paired query.

    Args:
        queries: Paired queries. Queries without a destination are omitted.

    Returns:
        One ``shared_destination`` finding per table, sorted by ``dataset.table``.
    """
    grouped: dict[str, list[ScheduledQuery]] = defaultdict(list)
    for query in queries:
        if query.destination is not None:
            grouped[query.destination.dataset_table].append(query)
    findings: list[Finding] = []
    for table, group in sorted(grouped.items()):
        if len(group) < 2:
            continue
        findings.append(
            Finding(
                kinds.SHARED_DESTINATION,
                f"{table} is written by {len(group)} configs: {', '.join(query.config_id for query in group)}",
                None,
                {
                    "table": table,
                    "configs": [
                        {
                            "config_id": query.config_id,
                            "write_disposition": query.write_disposition,
                            "schedule": query.schedule,
                        }
                        for query in group
                    ],
                },
            )
        )
    return findings


def _destination(config: TransferConfig) -> TableId | None:
    """Parse the config destination, or return ``None`` when it is incomplete.

    Args:
        config: Transfer config.

    Returns:
        The table id, or ``None`` when either part is missing or the pair
        is not a table identifier.
    """
    if config.destination_dataset is None or config.destination_table is None:
        return None
    return try_parse_table_id(
        f"{config.destination_dataset}.{config.destination_table}"
    )


def _enabled(state: str | None, disabled: bool | None) -> bool | None:
    """Derive enabled from the two source fields without changing either.

    ENABLED with disabled false is enabled. DISABLED with disabled true is
    disabled. Any other combination is unknown and stays None.

    Args:
        state: Transfer-config ``state``.
        disabled: Transfer-config ``disabled``.

    Returns:
        ``True`` when enabled, ``False`` when disabled, or ``None`` when the
        fields disagree or either is missing.
    """
    if state == "ENABLED" and disabled is False:
        return True
    if state == "DISABLED" and disabled is True:
        return False
    return None


def _meta_sql_matches(root: Path, sql_path: Path, meta: str | None) -> bool:
    """Return whether ``_meta_sql_file`` names this SQL file.

    A bare filename matches on the base name. A relative path must equal the
    file's path from ``root`` using forward slashes.

    Args:
        root: Estate root.
        sql_path: Paired SQL file.
        meta: ``_meta_sql_file`` value, or ``None``.

    Returns:
        ``True`` only when the meta path names this file.
    """
    if meta is None:
        return False
    meta_path = Path(meta)
    if meta_path.name != sql_path.name:
        return False
    if meta_path.parent == Path("."):
        return True
    try:
        relative = sql_path.relative_to(root).as_posix()
    except ValueError:
        return False
    return relative == meta_path.as_posix()


def _catalog_tables(catalog: list[CatalogRecord]) -> set[str]:
    """Return ``dataset.table`` values that parsed in the catalog.

    Args:
        catalog: Catalog rows. Unparsed table cells are omitted.

    Returns:
        The join keys.
    """
    return {
        record.table.dataset_table for record in catalog if record.table is not None
    }


def _unparsed_destinations(label: str, unparsed: dict[str, int]) -> list[Finding]:
    """Build one finding per destination string that is not a table id.

    Args:
        label: Source named in the message, such as ``run_history destination``.
        unparsed: Raw cell to the number of rows.

    Returns:
        Findings sorted by the raw cell.
    """
    return [
        Finding(
            kinds.UNPARSED_TABLE_ID,
            f"{label} {destination!r} is not a table identifier ({kinds.count_label(count, 'row')})",
            None,
            {"destination": destination, "row_count": count},
        )
        for destination, count in sorted(unparsed.items())
    ]


def _unparsed_tables(label: str, unparsed: dict[str, int]) -> list[Finding]:
    """Build one finding per table cell that is not a table id.

    Args:
        label: Source named in the message.
        unparsed: Raw cell to the number of rows.

    Returns:
        Findings sorted by the raw cell.
    """
    return [
        Finding(
            kinds.UNPARSED_TABLE_ID,
            f"{label} {table!r} is not a table identifier ({kinds.count_label(count, 'row')})",
            None,
            {"table": table, "row_count": count},
        )
        for table, count in sorted(unparsed.items())
    ]


def _absent_history_tables(absent: dict[str, int]) -> list[Finding]:
    """Build one finding per history destination missing from the catalog.

    Args:
        absent: Raw destination cell to the number of rows.

    Returns:
        Findings sorted by the raw cell.
    """
    return [
        Finding(
            kinds.HISTORY_TABLE_NOT_IN_CATALOG,
            (
                f"{destination} is a run-history destination and is absent from the table catalog "
                f"({kinds.count_label(count, 'row')})"
            ),
            None,
            {"destination": destination, "row_count": count},
        )
        for destination, count in sorted(absent.items())
    ]


def _unknown_history(unknown: dict[str, list[HistoryRecord]]) -> list[Finding]:
    """Build one finding per history config id that has no paired query.

    Args:
        unknown: Config id to the rows that cited it.

    Returns:
        Findings sorted by config id.
    """
    return [
        Finding(
            kinds.HISTORY_UNKNOWN_CONFIG,
            f"{config_id}: {kinds.count_label(len(rows), 'run-history row')} with no mapped config",
            config_id,
            {
                "row_count": len(rows),
                "destinations": sorted({row.raw["destination"] for row in rows}),
            },
        )
        for config_id, rows in sorted(unknown.items())
    ]


def _mismatched_history(mismatched: dict[str, list[HistoryRecord]]) -> list[Finding]:
    """Build one finding per config whose history destination differs.

    Comparison uses ``dataset.table``. The message quotes the paired query's
    destination.

    Args:
        mismatched: Config id to the rows that disagree.

    Returns:
        Findings sorted by config id.
    """
    findings: list[Finding] = []
    for config_id, rows in sorted(mismatched.items()):
        query = rows[0].query
        expected = (
            query.destination.dataset_table if query and query.destination else None
        )
        findings.append(
            Finding(
                kinds.HISTORY_DESTINATION_MISMATCH,
                (
                    f"{config_id}: run history destination differs from {expected} "
                    f"({kinds.count_label(len(rows), 'row')})"
                ),
                config_id,
                {
                    "expected": expected,
                    "destinations": sorted({row.raw["destination"] for row in rows}),
                    "row_count": len(rows),
                },
            )
        )
    return findings
