"""Tests for the scheduled-query dependency graph."""

from __future__ import annotations

from pathlib import Path

import pytest

from estate.identifiers import TableId
from estate.models import CatalogRecord, Inventory, ScheduledQuery, UsageRecord
from graph import (
    build_graph,
    downstream,
    downstream_lineage,
    duplicate_writers,
    format_summary,
    migration_scope,
    missing_producers,
    self_reads,
    shared_source_siblings,
    strongly_connected,
    uncertain_reads,
    upstream,
)
from graph.models import query_id, table_id

_REPO = Path(__file__).resolve().parents[2]


def _query(
    config_id: str,
    dataset: str,
    table: str,
    sql: str,
    *,
    disposition: str = "WRITE_TRUNCATE",
) -> ScheduledQuery:
    return ScheduledQuery(
        config_id=config_id,
        sql_path=Path(f"{config_id}.sql"),
        sql_text=sql,
        params_query=sql,
        destination_dataset=dataset,
        destination_table=table,
        destination=TableId(None, dataset, table),
        schedule=None,
        timezone=None,
        state="ENABLED",
        disabled=False,
        enabled=True,
        write_disposition=disposition,
        transfer_name=None,
        meta_sql_file=None,
        filename_dataset=dataset,
        filename_table=table,
        raw_config={},
    )


def _inventory(
    queries: list[ScheduledQuery],
    *,
    usage: tuple[UsageRecord, ...] = (),
    catalog: tuple[CatalogRecord, ...] = (),
) -> Inventory:
    return Inventory(
        root=Path("."),
        sql_files=(),
        transfer_configs=(),
        queries=tuple(queries),
        history=(),
        usage=usage,
        catalog=catalog,
        schemas=(),
        findings=(),
    )


def _catalog(*names: str) -> tuple[CatalogRecord, ...]:
    records = []
    for name in names:
        dataset, table = name.split(".")
        records.append(
            CatalogRecord(
                raw={},
                table=TableId(None, dataset, table),
                writer_config_ids=(),
            )
        )
    return tuple(records)


def _usage(table: str, kind: str, consumer: str) -> UsageRecord:
    dataset, name = table.split(".")
    return UsageRecord(
        raw={"queries_30d": "4"},
        table=TableId(None, dataset, name),
        consumer_kind=kind,
        consumer_id=consumer,
        query=None,
        writer_config_ids=(),
    )


def test_linear_dependencies() -> None:
    graph = build_graph(
        _inventory(
            [
                _query("q1", "staging", "mid", "SELECT * FROM `raw.a`"),
                _query("q2", "reports", "out", "SELECT * FROM `staging.mid`"),
            ],
            catalog=_catalog("raw.a"),
        )
    )
    assert upstream(graph, table_id("reports.out")) == (
        query_id("q1"),
        query_id("q2"),
        table_id("raw.a"),
        table_id("staging.mid"),
    )
    assert downstream(graph, table_id("raw.a")) == (
        query_id("q1"),
        query_id("q2"),
        table_id("reports.out"),
        table_id("staging.mid"),
    )


def test_branched_dependencies() -> None:
    graph = build_graph(
        _inventory(
            [
                _query("q1", "reports", "left", "SELECT * FROM `raw.a`"),
                _query("q2", "reports", "right", "SELECT * FROM `raw.a`"),
            ],
            catalog=_catalog("raw.a"),
        )
    )
    assert set(downstream(graph, table_id("raw.a"))) == {
        query_id("q1"),
        query_id("q2"),
        table_id("reports.left"),
        table_id("reports.right"),
    }
    assert query_id("q2") not in upstream(graph, table_id("reports.left"))


def test_multiple_writers_stay_on_the_shared_destination() -> None:
    graph = build_graph(
        _inventory(
            [
                _query(
                    "q_daily", "reports", "revenue_daily", "SELECT * FROM `staging.src`"
                ),
                _query(
                    "q_hourly",
                    "reports",
                    "revenue_daily",
                    "SELECT * FROM `raw.payments`",
                    disposition="WRITE_APPEND",
                ),
                _query("q_src", "staging", "src", "SELECT * FROM `raw.payments`"),
            ],
            catalog=_catalog("raw.payments"),
        )
    )
    assert duplicate_writers(graph) == (
        (
            table_id("reports.revenue_daily"),
            (query_id("q_daily"), query_id("q_hourly")),
        ),
    )
    scope = set(migration_scope(graph))
    assert query_id("q_daily") in scope
    assert query_id("q_hourly") in scope


