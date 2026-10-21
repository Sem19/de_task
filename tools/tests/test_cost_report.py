"""COST.txt is rendered from the cost engine, not from fixed estate totals."""

from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from audit import collect_findings
from cost import BYTES_PER_TIB, calculate_estate, present_usd, sum_published
from cost.render import render_cost
from cost.scenarios import build_cost_analysis
from cost_report import build_report, main
from estate import load_inventory
from estate.sources import CATALOG_COLUMNS, HISTORY_COLUMNS, USAGE_COLUMNS
from graph import build_graph
from sql import parse_inventory

_REPO = Path(__file__).resolve().parents[2]
_TIB = BYTES_PER_TIB
_WHEN = "2026-06-01T03:00:00+00:00"


def test_report_totals_and_ranking_follow_the_engine(tmp_path: Path) -> None:
    analysis, text, estate = _analyze(
        tmp_path, _fixture_specs(), _fixture_runs(), _fixture_usage()
    )
    assert f"Queries: {estate.query_count}" in text
    assert f"${present_usd(estate.normalized_daily_run_rate_usd)}" in text
    assert f"${present_usd(estate.historical_estimated_cost_usd)}" in text
    for slice_cost in estate.by_frequency:
        if slice_cost.configs_with_run_rate == 0:
            continue
        assert f"${present_usd(slice_cost.normalized_daily_run_rate_usd)}" in text
    ranked = [config.config_id for config in estate.configs]
    block = text.split("4. Lineage route", maxsplit=1)[0]
    positions = [block.index(config_id) for config_id in ranked]
    assert positions == sorted(positions)
    assert analysis.ranked_configs[0].config_id == ranked[0]
    assert "no daily rate" in text
    assert render_cost(analysis) == text


def test_changing_recorded_bytes_changes_the_ranking_and_the_total(
    tmp_path: Path,
) -> None:
    low_high, low_high_text, low_high_cost = _analyze(
        tmp_path / "low-high",
        [
            _spec("sq_a", "marts", "a", "SELECT 1 AS a"),
            _spec("sq_b", "marts", "b", "SELECT 2 AS b"),
        ],
        [_run("sq_a", "a", _TIB), _run("sq_b", "b", _TIB * 2)],
        [("marts.a", "dashboard", "a"), ("marts.b", "dashboard", "b")],
    )
    high_low, high_low_text, high_low_cost = _analyze(
        tmp_path / "high-low",
        [
            _spec("sq_a", "marts", "a", "SELECT 1 AS a"),
            _spec("sq_b", "marts", "b", "SELECT 2 AS b"),
        ],
        [_run("sq_a", "a", _TIB * 4), _run("sq_b", "b", _TIB)],
        [("marts.a", "dashboard", "a"), ("marts.b", "dashboard", "b")],
    )
    assert low_high_text != high_low_text
    assert [config.config_id for config in low_high.ranked_configs] == ["sq_b", "sq_a"]
    assert [config.config_id for config in high_low.ranked_configs] == ["sq_a", "sq_b"]
    low_total = f"${present_usd(low_high_cost.normalized_daily_run_rate_usd)}"
    high_total = f"${present_usd(high_low_cost.normalized_daily_run_rate_usd)}"
    assert low_total in low_high_text
    assert low_total not in high_low_text
    assert high_total in high_low_text


def test_equal_daily_rates_rank_by_config_id(tmp_path: Path) -> None:
    analysis, text, _estate = _analyze(
        tmp_path,
        [
            _spec("sq_b", "marts", "b", "SELECT 1 AS b"),
            _spec("sq_a", "marts", "a", "SELECT 2 AS a"),
        ],
        [_run("sq_b", "b", _TIB), _run("sq_a", "a", _TIB)],
        [("marts.a", "dashboard", "a"), ("marts.b", "dashboard", "b")],
    )
    assert [config.config_id for config in analysis.ranked_configs] == ["sq_a", "sq_b"]
    block = text.split("4. Lineage route", maxsplit=1)[0]
    assert block.index("sq_a") < block.index("sq_b")


