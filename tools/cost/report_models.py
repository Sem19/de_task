"""Values the cost report is allowed to print.

The renderer formats these records. It does not price queries or choose
which configuration a scenario stops.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, NamedTuple

from cost.models import ConfigCost, EstateCost, SliceCost

ChangeClass = Literal["owner_confirmation", "business_logic"]
ScenarioKind = Literal["measured", "assumed"]
MoneyRole = Literal[
    "conditional_avoidable",
    "hypothetical",
    "assumed_replacement",
    "operational_cleanup",
]


class ScenarioMember(NamedTuple):
    """One configuration's contribution to a scenario."""

    config_id: str
    daily_savings_usd: Decimal
    detail: str


class Scenario(NamedTuple):
    """A priced what-if. Dollar amounts come from the cost engine.

    ``measured`` means the figure is the observed daily rate of queries that
    would stop, with no replacement scan added. ``assumed`` means the figure
    depends on a replacement or a schedule change that was not executed.
    """

    scenario_id: str
    title: str
    kind: ScenarioKind
    change_class: ChangeClass
    stops: tuple[str, ...]
    retains: tuple[str, ...]
    daily_savings_usd: Decimal | None
    members: tuple[ScenarioMember, ...]
    evidence: str
    assumption: str
    uncertainty: str
    money_role: MoneyRole


class PricedQuery(NamedTuple):
    """A query on a route, carrying the engine's daily figures."""

    config_id: str
    destination: str | None
    frequency: str
    daily_cost_usd: Decimal | None
    daily_tib: Decimal | None
    sql_path: str | None


class RouteCost(NamedTuple):
    """Observed daily cost of the scheduled queries on one lineage route."""

    table_name: str
    queries: tuple[PricedQuery, ...]
    daily_cost_usd: Decimal
    daily_tib: Decimal
    missing_rate_config_ids: tuple[str, ...]
    excluded_sibling_config_ids: tuple[str, ...]
    excluded_sibling_daily_cost_usd: Decimal


class FindingCost(NamedTuple):
    """Observed daily cost of the queries an audit finding names.

    This is context. It is not the saving from applying the finding.
    """

    finding_id: str
    priority: int
    confidence: str
    title: str
    config_ids: tuple[str, ...]
    daily_cost_usd: Decimal
    missing_rate_config_ids: tuple[str, ...]


class DriverReview(NamedTuple):
    """Evidence from SQL, the catalog, the schedule, and recorded executions.

    The text does not estimate a byte reduction.
    """

    config_id: str
    destination: str | None
    sql_path: str | None
    frequency: str
    schedule: str | None
    write_disposition: str | None
    daily_cost_usd: Decimal | None
    daily_tib: Decimal | None
    finding_ids: tuple[str, ...]
    processing_pattern: str
    scan_volume: str
    investigation: str
    risks: str
    verification: str


class Recommendation(NamedTuple):
    """One engineering next step. Dollar fields are observed or conditional.

    ``conditionally_avoidable_usd`` is set only when stopping the queries
    would remove their observed scan cost and no replacement scan is assumed.
    It is still conditional on an owner check. It is not a guaranteed saving.
    """

    rank: int
    heading: str
    config_ids: tuple[str, ...]
    observed_daily_cost_usd: Decimal | None
    conditionally_avoidable_usd: Decimal | None
    body: str


class CostAnalysis(NamedTuple):
    """Everything ``render_cost`` needs. Figures are already calculated."""

    source_name: str
    estate: EstateCost
    configs_with_daily_rate: int
    frequency: tuple[SliceCost, ...]
    enabled: tuple[SliceCost, ...]
    ranked_configs: tuple[ConfigCost, ...]
    top_configs: tuple[ConfigCost, ...]
    driver_reviews: tuple[DriverReview, ...]
    recommendations: tuple[Recommendation, ...]
    route: RouteCost
    scenarios: tuple[Scenario, ...]
    finding_costs: tuple[FindingCost, ...]
    configs_with_date_gaps: int
