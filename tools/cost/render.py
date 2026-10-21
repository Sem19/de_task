"""Render COST.txt from a calculated cost analysis.

This module formats values it is given. It does not price queries, rank
configurations, or choose scenario savings.
"""

from __future__ import annotations

import textwrap
from decimal import Decimal

from cost.calculate import present_usd
from cost.models import ConfigCost
from cost.report_models import (
    CostAnalysis,
    FindingCost,
    PricedQuery,
    RouteCost,
    Scenario,
    SliceCost,
)

_WIDTH = 88


def render_cost(analysis: CostAnalysis) -> str:
    """Render the cost report.

    Args:
        analysis: Calculated totals, rankings, route cost, and scenarios.

    Returns:
        The report, ending with a newline.
    """
    parts = [
        _header(analysis),
        _run_rate(analysis),
        _breakdown(analysis),
        _ranking(analysis),
        _route(analysis.route),
        _scenarios(analysis.scenarios),
        _by_class(
            analysis.scenarios,
            "owner_confirmation",
            "6. Schedule stops that do not change SQL",
        ),
        _by_class(
            analysis.scenarios,
            "business_logic",
            "7. Changes that need a business or SQL decision",
        ),
        _findings(analysis.finding_costs),
        _limitations(analysis),
    ]
    return "\n\n".join(part for part in parts if part) + "\n"


def _header(analysis: CostAnalysis) -> str:
    estate = analysis.estate
    return "\n".join(
        [
            "COST",
            "Scheduled-query estate",
            f"Source directory: {analysis.source_name}/",
            "Regenerate: python -m cost_report",
            "",
            "Figures are estimates from run_history.total_bytes_processed.",
            "They are not an invoice and they are not a forecast of future spend.",
            _paragraph(
                "Pricing from the cost engine: "
                f"${estate.usd_per_tib} per TiB ({estate.bytes_per_tib} bytes). "
                "Minimum billed bytes for a successful execution with a positive "
                f"recorded byte count: {estate.minimum_billed_bytes}. "
                "Failed executions and successful zero-byte executions are "
                "estimated at zero. Display amounts are rounded half-up from "
                "the unrounded estimates."
            ),
        ]
    )


def _run_rate(analysis: CostAnalysis) -> str:
    estate = analysis.estate
    return "\n".join(
        [
            "1. Daily run-rate",
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
            f"Normalized daily scanned TiB: {_tib(estate.normalized_daily_scanned_tib)}",
            f"Normalized daily estimated cost: {_usd(estate.normalized_daily_run_rate_usd)}",
            f"Historical estimated cost: {_usd(estate.historical_estimated_cost_usd)}",
            _paragraph(
                "The estate daily rate is the sum of per-configuration rates. "
                "Each configuration uses its own observed schedule dates. "
                "Historical cost is not divided by one shared window. "
                "A configuration with no executions has no daily rate. "
                "That is not a recorded cost of zero."
            ),
        ]
    )


def _breakdown(analysis: CostAnalysis) -> str:
    lines = ["2. Where the cost sits", "By schedule frequency:"]
    lines.extend(_slice_line(item) for item in analysis.frequency)
    lines.append("By enabled state:")
    lines.extend(_slice_line(item) for item in analysis.enabled)
    return "\n".join(lines)


def _slice_line(item: SliceCost) -> str:
    if item.configs_with_run_rate == 0:
        rate = "no daily rate"
    else:
        rate = (
            f"{_tib(item.normalized_daily_scanned_tib)} / day, "
            f"{_usd(item.normalized_daily_run_rate_usd)} / day"
        )
    return (
        f"  {item.label}: {item.config_count} configurations, "
        f"{item.configs_with_run_rate} with a daily rate, {rate}"
    )


def _ranking(analysis: CostAnalysis) -> str:
    lines = [
        "3. Daily cost ranking",
        "Order is normalized daily cost descending, then config_id.",
    ]
    for index, config in enumerate(analysis.ranked_configs, start=1):
        lines.append(_paragraph(f"  {index}. {_config_line(config)}"))
    return "\n".join(lines)


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