def test_independent_source_tables_do_not_connect() -> None:
    graph = build_graph(
        _inventory(
            [
                _query("q1", "reports", "one", "SELECT * FROM `raw.a`"),
                _query("q2", "reports", "two", "SELECT * FROM `raw.b`"),
            ],
            catalog=_catalog("raw.a", "raw.b"),
        )
    )
    assert table_id("raw.b") not in downstream(graph, table_id("raw.a"))
    assert table_id("raw.a") not in upstream(graph, table_id("reports.two"))
    sources = {
        graph.node(node_id).table.dataset_table
        for node_id in (table_id("raw.a"), table_id("raw.b"))
    }
    assert sources == {"raw.a", "raw.b"}
    assert not graph.node(table_id("raw.a")).produced
    assert graph.node(table_id("reports.one")).produced


def test_missing_reference_is_not_an_external_source() -> None:
    graph = build_graph(
        _inventory(
            [_query("q1", "reports", "out", "SELECT * FROM `staging.gone`")],
            catalog=_catalog("raw.kept"),
        )
    )
    assert missing_producers(graph) == (table_id("staging.gone"),)
    assert not graph.node(table_id("staging.gone")).in_catalog
    assert not graph.node(table_id("staging.gone")).produced


def test_uncertain_reference_is_kept_out_of_lineage() -> None:
    sql = """
    SELECT (1 -- `raw.commented`
    FROM `acme-analytics-prod.raw.kept`
    """
    graph = build_graph(
        _inventory(
            [_query("q1", "reports", "out", sql)],
            catalog=_catalog("raw.kept"),
        )
    )
    query = graph.node(query_id("q1"))
    assert query.parsed is False
    assert query.parse_errors
    assert uncertain_reads(graph) == (
        (
            table_id("raw.kept"),
            query_id("q1"),
            "reads",
            True,
            None,
        ),
    )
    assert downstream(graph, table_id("raw.kept")) == ()
    assert downstream(graph, table_id("raw.kept"), include_uncertain=True) == (
        query_id("q1"),
        table_id("reports.out"),
    )
    assert missing_producers(graph) == ()
    assert graph.node(table_id("raw.kept")).projects == ("acme-analytics-prod",)


def test_self_read_and_query_cycle() -> None:
    cycle = build_graph(
        _inventory(
            [
                _query("q1", "marts", "left", "SELECT * FROM `marts.right`"),
                _query("q2", "marts", "right", "SELECT * FROM `marts.left`"),
            ]
        )
    )
    assert strongly_connected(cycle) == (
        (
            query_id("q1"),
            query_id("q2"),
            table_id("marts.left"),
            table_id("marts.right"),
        ),
    )
    assert self_reads(cycle) == ()

    loop = build_graph(
        _inventory([_query("q1", "reports", "snap", "SELECT * FROM `reports.snap`")])
    )
    assert self_reads(loop) == (query_id("q1"),)
    assert strongly_connected(loop) == ((query_id("q1"), table_id("reports.snap")),)


def test_external_consumer_has_no_sql_dependencies() -> None:
    graph = build_graph(
        _inventory(
            [_query("q1", "reports", "out", "SELECT * FROM `raw.a`")],
            usage=(_usage("reports.out", "dashboard", "looker/exec"),),
            catalog=_catalog("raw.a"),
        )
    )
    consumer = "consumer:dashboard:looker/exec"
    assert graph.node(consumer).kind == "consumer"
    assert downstream(graph, consumer) == ()
    assert [edge.relation for edge in graph.edges if edge.target == consumer] == [
        "consumes"
    ]
    assert not any(edge.source == consumer for edge in graph.edges)


def test_upstream_and_downstream_are_sorted() -> None:
    graph = build_graph(
        _inventory(
            [
                _query("q_b", "staging", "b", "SELECT * FROM `raw.z`"),
                _query("q_a", "reports", "a", "SELECT * FROM `staging.b` JOIN `raw.m`"),
            ],
            catalog=_catalog("raw.m", "raw.z"),
        )
    )
    assert upstream(graph, table_id("reports.a")) == tuple(
        sorted(upstream(graph, table_id("reports.a")))
    )
    with pytest.raises(KeyError):
        upstream(graph, "query:missing")


