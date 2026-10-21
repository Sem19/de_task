"""Focused tests for the corrected audit detectors."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from audit import collect_findings, render_audit
from audit.sql_ast import partition_coverage, projection_count, retention_days
from audit_report import build_report
from estate import load_inventory
from estate.sources import CATALOG_COLUMNS, HISTORY_COLUMNS, USAGE_COLUMNS
from graph import build_graph
from sql import parse_inventory

_BROKEN = """
SELECT (1 -- `acme.marts.kept`
FROM `acme.marts.kept`
"""


def _config(
    config_id: str,
    dataset: str,
    table: str,
    query: str,
    *,
    schedule: str = "every day 03:00",
    disposition: str = "WRITE_TRUNCATE",
) -> dict[str, object]:
    return {
        "name": f"projects/acme/locations/eu/transferConfigs/{config_id}",
        "displayName": config_id,
        "destinationDatasetId": dataset,
        "schedule": schedule,
        "scheduleOptionsV2": {"timeZone": "Europe/Kyiv"},
        "state": "ENABLED",
        "disabled": False,
        "params": {
            "query": query,
            "destination_table_name_template": table,
            "write_disposition": disposition,
        },
        "_meta_sql_file": f"scheduled_queries/{config_id}__{dataset}__{table}.sql",
    }


def _write(
    root: Path,
    specs: list[tuple[str, str, str, str]],
    *,
    history: list[list[str]] | None = None,
    usage: list[list[str]] | None = None,
    catalog: list[list[str]] | None = None,
    schedules: dict[str, str] | None = None,
    dispositions: dict[str, str] | None = None,
) -> None:
    sql_dir = root / "scheduled_queries"
    sql_dir.mkdir(parents=True)
    configs = []
    schedules = schedules or {}
    dispositions = dispositions or {}
    for config_id, dataset, table, sql in specs:
        name = f"{config_id}__{dataset}__{table}.sql"
        (sql_dir / name).write_text(sql, encoding="utf-8")
        configs.append(
            _config(
                config_id,
                dataset,
                table,
                sql,
                schedule=schedules.get(config_id, "every day 03:00"),
                disposition=dispositions.get(config_id, "WRITE_TRUNCATE"),
            )
        )
    (root / "transfer_configs.json").write_text(
        json.dumps({"transferConfigs": configs}), encoding="utf-8"
    )
    _csv(root / "run_history.csv", HISTORY_COLUMNS, history or [])
    _csv(root / "downstream_usage.csv", USAGE_COLUMNS, usage or [])
    _csv(root / "table_catalog.csv", CATALOG_COLUMNS, catalog or [])
    (root / "schemas.json").write_text("{}", encoding="utf-8")


def _csv(path: Path, header: tuple[str, ...], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _findings(root: Path):
    inventory = load_inventory(root)
    parses = parse_inventory(inventory)
    graph = build_graph(inventory, parses)
    return collect_findings(inventory, parses, graph)


def test_projection_count_uses_the_outer_select() -> None:
    sql = """
    WITH inner_a AS (
      SELECT a, b, c FROM `acme.raw.t`
    ),
    inner_b AS (
      SELECT x, y FROM inner_a
    )
    SELECT x FROM inner_b
    """
    assert projection_count(sql) == 1
    nested = "SELECT a, b FROM (SELECT a, b, c, d FROM `acme.raw.t`)"
    assert projection_count(nested) == 2
    assert projection_count("SELECT * FROM (SELECT a, b FROM `acme.raw.t`)") is None
    assert projection_count("SELECT t.* EXCEPT (a), b FROM `acme.raw.t` t") is None
    assert projection_count("SELECT a, COUNT(*) AS n FROM `acme.raw.t`") == 2
    union = """
    WITH c AS (SELECT a, b, c, d FROM `acme.raw.t`)
    SELECT a, b FROM c
    UNION ALL
    SELECT a, b, c FROM c
    """
    assert projection_count(union) is None
    agreed = """
    SELECT a, b FROM `acme.raw.t`
    UNION ALL
    SELECT c, d FROM `acme.raw.t`
    """
    assert projection_count(agreed) == 2


def test_known_column_is_reported_and_unknown_column_is_not(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [
            ("q_drop", "reports", "out", "SELECT b, * EXCEPT (a) FROM `acme.raw.src`"),
            ("q_read", "reports", "child", "SELECT a FROM `acme.reports.out`"),
            ("q_keep", "reports", "kept", "SELECT a, b FROM `acme.raw.src`"),
            ("q_ok", "reports", "fine", "SELECT a FROM `acme.reports.kept`"),
            (
                "q_star",
                "reports",
                "wide",
                "SELECT a, * EXCEPT (a) FROM `acme.raw.src`",
            ),
            ("q_mystery", "reports", "ask", "SELECT mystery FROM `acme.reports.wide`"),
        ],
        catalog=[
            [
                "raw.src",
                "BASE TABLE",
                "1",
                "2",
                "",
                "",
                "2026-01-01",
                "2026-01-01",
                "2",
            ],
            [
                "reports.out",
                "BASE TABLE",
                "1",
                "2",
                "",
                "",
                "2026-01-01",
                "2026-01-01",
                "2",
            ],
            [
                "reports.kept",
                "BASE TABLE",
                "1",
                "2",
                "",
                "",
                "2026-01-01",
                "2026-01-01",
                "2",
            ],
            [
                "reports.wide",
                "BASE TABLE",
                "1",
                "2",
                "",
                "",
                "2026-01-01",
                "2026-01-01",
                "2",
            ],
        ],
    )
    by_id = {item.finding_id: item for item in _findings(tmp_path)}
    omitted = by_id["CR-COLUMN-NOT-PROJECTED"]
    assert "q_read" in omitted.config_ids
    assert "q_drop" in omitted.config_ids
    assert " reads a from reports.out" in omitted.evidence
    assert "q_ok" not in omitted.config_ids
    assert "mystery" not in omitted.evidence
    assert omitted.confidence == "needs_verification"


def test_partition_predicate_is_per_source() -> None:
    partitioned = {
        "raw.events": "_PARTITIONTIME",
        "raw.payments": "_PARTITIONTIME",
    }
    one = """
    SELECT e.id
    FROM `acme.raw.events` e
    JOIN `acme.raw.payments` p ON e.id = p.id
    WHERE e._PARTITIONTIME >= TIMESTAMP('2026-01-01')
    """
    coverage = partition_coverage(one, partitioned)
    assert coverage is not None
    assert "raw.events" in coverage.filtered
    assert "raw.payments" in coverage.unfiltered
    ambiguous = """
    SELECT e.id
    FROM `acme.raw.events` e
    JOIN `acme.raw.payments` p ON e.id = p.id
    WHERE _PARTITIONTIME >= TIMESTAMP('2026-01-01')
    """
    mixed = partition_coverage(ambiguous, partitioned)
    assert mixed is not None
    assert mixed.unfiltered == frozenset()
    assert mixed.ambiguous == frozenset({"raw.events", "raw.payments"})
    ordered = "SELECT id FROM `acme.raw.events` ORDER BY _PARTITIONTIME"
    order_only = partition_coverage(ordered, {"raw.events": "_PARTITIONTIME"})
    assert order_only is not None
    assert "raw.events" in order_only.unfiltered


def test_interval_outside_the_outer_filter_is_not_retention() -> None:
    select_only = "SELECT DATE_ADD(d, INTERVAL 3 DAY) AS next_day FROM `acme.raw.t`"
    assert retention_days(select_only) == (None, False)
    outer = """
    SELECT d
    FROM `acme.raw.t`
    WHERE d >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
    """
    assert retention_days(outer) == (3, False)
    nested = """
    WITH c AS (
      SELECT d FROM `acme.raw.t`
      WHERE d >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
    )
    SELECT d FROM c
    """
    assert retention_days(nested).uncertain is True
    either = """
    SELECT d FROM `acme.raw.t`
    WHERE d >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
       OR d >= DATE_SUB(CURRENT_DATE(), INTERVAL 400 DAY)
    """
    assert retention_days(either).uncertain is True


def test_lookback_ignores_an_interval_that_is_not_the_write_window(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        [
            (
                "q_writer",
                "reports",
                "out",
                """
                SELECT d AS event_date, DATE_ADD(d, INTERVAL 1 DAY) AS next_day
                FROM `acme.raw.events`
                WHERE d >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
                """,
            ),
            (
                "q_reader",
                "reports",
                "child",
                """
                SELECT event_date, DATE_ADD(event_date, INTERVAL 1 DAY) AS next_day
                FROM `acme.reports.out`
                WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 400 DAY)
                """,
            ),
            (
                "q_spine",
                "reports",
                "spine",
                """
                SELECT DATE_ADD(d, INTERVAL 3 DAY) AS event_date
                FROM `acme.raw.events`
                """,
            ),
            (
                "q_long",
                "reports",
                "late",
                """
                SELECT event_date
                FROM `acme.reports.spine`
                WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 400 DAY)
                """,
            ),
        ],
        catalog=[
            [
                "raw.events",
                "BASE TABLE",
                "1",
                "2",
                "",
                "",
                "2026-01-01",
                "2026-01-01",
                "2",
            ],
        ],
    )
    ids = {item.finding_id for item in _findings(tmp_path)}
    assert "CR-SHORT-LOOKBACK-q_writer" in ids
    assert "CR-SHORT-LOOKBACK-q_spine" not in ids


def test_hourly_same_day_run_is_not_paired_with_an_earlier_reader(
    tmp_path: Path,
) -> None:
    history = []
    for day in range(1, 15):
        date = f"2026-06-{day:02d}"
        history.append(
            [
                "p" + str(day) + "a",
                "q_hour",
                "reports.out",
                f"{date}T04:00:00+00:00",
                f"{date}T04:00:10+00:00",
                f"{date}T04:00:20+00:00",
                "SUCCEEDED",
                "",
                "0",
                "0",
            ]
        )
        history.append(
            [
                "p" + str(day) + "b",
                "q_hour",
                "reports.out",
                f"{date}T23:00:00+00:00",
                f"{date}T23:00:10+00:00",
                f"{date}T23:00:40+00:00",
                "SUCCEEDED",
                "",
                "0",
                "0",
            ]
        )
        history.append(
            [
                "r" + str(day),
                "q_day",
                "reports.child",
                f"{date}T05:00:00+00:00",
                f"{date}T05:00:10+00:00",
                f"{date}T05:00:20+00:00",
                "SUCCEEDED",
                "",
                "0",
                "0",
            ]
        )
    _write(
        tmp_path,
        [
            ("q_hour", "reports", "out", "SELECT 1 AS id FROM `acme.raw.events`"),
            ("q_day", "reports", "child", "SELECT id FROM `acme.reports.out`"),
        ],
        history=history,
        schedules={"q_hour": "every 1 hours", "q_day": "every day 05:00"},
        catalog=[
            [
                "raw.events",
                "BASE TABLE",
                "1",
                "2",
                "",
                "",
                "2026-01-01",
                "2026-01-01",
                "1",
            ],
        ],
    )
    ids = {item.finding_id for item in _findings(tmp_path)}
    assert "OP-SCHEDULE-OVERLAP-q_hour-q_day" not in ids


def test_daily_overlap_is_a_clock_fact_and_hourly_overlap_is_lower_priority(
    tmp_path: Path,
) -> None:
    history = []
    for day in range(1, 15):
        history.append(
            [
                f"p{day}",
                "q_prod",
                "reports.out",
                f"2026-06-{day:02d}T03:00:00+00:00",
                f"2026-06-{day:02d}T03:00:10+00:00",
                f"2026-06-{day:02d}T03:40:00+00:00",
                "SUCCEEDED",
                "",
                "0",
                "0",
            ]
        )
        history.append(
            [
                f"r{day}",
                "q_read",
                "reports.child",
                f"2026-06-{day:02d}T03:10:00+00:00",
                f"2026-06-{day:02d}T03:10:05+00:00",
                f"2026-06-{day:02d}T03:20:00+00:00",
                "SUCCEEDED",
                "",
                "0",
                "0",
            ]
        )
        history.append(
            [
                f"h{day}",
                "q_hour",
                "reports.hour",
                f"2026-06-{day:02d}T05:00:00+00:00",
                f"2026-06-{day:02d}T05:00:01+00:00",
                f"2026-06-{day:02d}T05:02:00+00:00",
                "SUCCEEDED",
                "",
                "0",
                "0",
            ]
        )
        history.append(
            [
                f"d{day}",
                "q_after",
                "reports.after",
                f"2026-06-{day:02d}T05:00:00+00:00",
                f"2026-06-{day:02d}T05:00:10+00:00",
                f"2026-06-{day:02d}T05:01:00+00:00",
                "SUCCEEDED",
                "",
                "0",
                "0",
            ]
        )
    _write(
        tmp_path,
        [
            ("q_prod", "reports", "out", "SELECT 1 AS id FROM `acme.raw.events`"),
            ("q_read", "reports", "child", "SELECT id FROM `acme.reports.out`"),
            ("q_hour", "reports", "hour", "SELECT 1 AS id FROM `acme.raw.events`"),
            ("q_after", "reports", "after", "SELECT id FROM `acme.reports.hour`"),
        ],
        history=history,
        schedules={"q_hour": "every 1 hours", "q_after": "every day 05:00"},
        catalog=[
            [
                "raw.events",
                "BASE TABLE",
                "1",
                "2",
                "",
                "",
                "2026-01-01",
                "2026-01-01",
                "1",
            ],
        ],
    )
    by_id = {item.finding_id: item for item in _findings(tmp_path)}
    daily = by_id["OP-SCHEDULE-OVERLAP-q_prod-q_read"]
    assert daily.priority == 1
    assert daily.confidence == "confirmed"
    assert "stale" in daily.evidence
    hourly = by_id["OP-SCHEDULE-OVERLAP-q_hour-q_after"]
    assert hourly.priority == 2
    assert "hourly" in hourly.evidence


def test_catalog_gap_is_separate_from_not_found(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [
            ("q_fail", "reports", "failed", "SELECT id FROM `acme.raw.missing`"),
            ("q_gap", "reports", "open", "SELECT id FROM `acme.raw.uncatalogued`"),
        ],
        history=[
            [
                "run1",
                "q_fail",
                "reports.failed",
                "2026-06-01T03:00:00+00:00",
                "2026-06-01T03:00:01+00:00",
                "2026-06-01T03:00:02+00:00",
                "FAILED",
                "Not found: Table acme:raw.missing was not found",
                "0",
                "0",
            ]
        ],
    )
    by_id = {item.finding_id: item for item in _findings(tmp_path)}
    failed = by_id["OP-NOT-FOUND-raw-missing"]
    assert failed.confidence == "confirmed"
    assert "q_fail" in failed.config_ids
    assert "q_gap" not in failed.config_ids
    gap = by_id["OP-CATALOG-GAP-raw-uncatalogued"]
    assert gap.confidence == "needs_verification"
    assert "absent from production" in gap.evidence
    assert "OP-NOT-FOUND-raw-uncatalogued" not in by_id


def test_uncertain_reader_blocks_retirement(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [
            ("q_kept", "marts", "kept", "SELECT 1 AS id"),
            ("q_bad", "reports", "broken", _BROKEN),
            ("q_idle", "marts", "unused", "SELECT 1 AS id"),
        ],
    )
    findings = _findings(tmp_path)
    retire = next(
        item for item in findings if item.finding_id == "MN-RETIRE-NO-RECORDED-CONSUMER"
    )
    assert "q_kept" not in retire.config_ids
    assert "q_idle" in retire.config_ids
    assert retire.confidence == "needs_verification"
    text = render_audit(
        load_inventory(tmp_path),
        parse_inventory(load_inventory(tmp_path)),
        build_graph(
            load_inventory(tmp_path), parse_inventory(load_inventory(tmp_path))
        ),
        findings,
    )
    assert "cannot be disabled automatically" in text


def test_findings_and_report_are_deterministic(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [("q1", "reports", "out", "SELECT a FROM `acme.raw.t`")],
        catalog=[
            ["raw.t", "BASE TABLE", "1", "2", "", "", "2026-01-01", "2026-01-01", "1"]
        ],
    )
    assert _findings(tmp_path) == _findings(tmp_path)
    assert build_report(tmp_path) == build_report(tmp_path)
