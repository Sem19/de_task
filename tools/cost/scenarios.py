"""Price savings scenarios from the cost engine, the graph, and the audit.

No estate total, config id, or dollar amount is fixed here. A scenario's
money is a sum of ``ConfigCost`` rates, or it is left unmeasured.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Context, Decimal, localcontext

from audit.common import consumers, table_name as dataset_table
from audit.models import AuditFinding
from cost.calculate import CALCULATION_PRECISION, divide, sum_published
from cost.models import ConfigCost, EstateCost
from cost.report_models import (
    ChangeClass,
    CostAnalysis,
    FindingCost,
    PricedQuery,
    RouteCost,
    Scenario,
    ScenarioKind,
    ScenarioMember,
)
from estate.models import Inventory, ScheduledQuery
from graph.analyze import (
    REVENUE_DAILY_TABLE,
    duplicate_writers,
    migration_scope,
    shared_source_siblings,
)
from graph.models import Graph

_WIDE = Context(prec=CALCULATION_PRECISION + 20)
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_SPACE = re.compile(r"\s+")

_NO_CONSUMER_UNCERTAINTY = (
    "A usage extract can miss a reader. The amount is the observed daily rate "
    "of the listed queries and assumes no replacement scan. Do not disable "
    "them from this report alone, and do not delete the tables."
)
_WRITER_UNCERTAINTY = (
    "The retained query is the lowest observed daily rate. config_id breaks "
    "ties. The stopped query may project different columns, use a different "
    "write disposition, or cover a different window. Whether the retained "
    "query can replace it was not measured. Stopping a writer does not delete "
    "the table."
)
_REPEATED_UNCERTAINTY = (
    "Identical SQL text can write different tables. The retained copy is the "
    "cheapest copy that has a recorded consumer, or the cheapest copy when "
    "none do. config_id breaks ties. Whether one destination can replace the "
    "others was not measured."
)
_HOURLY_UNCERTAINTY = (
    "Each omitted execution is assumed to scan the same bytes as the "
    "execution that would remain. A single daily run of an incremental query "
    "can scan more than one hourly run. That replacement scan was not "
    "measured. This is not a forecast of savings from changing the schedule."
)


def build_cost_analysis(
    inventory: Inventory,
    estate: EstateCost,
    graph: Graph,
    findings: tuple[AuditFinding, ...],
    *,
    source_name: str,
    table_name: str = REVENUE_DAILY_TABLE,
) -> CostAnalysis:
    """Build the figures and scenarios the cost report renders.

    Args:
        inventory: Loaded estate. Used for SQL text, usage, and schedules.
        estate: Output of ``calculate_estate`` for the same inventory.
        graph: Dependency graph for the same inventory.
        findings: Audit findings. Their query costs are attached, not re-decided.
        source_name: Directory name recorded in the report.
        table_name: Exact table name of the lineage route. Defaults to
            ``revenue_daily``.

    Returns:
        Calculated report inputs, with scenarios ordered by daily savings
        descending and then scenario id.
    """
    by_config = {config.config_id: config for config in estate.configs}
    queries = {query.config_id: query for query in inventory.queries}
    readers = {
        dataset_table(edge.source) for edge in graph.edges if edge.relation == "reads"
    }
    scenarios = [
        scenario
        for scenario in (
            _no_consumer_scenario(inventory, by_config, readers),
            _zero_scan_scenario(by_config),
            _hourly_scenario(by_config),
        )
        if scenario is not None
    ]
    scenarios.extend(_writer_scenarios(graph, by_config, queries))
    scenarios.extend(_repeated_scenarios(queries, by_config, inventory, readers))
    ordered = tuple(sorted(scenarios, key=_scenario_key))
    return CostAnalysis(
        source_name=source_name,
        estate=estate,
        configs_with_daily_rate=sum(
            config.normalized_daily_run_rate_usd is not None
            for config in estate.configs
        ),
        frequency=estate.by_frequency,
        enabled=estate.by_enabled,
        ranked_configs=estate.configs,
        route=_route_cost(graph, by_config, table_name),
        scenarios=ordered,
        finding_costs=_finding_costs(findings, by_config),
        configs_with_date_gaps=sum(
            config.schedule_dates_contiguous is False for config in estate.configs
        ),
    )


def _no_consumer_scenario(
    inventory: Inventory,
    by_config: dict[str, ConfigCost],
    readers: set[str],
) -> Scenario | None:
    idle = []
    for config in by_config.values():
        if config.enabled is not True or not config.destination:
            continue
        if config.destination in readers or consumers(inventory, config.destination):
            continue
        idle.append(config)
    if not idle:
        return None
    idle.sort(key=lambda config: config.config_id)
    return _stop_scenario(
        "stop-no-recorded-consumer",
        "Stop enabled queries with no recorded consumer",
        "measured",
        "owner_confirmation",
        idle,
        retains=(),
        evidence=(
            "Each destination has no graph read, certain or uncertain, and no "
            f"downstream_usage row. Queries: {len(idle)}."
        ),
        assumption=(
            "Stopping the schedule removes its observed scan cost. No "
            "replacement query is added."
        ),
        uncertainty=_NO_CONSUMER_UNCERTAINTY,
    )


def _zero_scan_scenario(by_config: dict[str, ConfigCost]) -> Scenario | None:
    failed = [
        config
        for config in by_config.values()
        if config.enabled is True
        and config.execution_count > 0
        and config.failed_count == config.execution_count
        and config.normalized_daily_run_rate_usd == Decimal(0)
    ]
    if not failed:
        return None
    failed.sort(key=lambda config: config.config_id)
    return _stop_scenario(
        "stop-zero-scan-failures",
        "Disable enabled queries whose observed scan cost is already zero",
        "measured",
        "owner_confirmation",
        failed,
        retains=(),
        evidence=(
            "Every recorded execution failed and the estimated daily rate is "
            f"zero. Queries: {len(failed)}."
        ),
        assumption="No replacement query is added.",
        uncertainty=(
            "These executions are already estimated at zero scan cost, so "
            "disabling them does not reduce the estimated bill. The failure "
            "is an operational issue. Do not delete the destination tables."
        ),
    )


def _writer_scenarios(
    graph: Graph,
    by_config: dict[str, ConfigCost],
    queries: dict[str, ScheduledQuery],
) -> list[Scenario]:
    scenarios = []
    for table_node, writer_nodes in duplicate_writers(graph):
        writers = []
        for node_id in writer_nodes:
            config = by_config.get(node_id.removeprefix("query:"))
            if config is not None:
                writers.append(config)
        retained = _retain(writers)
        if retained is None:
            continue
        stopped = [
            config for config in writers if config.config_id != retained.config_id
        ]
        labels = [
            _writer_label(queries.get(config.config_id), config)
            for config in sorted(writers, key=lambda item: item.config_id)
        ]
        table = table_node.removeprefix("table:")
        scenarios.append(
            _stop_scenario(
                f"stop-competing-writer-{table}",
                f"Keep one writer of {table}",
                "assumed",
                "business_logic",
                stopped,
                retains=(retained.config_id,),
                evidence=(
                    f"The graph records more than one writer of {table}. "
                    + "; ".join(labels)
                    + "."
                ),
                assumption=(
                    "Consumers of the table can be served by the retained "
                    "writer alone."
                ),
                uncertainty=_WRITER_UNCERTAINTY,
            )
        )
    return scenarios


def _repeated_scenarios(
    queries: dict[str, ScheduledQuery],
    by_config: dict[str, ConfigCost],
    inventory: Inventory,
    readers: set[str],
) -> list[Scenario]:
    groups: dict[str, list[ScheduledQuery]] = {}
    for query in queries.values():
        groups.setdefault(_normalize_sql(query.sql_text), []).append(query)
    scenarios = []
    for copies in groups.values():
        if len(copies) < 2:
            continue
        configs = [
            by_config[query.config_id]
            for query in copies
            if query.config_id in by_config
        ]
        preferred = {
            query.config_id
            for query in copies
            if query.destination is not None
            and (
                query.destination.dataset_table in readers
                or consumers(inventory, query.destination.dataset_table)
            )
        }
        retained = _retain(configs, preferred)
        if retained is None:
            continue
        stopped = [
            config for config in configs if config.config_id != retained.config_id
        ]
        ordered_ids = tuple(sorted(config.config_id for config in configs))
        destinations = ", ".join(
            f"{config.config_id}->{config.destination or 'unknown'}"
            for config in sorted(configs, key=lambda item: item.config_id)
        )
        scenarios.append(
            _stop_scenario(
                "stop-repeated-sql-" + "-".join(ordered_ids),
                "Keep one copy of SQL that is repeated after comment removal",
                "assumed",
                "business_logic",
                stopped,
                retains=(retained.config_id,),
                evidence=(
                    "The SQL matches after comment and whitespace removal. "
                    f"Destinations: {destinations}."
                ),
                assumption=(
                    "The retained destination can stand in for the stopped "
                    "destinations."
                ),
                uncertainty=_REPEATED_UNCERTAINTY,
            )
        )
    return scenarios


def _hourly_scenario(by_config: dict[str, ConfigCost]) -> Scenario | None:
    members: list[ScenarioMember] = []
    for config in by_config.values():
        dates = len(config.observed_schedule_dates)
        rate = config.normalized_daily_run_rate_usd
        historical = config.historical_estimated_cost_usd
        if (
            config.frequency != "hourly"
            or rate is None
            or historical is None
            or dates == 0
            or config.execution_count <= dates
        ):
            continue
        per_run = divide(historical, config.execution_count)
        savings = _minus(rate, per_run)
        if savings <= 0:
            continue
        members.append(
            ScenarioMember(
                config_id=config.config_id,
                daily_savings_usd=savings,
                detail=(
                    f"{config.execution_count} executions on {dates} observed "
                    f"dates; {config.destination or 'unknown destination'}"
                ),
            )
        )
    if not members:
        return None
    ordered = tuple(
        sorted(
            members, key=lambda member: (-member.daily_savings_usd, member.config_id)
        )
    )
    return Scenario(
        scenario_id="one-run-per-observed-day",
        title="Run each hourly query once per observed day",
        kind="assumed",
        change_class="business_logic",
        stops=tuple(member.config_id for member in ordered),
        retains=(),
        daily_savings_usd=sum_published(member.daily_savings_usd for member in ordered),
        members=ordered,
        evidence=(
            "The saving for a query is its observed daily rate minus the "
            "average cost of one recorded execution. The estate figure is "
            f"the sum across {len(ordered)} hourly queries that ran more "
            "than once on an observed date."
        ),
        assumption=(
            "A single remaining execution would scan the same bytes as the "
            "average recorded execution, not a full extra day of increments."
        ),
        uncertainty=_HOURLY_UNCERTAINTY,
    )


def _route_cost(
    graph: Graph,
    by_config: dict[str, ConfigCost],
    table_name: str,
) -> RouteCost:
    scope = migration_scope(graph, table_name)
    config_ids = [
        node_id.removeprefix("query:")
        for node_id in scope
        if node_id.startswith("query:") and node_id.removeprefix("query:") in by_config
    ]
    queries = _priced(by_config, config_ids)
    siblings = [
        node_id.removeprefix("query:")
        for node_id in shared_source_siblings(graph, scope)
        if node_id.startswith("query:")
    ]
    sibling_queries = _priced(by_config, siblings)
    return RouteCost(
        table_name=table_name,
        queries=queries,
        daily_cost_usd=_sum_rates(query.daily_cost_usd for query in queries),
        daily_tib=_sum_rates(query.daily_tib for query in queries),
        missing_rate_config_ids=tuple(
            query.config_id for query in queries if query.daily_cost_usd is None
        ),
        excluded_sibling_config_ids=tuple(query.config_id for query in sibling_queries),
        excluded_sibling_daily_cost_usd=_sum_rates(
            query.daily_cost_usd for query in sibling_queries
        ),
    )


def _finding_costs(
    findings: tuple[AuditFinding, ...],
    by_config: dict[str, ConfigCost],
) -> tuple[FindingCost, ...]:
    priced = []
    for finding in findings:
        known = []
        missing = []
        for config_id in finding.config_ids:
            config = by_config.get(config_id)
            if config is None or config.normalized_daily_run_rate_usd is None:
                missing.append(config_id)
            else:
                known.append(config.normalized_daily_run_rate_usd)
        priced.append(
            FindingCost(
                finding_id=finding.finding_id,
                priority=finding.priority,
                confidence=finding.confidence,
                title=finding.title,
                config_ids=finding.config_ids,
                daily_cost_usd=sum_published(known),
                missing_rate_config_ids=tuple(missing),
            )
        )
    return tuple(
        sorted(priced, key=lambda item: (-item.daily_cost_usd, item.finding_id))
    )


def _stop_scenario(
    scenario_id: str,
    title: str,
    kind: ScenarioKind,
    change_class: ChangeClass,
    stopped: list[ConfigCost],
    *,
    retains: tuple[str, ...],
    evidence: str,
    assumption: str,
    uncertainty: str,
) -> Scenario:
    known = [
        config for config in stopped if config.normalized_daily_run_rate_usd is not None
    ]
    missing = [
        config.config_id
        for config in stopped
        if config.normalized_daily_run_rate_usd is None
    ]
    members = tuple(
        ScenarioMember(
            config_id=config.config_id,
            daily_savings_usd=config.normalized_daily_run_rate_usd or Decimal(0),
            detail=(
                f"{config.frequency} {config.destination or 'unknown destination'}"
            ),
        )
        for config in sorted(
            known,
            key=lambda config: (
                -(config.normalized_daily_run_rate_usd or Decimal(0)),
                config.config_id,
            ),
        )
    )
    savings: Decimal | None
    if not known:
        savings = None
    else:
        savings = sum_published(member.daily_savings_usd for member in members)
    if missing:
        uncertainty = (
            uncertainty
            + " No daily rate, so those queries are omitted from the dollar "
            "figure: " + ", ".join(missing) + "."
        )
    return Scenario(
        scenario_id=scenario_id,
        title=title,
        kind=kind,
        change_class=change_class,
        stops=tuple(sorted(config.config_id for config in stopped)),
        retains=retains,
        daily_savings_usd=savings,
        members=members,
        evidence=evidence,
        assumption=assumption,
        uncertainty=uncertainty,
    )


def _retain(
    configs: list[ConfigCost],
    preferred: set[str] | None = None,
) -> ConfigCost | None:
    eligible = [
        config for config in configs if config.normalized_daily_run_rate_usd is not None
    ]
    if preferred:
        chosen = [config for config in eligible if config.config_id in preferred]
        if chosen:
            eligible = chosen
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda config: (config.normalized_daily_run_rate_usd, config.config_id),
    )


def _priced(
    by_config: dict[str, ConfigCost], config_ids: list[str]
) -> tuple[PricedQuery, ...]:
    configs = [
        by_config[config_id] for config_id in config_ids if config_id in by_config
    ]
    ordered = sorted(
        configs,
        key=lambda config: (
            config.normalized_daily_run_rate_usd is None,
            -(config.normalized_daily_run_rate_usd or Decimal(0)),
            config.config_id,
        ),
    )
    return tuple(
        PricedQuery(
            config_id=config.config_id,
            destination=config.destination,
            frequency=config.frequency,
            daily_cost_usd=config.normalized_daily_run_rate_usd,
            daily_tib=config.normalized_daily_scanned_tib,
            sql_path=config.sql_path,
        )
        for config in ordered
    )


def _sum_rates(amounts: Iterable[Decimal | None]) -> Decimal:
    return sum_published(amount for amount in amounts if amount is not None)


def _writer_label(query: ScheduledQuery | None, config: ConfigCost) -> str:
    disposition = "unknown disposition" if query is None else query.write_disposition
    schedule = config.schedule or "unknown schedule"
    destination = config.destination or "unknown destination"
    return f"{config.config_id} {disposition} {schedule} -> {destination}"


def _normalize_sql(sql: str) -> str:
    text = _COMMENT_BLOCK.sub(" ", sql)
    text = _COMMENT_LINE.sub("", text)
    return _SPACE.sub(" ", text).strip()


def _minus(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_WIDE):
        return left - right


def _scenario_key(scenario: Scenario) -> tuple[bool, Decimal, str]:
    savings = scenario.daily_savings_usd
    return (savings is None, -(savings or Decimal(0)), scenario.scenario_id)
