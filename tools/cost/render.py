"""Render COST.txt from a calculated cost analysis.

This module formats values it is given. It does not price queries, rank
configurations, or choose which optimization to investigate.
"""

from __future__ import annotations

import textwrap
from decimal import Decimal

from cost.calculate import present_usd
from cost.models import ConfigCost
from cost.report_models import (
    CostAnalysis,
    DriverReview,
    FindingCost,
    PricedQuery,
    Recommendation,
    RouteCost,
    Scenario,
    SliceCost,
)

_WIDTH = 88
_ROLE_LABEL = {
    "conditional_avoidable": "Conditionally avoidable daily scan cost",
    "hypothetical": "Hypothetical daily difference",
    "assumed_replacement": "Observed daily cost of the queries that would stop",
    "operational_cleanup": "Observed daily scan cost",
}


def render_cost(analysis: CostAnalysis) -> str:
    """Render the cost report.

    Args:
        analysis: Calculated totals, reviews, rankings, and scenarios.

    Returns:
        The report, ending with a newline.
    """
    parts = [
        _header(analysis),
        _executive(analysis),
        _methodology(analysis),
        _top(analysis),
        _recommendations(analysis),
        _cleanup(analysis.scenarios),
        _scenarios(analysis.scenarios),
        _route(analysis.route),
        _findings(analysis),
        _limitations(analysis),
        _appendix(analysis),
    ]
    return "\n\n".join(part for part in parts if part) + "\n"


def _header(analysis: CostAnalysis) -> str:
    return "\n".join(
        [
            "COST",
            "Scheduled-query estate",
            f"Source directory: {analysis.source_name}/",
            "Regenerate: python -m cost_report",
            "",
            "Figures are estimates from run_history.total_bytes_processed.",
            "They are not an invoice and they are not a forecast of future spend.",
        ]
    )


def _executive(analysis: CostAnalysis) -> str:
    estate = analysis.estate
    lines = [
        "1. Executive summary",
        f"Estimated normalized daily cost: {_usd(estate.normalized_daily_run_rate_usd)}",
        f"Normalized daily scanned volume: {_tib(estate.normalized_daily_scanned_tib)}",
        f"Historical estimated cost: {_usd(estate.historical_estimated_cost_usd)}",
    ]
    if analysis.driver_reviews:
        lead = analysis.driver_reviews[0]
        lines.append(
            f"Largest cost driver: {lead.config_id} "
            f"({lead.destination or 'unknown destination'}, {lead.frequency}) "
            f"{_usd(lead.daily_cost_usd)} / day, {_tib(lead.daily_tib)}."
        )
    else:
        lines.append("Largest cost driver: none with a daily rate.")
    lines.append("Highest-priority cost optimization investigations:")
    if not analysis.driver_reviews:
        lines.append("  None.")
    for review in analysis.driver_reviews:
        action = review.investigation.split(". ", maxsplit=1)[0]
        lines.append(
            _paragraph(
                f"  {review.config_id} ({review.destination or 'unknown'}, "
                f"{review.frequency}) {_usd(review.daily_cost_usd)} / day. "
                f"{action}."
            )
        )
    lines.append("Conditional avoidable scan-cost opportunities:")
    conditional = [
        scenario
        for scenario in analysis.scenarios
        if scenario.money_role == "conditional_avoidable"
    ]
    if not conditional:
        lines.append("  None.")
    for scenario in conditional:
        lines.append(
            _paragraph(
                f"  {scenario.scenario_id}: {_usd(scenario.daily_savings_usd)} / day "
                "observed scan cost. Conditionally avoidable if an owner confirms "
                "that no reader was missed. This is not a guaranteed saving."
            )
        )
    lines.append(
        _paragraph(
            "Hourly-to-daily arithmetic is a hypothetical scenario in section 5. "
            "It is not a recommended action and it is not listed here as "
            "avoidable cost."
        )
    )
    return "\n".join(lines)


