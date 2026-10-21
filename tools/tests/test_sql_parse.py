"""Tests for BigQuery SQL table extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from estate import load_inventory
from sql import format_issue, parse_inventory, parse_sql

_REPO = Path(__file__).resolve().parents[2]


def _names(sql: str, role: str, *, uncertain: bool = False) -> list[str]:
    result = parse_sql(sql)
    return [
        ref.table.dataset_table
        for ref in result.references
        if ref.role == role and ref.uncertain is uncertain
    ]


def test_select_and_join() -> None:
    result = parse_sql(
        """
        SELECT a.id, b.user_id
        FROM `raw.payments` AS a
        JOIN `raw.users` b ON a.user_id = b.user_id
        """
    )
    assert result.parsed
    assert result.errors == ()
    assert _names(
        """
        SELECT a.id, b.user_id
        FROM `raw.payments` AS a
        JOIN `raw.users` b ON a.user_id = b.user_id
        """,
        "read",
    ) == ["raw.payments", "raw.users"]
    assert result.writes == ()
    assert "a" not in {ref.raw for ref in result.references}
    assert "b" not in {ref.raw for ref in result.references}


def test_cte_shadowing_and_nested_ctes() -> None:
    sql = """
    WITH events AS (
      SELECT * FROM `raw.events`
    ),
    outer_q AS (
      WITH events AS (
        SELECT 1 AS x
      )
      SELECT x FROM events
    )
    SELECT e.x
    FROM events e
    JOIN outer_q q ON TRUE
    JOIN `raw.users` u ON TRUE
    """
    result = parse_sql(sql)
    assert result.parsed
    assert [ref.table.dataset_table for ref in result.reads] == [
        "raw.events",
        "raw.users",
    ]
    assert result.unresolved == ()


def test_subquery_and_alias_are_not_tables() -> None:
    result = parse_sql(
        """
        SELECT src.user_id
        FROM (
          SELECT user_id FROM `raw.users` u
        ) src
        WHERE user_id IN (SELECT user_id FROM `raw.subscriptions` s)
        """
    )
    assert [ref.table.dataset_table for ref in result.reads] == [
        "raw.subscriptions",
        "raw.users",
    ]
    assert result.unresolved == ()


def test_quoted_project_dataset_table() -> None:
    result = parse_sql("SELECT * FROM `acme-analytics-prod.raw.payments`")
    ref = result.reads[0]
    assert ref.table.project == "acme-analytics-prod"
    assert ref.table.dataset_table == "raw.payments"
    assert ref.table.fully_qualified == "acme-analytics-prod.raw.payments"
    assert ref.raw == "acme-analytics-prod.raw.payments"


def test_multiple_statements_keep_statement_index() -> None:
    result = parse_sql(
        """
        SELECT 1 FROM `raw.b`;
        INSERT INTO `staging.out` SELECT * FROM `raw.a`;
        """
    )
    assert result.parsed
    assert [
        (ref.statement_index, ref.role, ref.table.dataset_table)
        for ref in result.references
    ] == [
        (0, "read", "raw.b"),
        (1, "read", "raw.a"),
        (1, "write", "staging.out"),
    ]


def test_dml_and_ddl_split_sources_from_targets() -> None:
    result = parse_sql(
        """
        MERGE `marts.target` T
        USING `raw.source` S
        ON T.id = S.id
        WHEN MATCHED THEN UPDATE SET T.x = S.x;

        UPDATE `marts.target` SET x = 1
        WHERE id IN (SELECT id FROM `raw.other`);

        DELETE FROM `marts.target` WHERE id IN (SELECT id FROM `raw.gone`);

        CREATE TABLE `marts.copy` LIKE `raw.template`;
        DROP TABLE `marts.old`;
        """
    )
    assert result.parsed
    reads = {(ref.statement_index, ref.table.dataset_table) for ref in result.reads}
    writes = {(ref.statement_index, ref.table.dataset_table) for ref in result.writes}
    assert reads == {
        (0, "raw.source"),
        (1, "raw.other"),
        (2, "raw.gone"),
        (3, "raw.template"),
    }
    assert writes == {
        (0, "marts.target"),
        (1, "marts.target"),
        (2, "marts.target"),
        (3, "marts.copy"),
        (4, "marts.old"),
    }


def test_column_names_are_not_table_references() -> None:
    result = parse_sql(
        """
        SELECT payments, users.user_id, raw.missing.amount
        FROM `raw.users` users
        """
    )
    assert [ref.table.dataset_table for ref in result.reads] == ["raw.users"]
    assert all(item.raw != "payments" for item in result.unresolved)


def test_malformed_sql_is_a_result_with_context() -> None:
    sql = """
    SELECT SUM(amount * 0.92  -- rate)
    FROM `acme-analytics-prod.raw.payments` p
    JOIN `staging.stg_subscriptions` s ON s.id = p.id
    """
    result = parse_sql(sql, config_id="sq_example", path=Path("sq_example.sql"))
    assert not result.parsed
    assert result.errors
    assert result.errors[0].line is not None
    assert all(ref.uncertain and ref.role == "unknown" for ref in result.references)
    assert {ref.table.dataset_table for ref in result.references} == {
        "raw.payments",
        "staging.stg_subscriptions",
    }
    payments = next(ref for ref in result.references if ref.table.table == "payments")
    assert payments.table.project == "acme-analytics-prod"
    text = format_issue(result, result.errors[0])
    assert text.startswith("sq_example:")
    assert "Expecting )" in text


def test_fallback_ignores_commented_and_unqualified_names() -> None:
    sql = """
    SELECT (1 -- `raw.commented`
    FROM `raw.kept`
    JOIN bare_name ON TRUE
    """
    result = parse_sql(sql)
    assert not result.parsed
    assert [ref.table.dataset_table for ref in result.references] == ["raw.kept"]
    assert result.unresolved == ()


def test_table_function_and_dynamic_sql_are_conservative() -> None:
    result = parse_sql(
        """
        EXECUTE IMMEDIATE 'SELECT * FROM `raw.events`';
        SELECT * FROM `raw.fx_rates` fx
        CROSS JOIN UNNEST([1, 2]) AS n
        CROSS JOIN raw.my_func(1);
        """
    )
    assert result.parsed
    assert [ref.table.dataset_table for ref in result.reads] == ["raw.fx_rates"]
    uncertain = [ref for ref in result.references if ref.uncertain]
    assert [ref.table.dataset_table for ref in uncertain] == ["raw.events"]
    reasons = {item.reason for item in result.unresolved}
    assert "dynamic" in reasons
    assert "table_function" in reasons
    assert all(item.raw != "n" for item in result.unresolved)


def test_references_are_ordered_and_stable() -> None:
    sql = "SELECT * FROM `raw.z` z JOIN `raw.a` a ON TRUE"
    first = parse_sql(sql, config_id="sq_1")
    second = parse_sql(sql, config_id="sq_1")
    assert first == second
    assert [ref.table.table for ref in first.reads] == ["a", "z"]


def test_parse_does_not_modify_the_sql_string() -> None:
    sql = "SELECT * FROM `raw.t` -- tail\n"
    snapshot = sql
    parse_sql(sql)
    assert sql == snapshot


def test_repository_sql_parses_without_hardcoded_ids() -> None:
    inventory = load_inventory(_REPO / "task2_estate")
    before = {
        query.sql_path: query.sql_path.read_bytes() for query in inventory.queries
    }
    first = parse_inventory(inventory)
    second = parse_inventory(inventory)
    assert len(first) == len(inventory.queries)
    assert [item.config_id for item in first] == [
        query.config_id for query in inventory.queries
    ]
    assert first == second

    failed = [item for item in first if not item.parsed]
    parsed = [item for item in first if item.parsed]
    assert failed
    assert len(parsed) + len(failed) == len(first)
    for item in failed:
        assert item.errors
        assert all(ref.uncertain and ref.role == "unknown" for ref in item.references)
        assert format_issue(item, item.errors[0]).startswith(f"{item.config_id}:")
    for item, query in zip(first, inventory.queries, strict=True):
        for ref in item.references:
            assert ref.raw in query.sql_text
        if item.parsed:
            assert all(
                not ref.uncertain or ref.role == "unknown" for ref in item.references
            )
    assert before == {
        query.sql_path: query.sql_path.read_bytes() for query in inventory.queries
    }


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM `dataset.table`", ["dataset.table"]),
        ("SELECT * FROM `p.dataset.table`", ["dataset.table"]),
    ],
)
def test_normalized_dataset_table(sql: str, expected: list[str]) -> None:
    assert [ref.table.dataset_table for ref in parse_sql(sql).reads] == expected