def test_scenarios_are_priced_from_observed_rates(tmp_path: Path) -> None:
    analysis, text, _estate = _analyze(
        tmp_path, _fixture_specs(), _fixture_runs(), _fixture_usage()
    )
    idle = _scenario(analysis, "stop-no-recorded-consumer")
    assert idle.kind == "measured"
    assert idle.change_class == "owner_confirmation"
    assert idle.stops == ("sq_idle",)
    assert idle.daily_savings_usd == Decimal("6.25")
    assert "sq_kept" not in idle.stops

    failed = _scenario(analysis, "stop-zero-scan-failures")
    assert failed.daily_savings_usd == Decimal(0)
    assert failed.stops == ("sq_fail",)

    writers = _scenario(analysis, "stop-competing-writer-reports.shared")
    assert writers.kind == "assumed"
    assert writers.retains == ("sq_low",)
    assert writers.stops == ("sq_high",)
    assert writers.daily_savings_usd == Decimal("12.50")
    assert "was not measured" in writers.uncertainty

    repeated = _scenario(analysis, "stop-repeated-sql-sq_copy_a-sq_copy_b")
    assert repeated.retains == ("sq_copy_a",)
    assert repeated.daily_savings_usd == Decimal("18.75")
    assert "was not measured" in repeated.uncertainty

    hourly = _scenario(analysis, "one-run-per-observed-day")
    assert hourly.change_class == "business_logic"
    member = next(item for item in hourly.members if item.config_id == "sq_hour")
    assert member.daily_savings_usd == Decimal("6.25")
    assert "was not measured" in hourly.uncertainty
    assert "Scenarios are not additive." in text

    keys = [
        (
            scenario.daily_savings_usd is None,
            -(scenario.daily_savings_usd or 0),
            scenario.scenario_id,
        )
        for scenario in analysis.scenarios
    ]
    assert keys == sorted(keys)


def test_competing_writer_follow_whichever_rate_is_lower(tmp_path: Path) -> None:
    cheaper_low = _analyze(
        tmp_path / "low",
        [
            _spec("sq_low", "reports", "shared", "SELECT 1 AS low"),
            _spec("sq_high", "reports", "shared", "SELECT 2 AS high"),
        ],
        [_run("sq_low", "low", _TIB), _run("sq_high", "high", _TIB * 2)],
        [("reports.shared", "dashboard", "shared")],
    )[0]
    cheaper_high = _analyze(
        tmp_path / "high",
        [
            _spec("sq_low", "reports", "shared", "SELECT 1 AS low"),
            _spec("sq_high", "reports", "shared", "SELECT 2 AS high"),
        ],
        [_run("sq_low", "low", _TIB * 2), _run("sq_high", "high", _TIB)],
        [("reports.shared", "dashboard", "shared")],
    )[0]
    assert _scenario(cheaper_low, "stop-competing-writer-reports.shared").retains == (
        "sq_low",
    )
    assert _scenario(cheaper_high, "stop-competing-writer-reports.shared").retains == (
        "sq_high",
    )
    assert render_cost(cheaper_low) != render_cost(cheaper_high)


def test_revenue_route_excludes_a_query_that_only_shares_a_source(
    tmp_path: Path,
) -> None:
    analysis, text, _estate = _analyze(
        tmp_path, _fixture_specs(), _fixture_runs(), _fixture_usage()
    )
    route_ids = [query.config_id for query in analysis.route.queries]
    assert "sq_rev" in route_ids
    assert "sq_src" in route_ids
    assert "sq_sib" not in route_ids
    assert analysis.route.queries[0].config_id == "sq_rev"
    assert analysis.route.daily_cost_usd == Decimal("18.75")
    assert "sq_sib" in analysis.route.excluded_sibling_config_ids
    assert analysis.route.excluded_sibling_daily_cost_usd == Decimal("25.00")
    assert f"${present_usd(analysis.route.daily_cost_usd)}" in text
    assert "excluded from the route" in text


def test_finding_cost_is_the_sum_of_engine_rates(tmp_path: Path) -> None:
    inventory = _load(tmp_path, _fixture_specs(), _fixture_runs(), _fixture_usage())
    parses = parse_inventory(inventory)
    graph = build_graph(inventory, parses)
    findings = collect_findings(inventory, parses, graph)
    estate = calculate_estate(inventory)
    analysis = build_cost_analysis(
        inventory, estate, graph, findings, source_name=tmp_path.name
    )
    repeated = next(
        item
        for item in analysis.finding_costs
        if set(item.config_ids) == {"sq_copy_a", "sq_copy_b"}
    )
    by_id = {config.config_id: config for config in estate.configs}
    assert repeated.daily_cost_usd == sum_published(
        [
            by_id["sq_copy_a"].normalized_daily_run_rate_usd,
            by_id["sq_copy_b"].normalized_daily_run_rate_usd,
        ]
    )
    costs = [item.daily_cost_usd for item in analysis.finding_costs]
    assert costs == sorted(costs, reverse=True)