def _methodology(analysis: CostAnalysis) -> str:
    estate = analysis.estate
    lines = [
        "2. Methodology",
        f"Queries: {estate.query_count}",
        f"Configurations with a daily rate: {analysis.configs_with_daily_rate}",
        (
            "Configurations with no observed runs: "
            f"{estate.configs_with_no_observed_runs}"
        ),
        (
            "Configurations with missing cost evidence: "
            f"{estate.configs_missing_cost_evidence}"
        ),
        _paragraph(
            f"Pricing from the cost engine: ${estate.usd_per_tib} per TiB "
            f"({estate.bytes_per_tib} bytes). Minimum billed bytes for a "
            "successful execution with a positive recorded byte count: "
            f"{estate.minimum_billed_bytes}. Failed executions and successful "
            "zero-byte executions are estimated at zero. Display amounts are "
            "rounded half-up from the unrounded estimates."
        ),
        _paragraph(
            "The estate daily rate is the sum of per-configuration rates. "
            "Each configuration uses its own observed schedule dates. "
            "Historical cost is not divided by one shared window. "
            "A configuration with no executions has no daily rate. "
            "That is not a recorded cost of zero."
        ),
        _paragraph(
            "Observed query cost is the engine's normalized daily rate. "
            "Conditionally avoidable cost is that rate for queries whose "
            "destination has no recorded reader, and only if an owner confirms "
            "the extract is complete. A hypothetical difference assumes a "
            "replacement scan that was not run. Operational cleanup covers "
            "failing schedules whose estimated scan cost is already zero."
        ),
        "By schedule frequency:",
    ]
    lines.extend(_slice_line(item) for item in analysis.frequency)
    lines.append("By enabled state:")
    lines.extend(_slice_line(item) for item in analysis.enabled)
    return "\n".join(lines)


def _top(analysis: CostAnalysis) -> str:
    lines = [
        "3. Top 10 cost contributors",
        "Order is normalized daily cost descending, then config_id.",
        f"Shown: {len(analysis.top_configs)} of {analysis.configs_with_daily_rate} with a daily rate.",
    ]
    for index, config in enumerate(analysis.top_configs, start=1):
        lines.append(_paragraph(f"  {index}. {_config_line(config)}"))
    return "\n".join(lines)


def _recommendations(analysis: CostAnalysis) -> str:
    lines = [
        "4. Prioritized engineering recommendations",
        _paragraph(
            "Priority follows evidence from the largest drivers, then owner "
            "confirmation, then replacement decisions. The hypothetical hourly "
            "difference is not a priority action. Amounts are observed query "
            "cost unless a line says the cost is conditionally avoidable."
        ),
    ]
    if not analysis.recommendations:
        lines.append("None.")
        return "\n".join(lines)
    for item in analysis.recommendations:
        lines.extend(_recommendation(item))
    for review in analysis.driver_reviews:
        lines.extend(_review_block(review))
    return "\n".join(lines)


def _recommendation(item: Recommendation) -> list[str]:
    lines = [
        f"  {item.rank}. {item.heading}",
        f"    Observed daily query cost: {_usd(item.observed_daily_cost_usd)}",
        (
            "    Conditionally avoidable daily scan cost: "
            f"{_usd(item.conditionally_avoidable_usd)}"
        ),
    ]
    if item.config_ids:
        lines.append("    Configs: " + ", ".join(item.config_ids))
    lines.append(_paragraph(f"    {item.body}"))
    return lines


def _review_block(review: DriverReview) -> list[str]:
    findings = ", ".join(review.finding_ids) or "none"
    return [
        f"  Evidence for {review.config_id}",
        _paragraph(f"    Processing pattern: {review.processing_pattern}"),
        _paragraph(f"    Scan volume: {review.scan_volume}"),
        _paragraph(f"    Optimization to investigate: {review.investigation}"),
        _paragraph(f"    Freshness and correctness risks: {review.risks}"),
        _paragraph(f"    Verification in BigQuery: {review.verification}"),
        f"    Associated audit findings: {findings}",
        f"    SQL: {review.sql_path or 'no sql path'}",
    ]


def _cleanup(scenarios: tuple[Scenario, ...]) -> str:
    chosen = [
        scenario
        for scenario in scenarios
        if scenario.money_role == "operational_cleanup"
    ]
    lines = [
        "4a. Operational cleanup",
        _paragraph(
            "These schedules are not cost-saving opportunities. Their estimated "
            "scan cost is already zero. Disabling them does not reduce the bill."
        ),
    ]
    if not chosen:
        lines.append("None.")
        return "\n".join(lines)
    for scenario in chosen:
        lines.extend(_scenario_block(scenario))
    return "\n".join(lines)


