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
    """One configuration's contribution to a scenario.

    Attributes:
        config_id: Config that contributes this amount.
        daily_savings_usd: Its published contribution. Unpriced configs are
            omitted from the member list rather than stored here as ``None``.
        detail: Short evidence fragment printed after the amount.
    """

    config_id: str
    daily_savings_usd: Decimal
    detail: str


class Scenario(NamedTuple):
    """A priced what-if. Dollar amounts come from the cost engine.

    ``measured`` means the figure is the observed daily rate of queries that
    would stop, with no replacement scan added. ``assumed`` means the figure
    depends on a replacement or a schedule change that was not executed.

    Attributes:
        scenario_id: Stable id.
        title: One-line title.
        kind: ``measured`` or ``assumed``.
        change_class: ``owner_confirmation`` or ``business_logic``.
        stops: Config ids that would stop.
        retains: Config ids that would remain. Empty when nothing is kept.
        daily_savings_usd: Sum of the priced members, or ``None`` when none
            of the stopped configs has a rate.
        members: Priced contributions. Unpriced stopped configs are absent.
        evidence: What the files show.
        assumption: What the dollar figure depends on.
        uncertainty: Limits on that figure.
        money_role: How the report may describe the amount:
            ``conditional_avoidable``, ``hypothetical``,
            ``assumed_replacement``, or ``operational_cleanup``.
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
    """A query on a route, carrying the engine's daily figures.

    Attributes:
        config_id: Scheduled-query id.
        destination: ``dataset.table``, or ``None``.
        frequency: ``hourly``, ``daily``, or ``other``.
        daily_cost_usd: Normalized daily run-rate, or ``None``.
        daily_tib: Normalized daily scanned TiB, or ``None``.
        sql_path: SQL path, or ``None``.
    """

    config_id: str
    destination: str | None
    frequency: str
    daily_cost_usd: Decimal | None
    daily_tib: Decimal | None
    sql_path: str | None


class RouteCost(NamedTuple):
    """Observed daily cost of the scheduled queries on one lineage route.

    Attributes:
        table_name: Exact table name the route was walked from.
        queries: Scheduled queries on the route, unpriced ones last.
        daily_cost_usd: Sum of published daily rates. Zero when none are published.
        daily_tib: Sum of published scanned-TiB rates.
        missing_rate_config_ids: Route queries whose daily rate is ``None``.
        excluded_sibling_config_ids: Queries that only share an upstream source.
        excluded_sibling_daily_cost_usd: Sum of those siblings' published rates.
    """

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

    Attributes:
        finding_id: Audit finding id.
        priority: Audit priority. ``1`` is the highest.
        confidence: Audit confidence label.
        title: Audit title.
        config_ids: Config ids the finding names, in the finding's order.
        daily_cost_usd: Sum of the published rates among those ids. Zero
            when none of them has a rate.
        missing_rate_config_ids: Cited ids that are absent from the cost
            engine or have no daily rate.
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

    Attributes:
        config_id: Reviewed config.
        destination: ``dataset.table``, or ``None``.
        sql_path: SQL path, or ``None``.
        frequency: ``hourly``, ``daily``, or ``other``.
        schedule: Transfer schedule, or ``None``.
        write_disposition: Stored disposition, or ``None`` when the query
            is unpaired or the field is unset.
        daily_cost_usd: Normalized daily run-rate, or ``None``.
        daily_tib: Normalized daily scanned TiB, or ``None``.
        finding_ids: Audit ids that name this config, in finding order.
        processing_pattern: Schedule, destination, counts, and comments.
        scan_volume: Reads, partition filters, and statement shape.
        investigation: Checks to run. Not a measured saving.
        risks: How a change can affect published rows.
        verification: The BigQuery comparison to run.
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

    Attributes:
        rank: Position in the printed list, starting at 1.
        heading: One-line next step.
        config_ids: Configs the step names. Empty when the step is about
            a class of scenarios rather than one list of configs.
        observed_daily_cost_usd: Observed daily rate of those configs, or
            ``None`` when the step has no single observed amount.
        conditionally_avoidable_usd: That rate when stopping the queries
            would remove it and no replacement scan is assumed. ``None``
            for every other step.
        body: The evidence and the limit on the action.
    """

    rank: int
    heading: str
    config_ids: tuple[str, ...]
    observed_daily_cost_usd: Decimal | None
    conditionally_avoidable_usd: Decimal | None
    body: str


class CostAnalysis(NamedTuple):
    """Everything ``render_cost`` needs. Figures are already calculated.

    Attributes:
        source_name: Directory name printed in the header.
        estate: Engine totals for this inventory.
        configs_with_daily_rate: Configs whose daily run-rate is not ``None``.
        frequency: Frequency slices, in engine order.
        enabled: Enabled-state slices, in engine order.
        ranked_configs: Every config, unpriced after priced.
        top_configs: At most ten priced configs from the front of that ranking.
        driver_reviews: At most four of those top configs.
        recommendations: Numbered next steps.
        route: Daily cost of the lineage route.
        scenarios: Priced what-ifs, unmeasured last, then descending savings.
        finding_costs: Associated spend, descending daily cost.
        configs_with_date_gaps: Configs whose observed schedule dates are
            not contiguous.
    """

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