def test_a_config_with_no_runs_is_not_printed_as_zero_cost(tmp_path: Path) -> None:
    _analysis, text, _estate = _analyze(
        tmp_path, _fixture_specs(), _fixture_runs(), _fixture_usage()
    )
    ranking = text.split("3. Daily cost ranking", maxsplit=1)[1].split(
        "4. Lineage route", maxsplit=1
    )[0]
    idle_off = ranking.split("sq_off", maxsplit=1)[1].split("\n", maxsplit=1)[0]
    assert "no observed runs" in idle_off
    assert "$0.0000" not in idle_off


def test_report_text_is_stable_and_the_generator_writes_it(tmp_path: Path) -> None:
    root = tmp_path / "estate"
    _analyze(root, _fixture_specs(), _fixture_runs(), _fixture_usage())
    output = tmp_path / "COST.txt"
    other = tmp_path / "COST-again.txt"
    assert main([str(root), "--output", str(output)]) == 0
    assert main([str(root), "--output", str(other)]) == 0
    assert output.read_text(encoding="utf-8") == other.read_text(encoding="utf-8")
    assert output.read_text(encoding="utf-8") == build_report(root)


def test_generator_source_has_no_estate_specific_totals() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in (
        "cost/scenarios.py",
        "cost/render.py",
        "cost/report_models.py",
        "cost_report.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "sq_3030" not in source
        assert "1423.51" not in source
        assert "611.66" not in source


def test_real_estate_report_matches_the_engine_and_is_stable() -> None:
    root = _REPO / "task2_estate"
    before = _digest(root)
    first = build_report(root)
    second = build_report(root)
    assert first == second
    assert _digest(root) == before
    estate = calculate_estate(load_inventory(root))
    assert f"Queries: {estate.query_count}" in first
    assert f"${present_usd(estate.normalized_daily_run_rate_usd)}" in first
    assert "not an invoice" in first
    assert "Scenarios are not additive." in first
    assert "was not measured" in first
    block = first.split("4. Lineage route", maxsplit=1)[0]
    ranked = [
        config.config_id
        for config in estate.configs
        if config.normalized_daily_run_rate_usd is not None
    ]
    assert block.index(ranked[0]) < block.index(ranked[1])
    for slice_cost in estate.by_frequency:
        if slice_cost.label == "other":
            continue
        assert f"${present_usd(slice_cost.normalized_daily_run_rate_usd)}" in first


def _scenario(analysis, scenario_id: str):
    return next(item for item in analysis.scenarios if item.scenario_id == scenario_id)


def _analyze(
    root: Path,
    specs: list[dict[str, object]],
    runs: list[dict[str, str]],
    usage: list[tuple[str, str, str]],
):
    inventory = _load(root, specs, runs, usage)
    parses = parse_inventory(inventory)
    graph = build_graph(inventory, parses)
    estate = calculate_estate(inventory)
    analysis = build_cost_analysis(inventory, estate, graph, (), source_name=root.name)
    return analysis, render_cost(analysis), estate


def _fixture_specs() -> list[dict[str, object]]:
    shared = "SELECT id FROM `raw.events`"
    return [
        _spec("sq_idle", "marts", "idle", "SELECT 9 AS idle_marker"),
        _spec("sq_kept", "marts", "kept", "SELECT 8 AS kept_marker"),
        _spec("sq_fail", "marts", "failed", "SELECT 7 AS failed_marker"),
        _spec(
            "sq_off",
            "marts",
            "paused",
            "SELECT 6 AS paused",
            state="DISABLED",
            disabled=True,
        ),
        _spec("sq_copy_a", "reports", "copy_a", shared),
        _spec("sq_copy_b", "reports", "copy_b", shared),
        _spec("sq_low", "reports", "shared", "SELECT 1 AS low_writer"),
        _spec("sq_high", "reports", "shared", "SELECT 2 AS high_writer"),
        _spec(
            "sq_hour",
            "marts",
            "hourly",
            "SELECT 3 AS hourly_marker",
            schedule="every 1 hours",
        ),
        _spec("sq_src", "staging", "stg_payments", "SELECT id FROM `raw.payments`"),
        _spec(
            "sq_rev",
            "reports",
            "revenue_daily",
            "SELECT id FROM `staging.stg_payments`",
        ),
        _spec("sq_sib", "marts", "sibling", "SELECT id FROM `raw.payments`"),
    ]


def _fixture_runs() -> list[dict[str, str]]:
    runs = [
        _run("sq_idle", "idle", _TIB),
        _run("sq_kept", "kept", _TIB),
        _run("sq_fail", "fail", 0, state="FAILED"),
        _run("sq_copy_a", "copy-a", _TIB),
        _run("sq_copy_b", "copy-b", _TIB * 3),
        _run("sq_low", "low", _TIB),
        _run("sq_high", "high", _TIB * 2),
        _run("sq_src", "src", _TIB),
        _run("sq_rev", "rev", _TIB * 2),
        _run("sq_sib", "sib", _TIB * 4),
    ]
    runs.append(_run("sq_hour", "hour-1", _TIB, when="2026-06-01T01:00:00+00:00"))
    runs.append(_run("sq_hour", "hour-2", _TIB, when="2026-06-01T02:00:00+00:00"))
    return runs


def _fixture_usage() -> list[tuple[str, str, str]]:
    return [
        ("marts.kept", "dashboard", "kept"),
        ("marts.failed", "dashboard", "failed"),
        ("reports.copy_a", "dashboard", "copy-a"),
        ("reports.copy_b", "dashboard", "copy-b"),
        ("reports.shared", "dashboard", "shared"),
        ("marts.hourly", "dashboard", "hourly"),
        ("staging.stg_payments", "dashboard", "payments"),
        ("reports.revenue_daily", "dashboard", "revenue"),
        ("marts.sibling", "dashboard", "sibling"),
    ]


def _spec(
    config_id: str,
    dataset: str,
    table: str,
    sql: str,
    *,
    schedule: str = "every day 03:00",
    state: str = "ENABLED",
    disabled: bool = False,
) -> dict[str, object]:
    return {
        "config_id": config_id,
        "dataset": dataset,
        "table": table,
        "sql": sql,
        "schedule": schedule,
        "state": state,
        "disabled": disabled,
    }


def _run(
    config_id: str,
    run_id: str,
    total_bytes: int,
    *,
    state: str = "SUCCEEDED",
    when: str = _WHEN,
) -> dict[str, str]:
    return {
        "run_id": run_id,
        "config_id": config_id,
        "destination": "marts.out",
        "schedule_time": when,
        "start_time": when,
        "end_time": when,
        "state": state,
        "error_message": "" if state == "SUCCEEDED" else "failed",
        "total_bytes_processed": str(total_bytes),
        "rows_written": "0",
    }


def _load(
    root: Path,
    specs: list[dict[str, object]],
    runs: list[dict[str, str]],
    usage: list[tuple[str, str, str]],
):
    sql_dir = root / "scheduled_queries"
    sql_dir.mkdir(parents=True)
    configs = []
    for spec in specs:
        config_id = str(spec["config_id"])
        dataset = str(spec["dataset"])
        table = str(spec["table"])
        sql = str(spec["sql"])
        (sql_dir / f"{config_id}__{dataset}__{table}.sql").write_text(
            sql, encoding="utf-8"
        )
        configs.append(
            {
                "name": f"projects/acme/locations/eu/transferConfigs/{config_id}",
                "displayName": config_id,
                "destinationDatasetId": dataset,
                "schedule": spec["schedule"],
                "scheduleOptionsV2": {"timeZone": "Europe/Kyiv"},
                "state": spec["state"],
                "disabled": spec["disabled"],
                "params": {
                    "query": sql,
                    "destination_table_name_template": table,
                    "write_disposition": "WRITE_TRUNCATE",
                },
                "_meta_sql_file": f"scheduled_queries/{config_id}__{dataset}__{table}.sql",
            }
        )
    (root / "transfer_configs.json").write_text(
        json.dumps({"transferConfigs": configs}), encoding="utf-8"
    )
    with (root / "run_history.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_COLUMNS)
        writer.writeheader()
        writer.writerows(runs)
    usage_lines = [",".join(USAGE_COLUMNS)]
    usage_lines.extend(
        f"{table},{kind},{consumer},1" for table, kind, consumer in usage
    )
    (root / "downstream_usage.csv").write_text(
        "\n".join(usage_lines) + "\n", encoding="utf-8"
    )
    (root / "table_catalog.csv").write_text(
        ",".join(CATALOG_COLUMNS) + "\n", encoding="utf-8"
    )
    (root / "schemas.json").write_text("{}", encoding="utf-8")
    return load_inventory(root)


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()