def test_shared_source_is_not_a_migration_branch() -> None:
    graph = build_graph(
        _inventory(
            [
                _query("q_feed", "staging", "src", "SELECT * FROM `raw.events`"),
                _query(
                    "q_rev", "reports", "revenue_daily", "SELECT * FROM `staging.src`"
                ),
                _query(
                    "q_down",
                    "reports",
                    "child",
                    "SELECT * FROM `reports.revenue_daily`",
                ),
                _query(
                    "q_side",
                    "reports",
                    "other_child",
                    "SELECT * FROM `reports.revenue_daily`",
                ),
                _query("q_sibling", "other", "sibling", "SELECT * FROM `raw.events`"),
            ],
            usage=(_usage("reports.child", "adhoc", "analyst@acme.co"),),
            catalog=_catalog("raw.events"),
        )
    )
    scope = set(migration_scope(graph))
    lineage = set(downstream_lineage(graph))
    assert query_id("q_sibling") not in scope
    assert query_id("q_sibling") not in lineage
    assert query_id("q_sibling") in shared_source_siblings(
        graph, migration_scope(graph)
    )
    assert query_id("q_feed") in scope
    assert query_id("q_down") in lineage
    assert query_id("q_side") in lineage
    assert "consumer:adhoc:analyst@acme.co" in lineage
    assert "consumer:adhoc:analyst@acme.co" not in scope
    assert query_id("q_feed") not in lineage
    assert table_id("reports.revenue_daily_eur") not in scope


def test_table_can_be_read_and_written() -> None:
    graph = build_graph(
        _inventory(
            [
                _query("q1", "staging", "mid", "SELECT * FROM `raw.a`"),
                _query("q2", "reports", "out", "SELECT * FROM `staging.mid`"),
            ],
            catalog=_catalog("raw.a"),
        )
    )
    mid = graph.node(table_id("staging.mid"))
    assert mid.produced
    relations = {
        edge.relation
        for edge in graph.edges
        if edge.source == mid.id or edge.target == mid.id
    }
    assert relations == {"reads", "writes"}
    assert self_reads(graph) == ()


def test_sql_write_target_is_kept_beside_the_config_destination() -> None:
    graph = build_graph(
        _inventory(
            [
                _query(
                    "q1",
                    "reports",
                    "configured",
                    "INSERT INTO `marts.extra` SELECT * FROM `raw.a`",
                )
            ],
            catalog=_catalog("raw.a"),
        )
    )
    writes = {
        edge.target
        for edge in graph.edges
        if edge.relation == "writes" and edge.source == query_id("q1")
    }
    assert writes == {table_id("marts.extra"), table_id("reports.configured")}


def test_graph_output_is_deterministic() -> None:
    inventory = _inventory(
        [
            _query("q2", "reports", "out", "SELECT * FROM `staging.mid`"),
            _query("q1", "staging", "mid", "SELECT * FROM `raw.a`"),
        ],
        catalog=_catalog("raw.a"),
    )
    first = build_graph(inventory)
    second = build_graph(inventory)
    assert first == second
    assert format_summary(first) == format_summary(second)
    assert [node.id for node in first.nodes] == sorted(node.id for node in first.nodes)


def test_repository_route_excludes_shared_source_siblings() -> None:
    from estate import load_inventory

    root = _REPO / "task2_estate"
    before = {
        path: path.read_bytes() for path in (root / "scheduled_queries").glob("*.sql")
    }
    inventory = load_inventory(root)
    first = build_graph(inventory)
    second = build_graph(inventory)
    assert first == second
    assert before == {path: path.read_bytes() for path in before}

    targets = [
        node
        for node in first.nodes
        if node.kind == "table"
        and node.table is not None
        and node.table.table == "revenue_daily"
    ]
    assert targets
    assert all(
        node.table is not None and node.table.dataset == "reports" for node in targets
    )
    scope = migration_scope(first)
    lineage = downstream_lineage(first)
    siblings = shared_source_siblings(first, scope)
    assert siblings
    assert set(siblings).isdisjoint(scope)
    assert set(siblings).isdisjoint(lineage)
    assert any(node_id.startswith("consumer:") for node_id in lineage)
    assert not any(node_id.startswith("consumer:") for node_id in scope)
    assert set(lineage) != set(scope)
    producers = {
        node.id
        for node in first.nodes
        if node.kind == "table"
        and node.produced
        and node.table is not None
        and node.table.table != "revenue_daily"
    }
    assert any(
        node.table is not None and node.table.table.startswith("revenue_daily_")
        for node in first.nodes
        if node.kind == "table" and node.table is not None
    )
    prefixed = {
        node.id
        for node in first.nodes
        if node.kind == "table"
        and node.table is not None
        and node.table.table.startswith("revenue_daily_")
    }
    assert prefixed.isdisjoint(scope)
    assert duplicate_writers(first)
    assert self_reads(first)
    assert strongly_connected(first)
    assert missing_producers(first)
    assert uncertain_reads(first)
    assert producers
