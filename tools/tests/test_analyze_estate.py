"""Integration tests for the estate analyzer."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from analyze_estate import analyze, main
from estate import load_inventory
from estate.sources import CATALOG_COLUMNS, HISTORY_COLUMNS, USAGE_COLUMNS
from graph import build_graph
from sql import parse_inventory

_REPO = Path(__file__).resolve().parents[2]
_BROKEN = """
SELECT (1 -- `acme.raw.payments`
FROM `acme.raw.payments`
"""


def _config(
    config_id: str,
    dataset: str,
    table: str,
    query: str,
    *,
    project: str = "acme",
    disposition: str = "WRITE_TRUNCATE",
) -> dict[str, object]:
    return {
        "name": f"projects/{project}/locations/eu/transferConfigs/{config_id}",
        "displayName": config_id,
        "destinationDatasetId": dataset,
        "schedule": "every day 03:00",
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
    root: Path, specs: list[tuple[str, str, str, str]], **kwargs: object
) -> None:
    sql_dir = root / "scheduled_queries"
    sql_dir.mkdir(parents=True)
    configs = []
    for config_id, dataset, table, sql in specs:
        name = f"{config_id}__{dataset}__{table}.sql"
        (sql_dir / name).write_text(sql, encoding="utf-8")
        disposition = "WRITE_APPEND" if config_id == "q_hourly" else "WRITE_TRUNCATE"
        configs.append(_config(config_id, dataset, table, sql, disposition=disposition))
    (root / "transfer_configs.json").write_text(
        json.dumps({"transferConfigs": configs}), encoding="utf-8"
    )
    (root / "run_history.csv").write_text(
        ",".join(HISTORY_COLUMNS) + "\n", encoding="utf-8"
    )
    usage = kwargs.get("usage", "reports.revenue_daily,dashboard,looker/exec,3\n")
    (root / "downstream_usage.csv").write_text(
        ",".join(USAGE_COLUMNS) + "\n" + str(usage), encoding="utf-8"
    )
    catalog = kwargs.get(
        "catalog", "raw.payments,BASE TABLE,1,2,,,2026-01-01,2026-01-01,1\n"
    )
    (root / "table_catalog.csv").write_text(
        ",".join(CATALOG_COLUMNS) + "\n" + str(catalog), encoding="utf-8"
    )
    (root / "schemas.json").write_text("{}", encoding="utf-8")


def _synthetic(root: Path) -> None:
    _write(
        root,
        [
            ("q_up", "staging", "src", "SELECT * FROM `acme.raw.payments`"),
            ("q_daily", "reports", "revenue_daily", "SELECT * FROM `acme.staging.src`"),
            (
                "q_hourly",
                "reports",
                "revenue_daily",
                "SELECT * FROM `acme.raw.payments`",
            ),
            (
                "q_down",
                "reports",
                "child",
                "SELECT * FROM `acme.reports.revenue_daily`",
            ),
            ("q_self", "reports", "snap", "SELECT * FROM `acme.reports.snap`"),
            ("q_left", "marts", "left", "SELECT * FROM `acme.marts.right`"),
            ("q_right", "marts", "right", "SELECT * FROM `acme.marts.left`"),
            ("q_miss", "reports", "gap", "SELECT * FROM `acme.staging.gone`"),
            ("q_bad", "reports", "broken", _BROKEN),
            ("q_sibling", "other", "side", "SELECT * FROM `acme.raw.payments`"),
        ],
    )


def test_synthetic_estate_reports_structure_without_dropping_a_writer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _synthetic(root)
        before = {
            path: path.read_bytes()
            for path in (root / "scheduled_queries").glob("*.sql")
        }
        first = analyze(root)
        second = analyze(root)
        assert first == second
        assert before == {
            path: path.read_bytes()
            for path in (root / "scheduled_queries").glob("*.sql")
        }

    assert "inventory.queries: 10" in first
    assert "parse.queries: 10" in first
    assert "parse.succeeded: 9" in first
    assert "parse.failed: 1" in first
    assert "parse.error: q_bad:" in first
    assert "parse.uncertain: q_bad acme.raw.payments unknown" in first
    assert "parse.references.uncertain: 1" in first
    assert "identity.resolution: unambiguous" in first
    assert "identity.project: acme" in first
    assert "graph.edges.reads.uncertain: 1" in first
    assert "duplicate_writer:" in first
    assert "q_daily WRITE_TRUNCATE" in first
    assert "q_hourly WRITE_APPEND" in first
    assert "missing_producer: acme.staging.gone" in first
    assert "self_read: q_self acme.reports.snap" in first
    assert "cycle:" in first and "q_left" in first and "q_right" in first
    assert "lineage.upstream:" in first
    assert "query q_up" in first.split("lineage.downstream:")[0]
    assert "query q_down" in first
    assert "lineage.consumers: consumer dashboard looker/exec" in first
    assert "proposed_scope: graph slice only; not a migration decision" in first
    scope = first.split("proposed_scope.queries: ", 1)[1].split("\n", 1)[0]
    assert "q_daily" in scope and "q_hourly" in scope
    assert "q_sibling" not in scope


def test_conflicting_projects_are_not_resolved_to_one_label() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root,
            [
                ("q1", "reports", "out", "SELECT * FROM `p1.raw.t`"),
                ("q2", "reports", "other", "SELECT * FROM `p2.raw.t`"),
            ],
            usage="",
            catalog="raw.t,BASE TABLE,1,2,,,2026-01-01,2026-01-01,1\n",
        )
        report = analyze(root, target="out")
    assert "identity.resolution: conflict" in report
    assert "identity.project: none" in report
    assert "identity.conflicts: raw.t" in report
    assert "raw.t [projects p1 p2; unresolved]" in report
    assert "identity.project: p1" not in report


def test_real_estate_accounts_for_every_query_and_keeps_both_writers() -> None:
    root = _REPO / "task2_estate"
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    inventory = load_inventory(root)
    parses = parse_inventory(inventory)
    graph = build_graph(inventory, parses)
    first = analyze(root)
    second = analyze(root)
    assert first == second
    assert before == {path: path.read_bytes() for path in before}

    assert f"inventory.queries: {len(inventory.queries)}" in first
    assert f"parse.queries: {len(parses)}" in first
    failed = [item for item in parses if not item.parsed]
    assert f"parse.succeeded: {len(parses) - len(failed)}" in first
    assert f"parse.failed: {len(failed)}" in first
    assert failed
    for item in failed:
        assert item.config_id is not None
        assert f"parse.error: {item.config_id}:" in first
        assert any(ref.uncertain for ref in item.references)
    assert f"graph.nodes: {len(graph.nodes)}" in first
    assert f"graph.edges: {len(graph.edges)}" in first
    confirmed = sum(not edge.uncertain for edge in graph.edges)
    uncertain = sum(edge.uncertain for edge in graph.edges)
    assert confirmed + uncertain == len(graph.edges)
    assert f"graph.edges.confirmed: {confirmed}" in first
    assert f"graph.edges.uncertain: {uncertain}" in first
    assert "identity.resolution: unresolved" in first
    assert "identity.sql_projects: acme-analytics-prod" in first
    assert "identity.config_projects: 418822" in first
    assert "identity.project: none" in first
    writer_line = next(
        line for line in first.splitlines() if line.startswith("duplicate_writer:")
    )
    assert "sq_0104" in writer_line and "sq_0105" in writer_line
    scope = next(
        line
        for line in first.splitlines()
        if line.startswith("proposed_scope.queries:")
    )
    assert "sq_0104" in scope and "sq_0105" in scope
    assert "self_read: sq_0112" in first
    assert "missing_producer:" in first
    assert "cycle:" in first
    assert "lineage.upstream:" in first and "query sq_0101" in first
    assert "lineage.consumers:" in first and "looker/exec_overview" in first
    assert "not a migration decision" in first
    assert len(graph.nodes) == sum(node.kind == "query" for node in graph.nodes) + sum(
        node.kind != "query" for node in graph.nodes
    )
    assert sum(node.kind == "query" for node in graph.nodes) == len(inventory.queries)


def test_cli_writes_the_same_bytes_twice(capsys: pytest.CaptureFixture[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _synthetic(root)
        output = root / "out.txt"
        assert (
            main([str(root), "--table", "revenue_daily", "--output", str(output)]) == 0
        )
        first = capsys.readouterr().out
        assert output.read_text(encoding="utf-8") == first
        assert main([str(root), "-o", str(output)]) == 0
        second = capsys.readouterr().out
        assert first == second
        assert output.read_text(encoding="utf-8") == second


def test_missing_input_returns_2(caplog: pytest.LogCaptureFixture) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with caplog.at_level("ERROR"):
            code = main([tmp])
    assert code == 2
    assert any("missing" in record.message for record in caplog.records)


def test_invalid_argument_exits_2() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--not-a-flag"])
    assert caught.value.code == 2


def test_unknown_lineage_target_is_reported() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _synthetic(root)
        report = analyze(root, target="does_not_exist")
    assert "lineage.target: none" in report
    assert "lineage.requested: does_not_exist" in report
    assert "q_hourly WRITE_APPEND" in report