def _route(route: RouteCost) -> str:
    lines = [
        f"4. Lineage route for {route.table_name}",
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


def _query_line(query: PricedQuery) -> str:
    return (
        f"{query.config_id} {query.frequency} "
        f"{query.destination or 'unknown destination'} "
        f"{_usd(query.daily_cost_usd, 4)} / day {_tib(query.daily_tib)} "
        f"{query.sql_path or 'no sql path'}"
    )


def _scenarios(scenarios: tuple[Scenario, ...]) -> str:
    lines = [
        "5. Savings scenarios",
        "Scenarios are not additive. One query can appear in more than one.",
        _paragraph(
            "Measured savings are observed daily rates of queries that would "
            "stop, with no replacement scan. Assumed savings depend on a "
            "replacement or a schedule change that was not executed."
        ),
    ]
    if not scenarios:
        lines.append("No scenario was priced from this estate.")
    for scenario in scenarios:
        lines.extend(_scenario_block(scenario))
    return "\n".join(lines)


def _scenario_block(scenario: Scenario) -> list[str]:
    retains = ", ".join(scenario.retains) or "none"
    lines = [
        scenario.scenario_id,
        f"  {scenario.title}",
        f"  Kind: {scenario.kind}. Change: {scenario.change_class}.",
        f"  Daily savings: {_usd(scenario.daily_savings_usd)}",
        f"  Retained: {retains}",
        _paragraph(f"  Evidence: {scenario.evidence}"),
        _paragraph(f"  Assumption: {scenario.assumption}"),
        _paragraph(f"  Uncertainty: {scenario.uncertainty}"),
    ]
    for member in scenario.members:
        lines.append(
            _paragraph(
                f"    {member.config_id} {_usd(member.daily_savings_usd, 4)} / day "
                f"({member.detail})"
            )
        )
    return lines


def _by_class(scenarios: tuple[Scenario, ...], change_class: str, title: str) -> str:
    chosen = [
        scenario for scenario in scenarios if scenario.change_class == change_class
    ]
    lines = [
        title,
        _paragraph(
            "The amounts below are the scenario totals already calculated. "
            "They are not a combined saving."
        ),
    ]
    if not chosen:
        lines.append("None.")
    for scenario in chosen:
        lines.append(
            f"  {scenario.scenario_id}: {_usd(scenario.daily_savings_usd)} / day "
            f"({scenario.kind})"
        )
    return "\n".join(lines)


def _findings(findings: tuple[FindingCost, ...]) -> str:
    lines = [
        "8. Audit findings by observed daily cost",
        _paragraph(
            "The amount is the observed daily rate of the queries the finding "
            "names. It is not the saving from the audit recommendation unless "
            "a scenario above stops those queries and prices that stop."
        ),
    ]
    if not findings:
        lines.append("No audit findings were supplied.")
        return "\n".join(lines)
    for finding in findings:
        missing = ", ".join(finding.missing_rate_config_ids)
        suffix = f" Missing daily rate: {missing}." if missing else ""
        configs = ", ".join(finding.config_ids) or "none"
        lines.append(
            _paragraph(
                f"  {finding.finding_id} P{finding.priority} {finding.confidence} "
                f"{_usd(finding.daily_cost_usd, 4)} / day configs: {configs}.{suffix}"
            )
        )
        lines.append(f"    {finding.title}")
    return "\n".join(lines)


def _limitations(analysis: CostAnalysis) -> str:
    return "\n".join(
        [
            "9. Limitations",
            _paragraph(
                "total_bytes_processed is the byte count stored in run history. "
                "It is not a BigQuery billing export. Cached bytes, bytes written, "
                "and charges for a failed query are not in the supplied files."
            ),
            _paragraph(
                "Observed schedule dates that are not contiguous: "
                f"{analysis.configs_with_date_gaps} configurations. A daily rate "
                "uses the count of observed dates, not the calendar span from "
                "the first date to the last."
            ),
            _paragraph(
                "The hourly scenario and the writer and repeated-SQL scenarios "
                "state a replacement that was not run. Their dollar figures stay "
                "assumptions. The no-consumer figure is the observed rate only, "
                "and the usage extract can still have missed a reader."
            ),
            _paragraph(
                "Disabling a schedule is not deleting a table. This report does "
                "not select queries for deletion."
            ),
        ]
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