def _scenarios(scenarios: tuple[Scenario, ...]) -> str:
    lines = [
        "5. Savings scenarios",
        "Scenarios are not additive. One query can appear in more than one.",
        _paragraph(
            "Conditionally avoidable cost is observed scan cost that disappears "
            "only if the queries stop and no reader was missed. Assumed figures "
            "require a replacement that was not measured. The hypothetical "
            "hourly difference is not a recommended action."
        ),
    ]
    groups = (
        ("Conditionally avoidable", "conditional_avoidable"),
        ("Assumed replacement, not measured", "assumed_replacement"),
        ("Hypothetical, not a recommended action", "hypothetical"),
    )
    any_shown = False
    for title, role in groups:
        chosen = [scenario for scenario in scenarios if scenario.money_role == role]
        if not chosen:
            continue
        any_shown = True
        lines.append(title)
        for scenario in chosen:
            lines.extend(_scenario_block(scenario))
    if not any_shown:
        lines.append("No scenario was priced from this estate.")
    return "\n".join(lines)


def _scenario_block(scenario: Scenario) -> list[str]:
    retains = ", ".join(scenario.retains) or "none"
    label = _ROLE_LABEL[scenario.money_role]
    lines = [
        scenario.scenario_id,
        f"  {scenario.title}",
        f"  Kind: {scenario.kind}. Change: {scenario.change_class}.",
        f"  {label}: {_usd(scenario.daily_savings_usd)}",
        f"  Retained: {retains}",
        _paragraph(f"  Evidence: {scenario.evidence}"),
        _paragraph(f"  Assumption: {scenario.assumption}"),
        _paragraph(f"  Uncertainty: {scenario.uncertainty}"),
    ]
    for member in scenario.members:
        lines.append(
            _paragraph(
                f"    {member.config_id} {_usd(member.daily_savings_usd, 4)} / day "
                f"observed ({member.detail})"
            )
        )
    return lines


def _route(route: RouteCost) -> str:
    lines = [
        f"6. Lineage route for {route.table_name}",
        _paragraph(
            "The route is the scheduled-query ancestors of that exact table "
            "name, every writer of the table, and scheduled-query descendants. "
            "Dashboard and ad-hoc consumers are excluded. Queries that only "
            "share an upstream source are excluded."
        ),
        f"Queries on the route: {len(route.queries)}",
        f"Normalized daily scanned TiB: {_tib(route.daily_tib)}",
        f"Normalized daily estimated cost: {_usd(route.daily_cost_usd)}",
        (
            "Queries on the route with no daily rate: "
            + (", ".join(route.missing_rate_config_ids) or "none")
        ),
    ]
    lines.extend(_paragraph(f"  {_query_line(query)}") for query in route.queries)
    siblings = ", ".join(route.excluded_sibling_config_ids) or "none"
    lines.append(
        _paragraph(
            "Queries that share an upstream source and are outside the route: "
            f"{siblings}. Their normalized daily estimated cost, excluded from "
            f"the route: {_usd(route.excluded_sibling_daily_cost_usd)}."
        )
    )
    return "\n".join(lines)


def _findings(analysis: CostAnalysis) -> str:
    lines = [
        "7. Audit findings and associated spend",
        _paragraph(
            "These totals represent spending associated with the queries the "
            "finding names. They are the observed daily cost of those queries. "
            "They are not the estimated saving from the audit recommendation, "
            "and they are not conditionally avoidable cost unless a scenario "
            "above stops those queries and says so."
        ),
    ]
    if analysis.driver_reviews:
        lines.append("Findings that name a reviewed cost driver:")
        for review in analysis.driver_reviews:
            named = ", ".join(review.finding_ids) or "none"
            lines.append(f"  {review.config_id}: {named}")
    if not analysis.finding_costs:
        lines.append("No audit findings were supplied.")
        return "\n".join(lines)
    for finding in analysis.finding_costs:
        lines.append(_paragraph(f"  {_finding_line(finding)}"))
        lines.append(f"    {finding.title}")
    return "\n".join(lines)


