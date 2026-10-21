"""Tests for audit detection and AUDIT.txt rendering."""

from __future__ import annotations

from pathlib import Path

from audit import collect_findings, render_audit
from audit_report import build_report
from estate import load_inventory
from graph import build_graph
from sql import parse_inventory

_REPO = Path(__file__).resolve().parents[2]
_ESTATE = _REPO / "task2_estate"


def _loaded():
    inventory = load_inventory(_ESTATE)
    parses = parse_inventory(inventory)
    graph = build_graph(inventory, parses)
    findings = collect_findings(inventory, parses, graph)
    return inventory, parses, graph, findings


def test_real_estate_findings_are_stable_and_traceable() -> None:
    inventory, parses, graph, first = _loaded()
    second = collect_findings(inventory, parses, graph)
    assert first == second
    order = {"operational": 0, "correctness": 1, "maintainability": 2}
    assert [item.finding_id for item in first] == [
        item.finding_id
        for item in sorted(
            first,
            key=lambda item: (order[item.category], item.priority, item.finding_id),
        )
    ]
    ids = {query.config_id for query in inventory.queries}
    paths = {query.sql_path.name for query in inventory.queries}
    assert len(ids) == len(inventory.queries)
    for finding in first:
        assert finding.config_ids
        assert finding.evidence
        assert finding.recommendation
        assert set(finding.config_ids) <= ids
        for path in finding.sql_paths:
            name = Path(path).name
            assert name in paths
            assert (_ESTATE / "scheduled_queries" / name).is_file()
    text = render_audit(inventory, parses, graph, first)
    assert text == render_audit(inventory, parses, graph, second)
    assert f"Queries considered: {len(inventory.queries)}" in text
    assert "uncertain" in text.lower()
    assert "needs_verification" in text
    assert "not a decision" in text
    assert "Executive summary" in text
    assert "not proven production defects" in text


def test_high_impact_findings_match_the_source_files() -> None:
    inventory, _parses, _graph, findings = _loaded()
    by_id = {item.finding_id: item for item in findings}

    writers = by_id["OP-DUPLICATE-WRITER-sq_0104-sq_0105"]
    assert writers.confidence == "confirmed"
    assert writers.config_ids == ("sq_0104", "sq_0105")
    destinations = {
        query.destination.dataset_table
        for query in inventory.queries
        if query.config_id in writers.config_ids and query.destination is not None
    }
    assert destinations == {"reports.revenue_daily"}

    broken = by_id["CR-UNPARSEABLE-SQL"]
    assert "sq_0107" in broken.config_ids
    sql = next(
        query.sql_text for query in inventory.queries if query.config_id == "sq_0107"
    )
    assert "0.92" in sql and "--" in sql
    assert "uncertain" in broken.evidence.lower()

    missing = by_id["OP-NOT-FOUND-raw-app_versions"]
    assert missing.confidence == "confirmed"
    assert "sq_5013" in missing.config_ids
    assert "does not prove the table is still missing" in missing.evidence
    catalog_gap = by_id["OP-CATALOG-GAP-raw-app_versions"]
    assert catalog_gap.confidence == "needs_verification"
    assert "does not prove the table is absent from production" in catalog_gap.evidence
    assert any(
        (row.raw.get("error_message") or "").startswith("Not found")
        for row in inventory.history
        if row.config_id == "sq_5013"
    )

    lookback = by_id["CR-SHORT-LOOKBACK-sq_0104"]
    text_0104 = next(
        query.sql_text for query in inventory.queries if query.config_id == "sq_0104"
    )
    text_0106 = next(
        query.sql_text for query in inventory.queries if query.config_id == "sq_0106"
    )
    assert "INTERVAL 3 DAY" in text_0104
    assert "INTERVAL 400 DAY" in text_0106
    assert "sq_0106" in lookback.config_ids

    cycle = by_id["OP-CYCLE-sq_0201-sq_0202"]
    assert {"sq_0201", "sq_0202"} <= set(cycle.config_ids)
    spend = next(
        query.sql_text for query in inventory.queries if query.config_id == "sq_0201"
    )
    assert "marketing.spend_enriched" in spend


def test_retirement_candidates_have_no_recorded_consumer() -> None:
    inventory, _parses, graph, findings = _loaded()
    retire = next(
        item for item in findings if item.finding_id == "MN-RETIRE-NO-RECORDED-CONSUMER"
    )
    assert retire.confidence == "needs_verification"
    assert "sq_5019" not in retire.config_ids
    by_id = {query.config_id: query for query in inventory.queries}
    read_tables = {
        edge.source.removeprefix("table:")
        for edge in graph.edges
        if edge.relation == "reads"
    }
    for config_id in retire.config_ids:
        query = by_id[config_id]
        assert query.enabled is True
        assert query.destination is not None
        table = query.destination.dataset_table
        assert table not in read_tables
        assert not any(
            row.table is not None and row.table.dataset_table == table
            for row in inventory.usage
        )
    text = render_audit(inventory, parse_inventory(inventory), graph, findings)
    assert "cannot be disabled automatically" in text
    assert "does not recommend deletion" in text


def test_report_file_is_deterministic_and_leaves_inputs_unchanged(
    tmp_path: Path,
) -> None:
    before = {path: path.read_bytes() for path in _ESTATE.rglob("*") if path.is_file()}
    first = build_report(_ESTATE)
    second = build_report(_ESTATE)
    assert first == second
    output = tmp_path / "AUDIT.txt"
    output.write_text(first, encoding="utf-8")
    assert output.read_text(encoding="utf-8") == second
    assert before == {
        path: path.read_bytes() for path in _ESTATE.rglob("*") if path.is_file()
    }
