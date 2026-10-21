"""Static checks for the revenue_daily Dataform models.

Compares each SQLX body with the scheduled query it came from. ${ref()} is
the only intended substitution. sqlglot normalizes both sides so formatting
is not the comparison.
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlglot

_REPO = Path(__file__).resolve().parents[2]
_DATAFORM = _REPO / "task2_estate" / "dataform"
_SQL = _REPO / "task2_estate" / "scheduled_queries"

_REFS = {
    "payments": "`acme-analytics-prod.raw.payments`",
    "subscriptions": "`acme-analytics-prod.raw.subscriptions`",
    "fx_rates": "`acme-analytics-prod.raw.fx_rates`",
    "stg_payments": "`acme-analytics-prod.staging.stg_payments`",
    "stg_subscriptions": "`acme-analytics-prod.staging.stg_subscriptions`",
    "stg_fx_daily": "`acme-analytics-prod.staging.stg_fx_daily`",
    "revenue_daily": "`acme-analytics-prod.reports.revenue_daily`",
}

_TABLES = {
    "stg_payments": ("staging", "sq_0101__staging__stg_payments.sql", ("payments",)),
    "stg_subscriptions": (
        "staging",
        "sq_0102__staging__stg_subscriptions.sql",
        ("subscriptions",),
    ),
    "stg_fx_daily": (
        "staging",
        "sq_0103__staging__stg_fx_daily.sql",
        ("fx_rates", "fx_rates"),
    ),
    "revenue_daily": (
        "reports",
        "sq_0104__reports__revenue_daily.sql",
        ("stg_payments", "stg_subscriptions", "stg_fx_daily"),
    ),
    "revenue_by_region": (
        "reports",
        "sq_0106__reports__revenue_by_region.sql",
        ("revenue_daily",),
    ),
    "revenue_unified": (
        "marts",
        "sq_5001__marts__revenue_unified.sql",
        ("revenue_daily",),
    ),
}

_CONFIG = re.compile(r"config\s*\{(?P<body>.*?)\n\}", re.DOTALL)
_REF = re.compile(r"\$\{ref\(\"([^\"]+)\"\)\}")


def _sqlx_files() -> list[Path]:
    return sorted((_DATAFORM / "definitions").rglob("*.sqlx"))


def _config(text: str) -> dict[str, str]:
    body = _CONFIG.search(text)
    assert body is not None
    fields = dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', body.group("body")))
    tags = re.search(r"tags\s*:\s*\[([^\]]*)\]", body.group("body"))
    assert tags is not None
    fields["tags"] = tags.group(1)
    return fields


def _body(text: str) -> str:
    match = _CONFIG.search(text)
    assert match is not None
    return text[match.end() :].lstrip("\n")


def _restore_refs(sql: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        assert name in _REFS
        return _REFS[name]

    return _REF.sub(replace, sql)


def _normalized(sql: str) -> str:
    return sqlglot.parse_one(sql, read="bigquery").sql(
        dialect="bigquery", normalize=True
    )


def test_slice_has_three_declarations_and_six_tables() -> None:
    declaration = (_DATAFORM / "definitions" / "sources" / "declaration.js").read_text()
    names = re.findall(r'name:\s*"([^"]+)"', declaration)
    schemas = re.findall(r'schema:\s*"([^"]+)"', declaration)
    assert names == ["payments", "subscriptions", "fx_rates"]
    assert schemas == ["raw", "raw", "raw"]
    assert sorted(path.stem for path in _sqlx_files()) == sorted(_TABLES)


def test_each_model_is_a_tagged_table_with_the_planned_refs() -> None:
    for path in _sqlx_files():
        text = path.read_text()
        config = _config(text)
        schema, _source, refs = _TABLES[path.stem]
        assert config["type"] == "table"
        assert config["schema"] == schema
        assert config["name"] == path.stem
        assert "revenue_daily" in config["tags"]
        assert "incremental" not in text
        assert tuple(_REF.findall(text)) == refs


def test_revenue_sql_matches_the_scheduled_query_after_ref_substitution() -> None:
    for name, (schema, source, _refs) in _TABLES.items():
        if schema == "staging":
            continue
        sqlx = (_DATAFORM / "definitions" / schema / f"{name}.sqlx").read_text()
        original = (_SQL / source).read_text()
        restored = _restore_refs(_body(sqlx))
        assert _normalized(restored) == _normalized(original)


def test_revenue_daily_keeps_the_original_windows_and_has_one_writer() -> None:
    daily = (_DATAFORM / "definitions" / "reports" / "revenue_daily.sqlx").read_text()
    by_region = (
        _DATAFORM / "definitions" / "reports" / "revenue_by_region.sqlx"
    ).read_text()
    unified = (_DATAFORM / "definitions" / "marts" / "revenue_unified.sqlx").read_text()
    assert "INTERVAL 3 DAY" in daily
    assert "INTERVAL 400 DAY" in by_region
    assert "INTERVAL 730 DAY" in unified
    assert "payment_date AS report_date" in by_region
    assert "payment_date AS report_date" in unified
    assert "* EXCEPT (payment_date, region)" in by_region
    assert "* EXCEPT (payment_date, region)" in unified
    definitions = "\n".join(path.read_text() for path in _sqlx_files())
    assert "attempt_hour" not in definitions
    assert "sq_0105" not in definitions
    assert definitions.count('name: "revenue_daily"') == 1


def test_model_refs_do_not_cycle() -> None:
    graph = {name: refs for name, (_schema, _source, refs) in _TABLES.items()}
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> None:
        assert node not in visiting
        if node in visited:
            return
        visiting.add(node)
        for parent in graph.get(node, ()):
            if parent in graph:
                walk(parent)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        walk(node)
