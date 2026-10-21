"""Aggregate priced executions by config and then across the estate.

Each config is normalized by its own distinct schedule dates. The estate
daily run-rate is the sum of those per-config rates. Historical spend is not
divided by a shared 90-day window.

The denominator is the number of distinct observed schedule dates, not the
calendar span from the first date to the last. A gap between dates leaves
``schedule_dates_contiguous`` false and does not add the missing days.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from cost.calculate import (
    BYTES_PER_TIB,
    MINIMUM_BILLED_BYTES,
    USD_PER_TIB,
    divide,
    price_run,
    sum_published,
)
from cost.models import ConfigCost, EstateCost, RunCost, SliceCost
from estate.models import HistoryRecord, Inventory, ScheduledQuery

_FREQUENCIES = ("daily", "hourly", "other")
_ENABLED = ("enabled", "disabled", "unknown")


def calculate_estate(inventory: Inventory) -> EstateCost:
    """Price every history row and every scheduled query in ``inventory``.

    History rows are not dropped. Queries with no history stay in the result
    with no invented cost. Configs are ordered by normalized daily run-rate
    descending, then by config_id. Runs are ordered by config_id, schedule
    time, and run id.

    Args:
        inventory: A loaded estate. Source files are not read or modified here.

    Returns:
        Per-run, per-config, and estate estimates.
    """
    duplicate_ids = _duplicate_run_ids(inventory.history)
    runs = tuple(
        sorted(
            (_price_history(row, duplicate_ids) for row in inventory.history),
            key=_run_sort_key,
        )
    )
    by_config: dict[str, list[RunCost]] = defaultdict(list)
    for run in runs:
        by_config[run.config_id].append(run)
    queries = {query.config_id: query for query in inventory.queries}
    config_ids = sorted(set(queries) | set(by_config))
    configs = tuple(
        sorted(
            (
                _config_cost(
                    config_id, queries.get(config_id), by_config[config_id], inventory
                )
                for config_id in config_ids
            ),
            key=_rank_key,
        )
    )
    return _estate_cost(inventory, runs, configs)


def frequency_of(schedule: str | None) -> str:
    """Classify a transfer schedule as hourly, daily, or other.

    Args:
        schedule: Schedule text from the transfer config.

    Returns:
        ``hourly`` when the text mentions hours, otherwise ``daily`` when it
        mentions days, otherwise ``other``.
    """
    text = (schedule or "").lower()
    if "hour" in text:
        return "hourly"
    if "day" in text:
        return "daily"
    return "other"


def _price_history(row: HistoryRecord, duplicate_ids: set[str]) -> RunCost:
    raw = row.raw
    run_id = raw.get("run_id", "")
    return price_run(
        config_id=row.config_id,
        run_id=run_id,
        schedule_time=raw.get("schedule_time", ""),
        start_time=raw.get("start_time", ""),
        end_time=raw.get("end_time", ""),
        state=raw.get("state", ""),
        total_bytes_processed=raw.get("total_bytes_processed", ""),
        duplicate_run_id=bool(run_id.strip()) and run_id.strip() in duplicate_ids,
    )


def _duplicate_run_ids(history: tuple[HistoryRecord, ...]) -> set[str]:
    counts: dict[str, int] = defaultdict(int)
    for row in history:
        run_id = row.raw.get("run_id", "").strip()
        if run_id:
            counts[run_id] += 1
    return {run_id for run_id, count in counts.items() if count > 1}


def _config_cost(
    config_id: str,
    query: ScheduledQuery | None,
    runs: list[RunCost],
    inventory: Inventory,
) -> ConfigCost:
    dates = tuple(
        sorted({run.schedule_date for run in runs if run.schedule_date is not None})
    )
    priced = [run for run in runs if run.estimated_cost_usd is not None]
    observed_cost = sum_published(
        [run.estimated_cost_usd for run in priced if run.estimated_cost_usd is not None]
    )
    bytes_complete = bool(runs) and all(run.recorded_bytes is not None for run in runs)
    cost_complete = bool(runs) and len(priced) == len(runs)
    dates_complete = bool(runs) and all(run.schedule_date is not None for run in runs)
    recorded = sum(run.recorded_bytes or 0 for run in runs) if bytes_complete else None
    billed = (
        sum(run.estimated_billed_bytes or 0 for run in runs) if cost_complete else None
    )
    rate_ready = cost_complete and dates_complete and bool(dates)
    scanned_rate = _per_day(recorded, len(dates)) if rate_ready else None
    billed_rate = _per_day(billed, len(dates)) if rate_ready else None
    cost_rate = divide(observed_cost, len(dates)) if rate_ready else None
    span, contiguous = _span(dates)
    missing = not runs or not rate_ready
    return ConfigCost(
        config_id=config_id,
        sql_path=_sql_path(inventory, query),
        schedule=None if query is None else query.schedule,
        frequency=frequency_of(None if query is None else query.schedule),
        enabled=None if query is None else query.enabled,
        transfer_state=None if query is None else query.state,
        destination=_destination(query),
        execution_count=len(runs),
        succeeded_count=sum(run.state == "SUCCEEDED" for run in runs),
        failed_count=sum(run.state == "FAILED" for run in runs),
        other_state_count=sum(run.state not in {"SUCCEEDED", "FAILED"} for run in runs),
        recorded_bytes=recorded,
        estimated_billed_bytes=billed,
        observed_priced_cost_usd=observed_cost,
        historical_estimated_cost_usd=observed_cost if cost_complete else None,
        observed_schedule_dates=dates,
        period_start=dates[0] if dates else None,
        period_end=dates[-1] if dates else None,
        calendar_span_days=span,
        schedule_dates_contiguous=contiguous,
        average_scanned_tib_per_observed_day=scanned_rate,
        average_estimated_cost_usd_per_observed_day=cost_rate,
        normalized_daily_run_rate_usd=cost_rate,
        normalized_daily_scanned_tib=scanned_rate,
        normalized_daily_billed_tib=billed_rate,
        no_observed_runs=not runs,
        missing_cost_evidence=missing,
    )


def _estate_cost(
    inventory: Inventory,
    runs: tuple[RunCost, ...],
    configs: tuple[ConfigCost, ...],
) -> EstateCost:
    priced_cost = sum_published([config.observed_priced_cost_usd for config in configs])
    rates = [
        config.normalized_daily_run_rate_usd
        for config in configs
        if config.normalized_daily_run_rate_usd is not None
    ]
    scanned = [
        config.normalized_daily_scanned_tib
        for config in configs
        if config.normalized_daily_scanned_tib is not None
    ]
    billed = [
        config.normalized_daily_billed_tib
        for config in configs
        if config.normalized_daily_billed_tib is not None
    ]
    costs_complete = all(run.estimated_cost_usd is not None for run in runs)
    dates_complete = all(run.schedule_date is not None for run in runs)
    daily_complete = costs_complete and dates_complete
    observed_rate = sum_published([rate for rate in rates if rate is not None])
    observed_scanned = sum_published([tib for tib in scanned if tib is not None])
    observed_billed = sum_published([tib for tib in billed if tib is not None])
    return EstateCost(
        usd_per_tib=USD_PER_TIB,
        bytes_per_tib=BYTES_PER_TIB,
        minimum_billed_bytes=MINIMUM_BILLED_BYTES,
        runs=runs,
        configs=configs,
        query_count=len(inventory.queries),
        observed_priced_cost_usd=priced_cost,
        historical_estimated_cost_usd=priced_cost if costs_complete else None,
        observed_normalized_daily_run_rate_usd=observed_rate,
        normalized_daily_run_rate_usd=observed_rate if daily_complete else None,
        observed_normalized_daily_scanned_tib=observed_scanned,
        normalized_daily_scanned_tib=observed_scanned if daily_complete else None,
        observed_normalized_daily_billed_tib=observed_billed,
        normalized_daily_billed_tib=observed_billed if daily_complete else None,
        by_frequency=_slices(configs, _FREQUENCIES, lambda config: config.frequency),
        by_enabled=_slices(configs, _ENABLED, _enabled_label),
        configs_with_no_observed_runs=sum(
            config.no_observed_runs for config in configs
        ),
        configs_missing_cost_evidence=sum(
            config.missing_cost_evidence for config in configs
        ),
        execution_costs_complete=costs_complete,
        daily_rate_complete=daily_complete,
    )


def _slices(
    configs: tuple[ConfigCost, ...], labels: tuple[str, ...], label_of
) -> tuple[SliceCost, ...]:
    grouped: dict[str, list[ConfigCost]] = {label: [] for label in labels}
    for config in configs:
        label = label_of(config)
        grouped.setdefault(label, []).append(config)
    extras = sorted(label for label in grouped if label not in labels)
    ordered = [label for label in (*labels, *extras) if label in grouped]
    return tuple(_slice(label, grouped[label]) for label in ordered)


def _slice(label: str, configs: list[ConfigCost]) -> SliceCost:
    rated = [
        config for config in configs if config.normalized_daily_run_rate_usd is not None
    ]
    scanned = [
        config for config in configs if config.normalized_daily_scanned_tib is not None
    ]
    return SliceCost(
        label=label,
        config_count=len(configs),
        configs_with_run_rate=len(rated),
        observed_priced_cost_usd=sum_published(
            [config.observed_priced_cost_usd for config in configs]
        ),
        normalized_daily_run_rate_usd=sum_published(
            [
                config.normalized_daily_run_rate_usd
                for config in rated
                if config.normalized_daily_run_rate_usd is not None
            ]
        ),
        normalized_daily_scanned_tib=sum_published(
            [
                config.normalized_daily_scanned_tib
                for config in scanned
                if config.normalized_daily_scanned_tib is not None
            ]
        ),
    )


def _per_day(num_bytes: int | None, days: int) -> Decimal | None:
    if num_bytes is None or days <= 0:
        return None
    return divide(num_bytes, days * BYTES_PER_TIB)


def _span(dates: tuple[date, ...]) -> tuple[int | None, bool | None]:
    if not dates:
        return None, None
    span = (dates[-1] - dates[0]).days + 1
    return span, span == len(dates)


def _destination(query: ScheduledQuery | None) -> str | None:
    if query is None or query.destination is None:
        return None
    return query.destination.dataset_table


def _sql_path(inventory: Inventory, query: ScheduledQuery | None) -> str | None:
    if query is None:
        return None
    try:
        return query.sql_path.resolve().relative_to(inventory.root.resolve()).as_posix()
    except ValueError:
        return query.sql_path.as_posix()


def _enabled_label(config: ConfigCost) -> str:
    if config.enabled is True:
        return "enabled"
    if config.enabled is False:
        return "disabled"
    return "unknown"


def _run_sort_key(run: RunCost) -> tuple[str, str, str]:
    return (run.config_id, run.schedule_time or "", run.run_id or "")


def _rank_key(config: ConfigCost) -> tuple[bool, Decimal, str]:
    rate = config.normalized_daily_run_rate_usd
    return (rate is None, -(rate or Decimal(0)), config.config_id)
