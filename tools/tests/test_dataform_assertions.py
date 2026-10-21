"""Static checks for the four revenue_daily uniqueness assertions."""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DATAFORM = _REPO / "task2_estate" / "dataform"
_ASSERTIONS = _DATAFORM / "definitions" / "assertions"

_KEYS = {
    "assert_stg_payments_payment_id": ("stg_payments", ("payment_id",)),
    "assert_stg_subscriptions_subscription_id": (
        "stg_subscriptions",
        ("subscription_id",),
    ),
    "assert_stg_fx_daily_rate_date_currency": (
        "stg_fx_daily",
        ("rate_date", "currency"),
    ),
    "assert_revenue_daily_grain": (
        "revenue_daily",
        ("payment_date", "country_code", "region", "tier", "billing_period_days"),
    ),
}

_CONFIG = re.compile(r"config\s*\{(?P<body>.*?)\n\}", re.DOTALL)
_REF = re.compile(r"\$\{ref\(\"([^\"]+)\"\)\}")
_GROUP = re.compile(r"GROUP BY\s+(.*?)HAVING", re.DOTALL | re.IGNORECASE)


def _config(text: str) -> dict[str, str]:
    body = _CONFIG.search(text)
    assert body is not None
    fields = dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', body.group("body")))
    tags = re.search(r"tags\s*:\s*\[([^\]]*)\]", body.group("body"))
    assert tags is not None
    fields["tags"] = tags.group(1)
    return fields


def _group_columns(text: str) -> tuple[str, ...]:
    match = _GROUP.search(text)
    assert match is not None
    columns = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group(1))
    return tuple(columns)


def test_four_named_assertions_and_thirteen_actions() -> None:
    files = sorted(_ASSERTIONS.glob("*.sqlx"))
    assert [path.stem for path in files] == sorted(_KEYS)
    declaration = (_DATAFORM / "definitions" / "sources" / "declaration.js").read_text()
    declarations = len(re.findall(r"^declare\s*\(", declaration, re.MULTILINE))
    tables = [
        path
        for path in (_DATAFORM / "definitions").rglob("*.sqlx")
        if 'type: "table"' in path.read_text()
    ]
    assertions = [
        path
        for path in (_DATAFORM / "definitions").rglob("*.sqlx")
        if 'type: "assertion"' in path.read_text()
    ]
    assert declarations == 3
    assert len(tables) == 6
    assert len(assertions) == 4
    assert declarations + len(tables) + len(assertions) == 13


def test_assertion_keys_dependencies_and_null_grouping() -> None:
    for path in sorted(_ASSERTIONS.glob("*.sqlx")):
        text = path.read_text()
        config = _config(text)
        target, keys = _KEYS[path.stem]
        assert config["type"] == "assertion"
        assert config["schema"] == "dataform_assertions"
        assert config["name"] == path.stem
        assert "revenue_daily" in config["tags"]
        assert _REF.findall(text) == [target]
        assert _group_columns(text) == keys
        assert "HAVING COUNT(*) > 1" in text
        assert "IS NULL" not in text.upper()
        assert "IS NOT NULL" not in text.upper()
        assert "nonNull" not in text
        assert "uniqueKey" not in text


def test_assertions_do_not_add_rate_checks_or_blocked_models() -> None:
    fx = (_ASSERTIONS / "assert_stg_fx_daily_rate_date_currency.sqlx").read_text()
    assert "rate_to_usd" not in fx
    assert "last_known_rate" not in fx
    definitions = "\n".join(
        path.read_text() for path in (_DATAFORM / "definitions").rglob("*.sqlx")
    )
    for blocked in (
        "exec_kpi_daily",
        "kpi_snapshot",
        "exec_kpi_v2",
        "sq_0111",
        "sq_0112",
        "sq_5005",
        "sq_0105",
        "attempt_hour",
    ):
        assert blocked not in definitions
    assert definitions.count('name: "revenue_daily"') == 1


def test_assertion_dependencies_point_at_tables_and_do_not_cycle() -> None:
    tables = {
        "stg_payments": ("payments",),
        "stg_subscriptions": ("subscriptions",),
        "stg_fx_daily": ("fx_rates",),
        "revenue_daily": ("stg_payments", "stg_subscriptions", "stg_fx_daily"),
        "revenue_by_region": ("revenue_daily",),
        "revenue_unified": ("revenue_daily",),
    }
    graph = dict(tables)
    for name, (target, _keys) in _KEYS.items():
        graph[name] = (target,)
        assert target in tables

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
    assert set(_KEYS) <= visited