def _finding_line(finding: FindingCost) -> str:
    missing = ", ".join(finding.missing_rate_config_ids)
    suffix = f" Missing daily rate: {missing}." if missing else ""
    configs = ", ".join(finding.config_ids) or "none"
    return (
        f"{finding.finding_id} P{finding.priority} {finding.confidence} "
        f"associated spend {_usd(finding.daily_cost_usd, 4)} / day "
        f"configs: {configs}.{suffix}"
    )


def _limitations(analysis: CostAnalysis) -> str:
    return "\n".join(
        [
            "8. Limitations",
            _paragraph(
                "total_bytes_processed is the byte count stored in run history. "
                "It is not a BigQuery billing export. Cached bytes, bytes written, "
                "and charges for a failed query are not in the supplied files."
            ),
            _paragraph(
                "Observed schedule dates that are not contiguous: "
                f"{_count(analysis.configs_with_date_gaps, 'configuration')}. "
                "A daily rate "
                "uses the count of observed dates, not the calendar span from "
                "the first date to the last."
            ),
            _paragraph(
                "A partition-column gap or a wide lookback is a reason to "
                "investigate. It is not a measured saving. The hourly scenario "
                "and the writer and repeated-SQL scenarios state a replacement "
                "that was not run. The no-consumer figure is not a guaranteed "
                "saving, because the usage extract can miss a reader."
            ),
            _paragraph(
                "Disabling a schedule is not deleting a table. This report does "
                "not select queries for deletion."
            ),
        ]
    )


def _appendix(analysis: CostAnalysis) -> str:
    lines = [
        "Appendix. Full per-config ranking",
        "Order is normalized daily cost descending, then config_id.",
    ]
    for index, config in enumerate(analysis.ranked_configs, start=1):
        lines.append(_paragraph(f"  {index}. {_config_line(config)}"))
    return "\n".join(lines)


def _count(value: int, noun: str) -> str:
    label = noun if value == 1 else f"{noun}s"
    return f"{value} {label}"


def _slice_line(item: SliceCost) -> str:
    if item.configs_with_run_rate == 0:
        rate = "no daily rate"
    else:
        rate = (
            f"{_tib(item.normalized_daily_scanned_tib)} / day, "
            f"{_usd(item.normalized_daily_run_rate_usd)} / day"
        )
    return (
        f"  {item.label}: {_count(item.config_count, 'configuration')}, "
        f"{item.configs_with_run_rate} with a daily rate, {rate}"
    )


def _config_line(config: ConfigCost) -> str:
    dates = len(config.observed_schedule_dates)
    if config.no_observed_runs:
        rate = "no observed runs"
    elif config.normalized_daily_run_rate_usd is None:
        rate = "no daily rate"
    else:
        rate = f"{_usd(config.normalized_daily_run_rate_usd, 4)} / day"
    return (
        f"{config.config_id} {config.frequency} "
        f"{config.destination or 'unknown destination'} "
        f"{rate} {_tib(config.normalized_daily_scanned_tib)} "
        f"{dates} observed dates, span {config.calendar_span_days}, "
        f"contiguous {config.schedule_dates_contiguous}, "
        f"{config.execution_count} executions, {config.failed_count} failed, "
        f"{config.sql_path or 'no sql path'}"
    )


def _query_line(query: PricedQuery) -> str:
    return (
        f"{query.config_id} {query.frequency} "
        f"{query.destination or 'unknown destination'} "
        f"{_usd(query.daily_cost_usd, 4)} / day {_tib(query.daily_tib)} "
        f"{query.sql_path or 'no sql path'}"
    )


def _usd(amount: Decimal | None, places: int = 2) -> str:
    if amount is None:
        return "unmeasured"
    return f"${present_usd(amount, places)}"


def _tib(amount: Decimal | None) -> str:
    if amount is None:
        return "unmeasured"
    return f"{present_usd(amount, 4)} TiB"


def _paragraph(text: str) -> str:
    return textwrap.fill(text, width=_WIDTH, subsequent_indent="  ")
