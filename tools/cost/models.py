"""Cost estimates produced from recorded scheduled-query executions.

``total_bytes_processed`` is the byte count stored in run history. Estimated
billed bytes apply the pricing rules in ``cost.calculate``. Neither figure is
an invoice. Actual billed bytes and invoiced cost are not in the supplied files.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import NamedTuple


class RunCost(NamedTuple):
    """One execution, kept even when its bytes or timestamps cannot be priced."""

    config_id: str
    run_id: str | None
    schedule_time: str | None
    start_time: str | None
    end_time: str | None
    schedule_date: date | None
    state: str
    recorded_bytes: int | None
    estimated_billed_bytes: int | None
    estimated_cost_usd: Decimal | None
    chargeable: bool
    duplicate_run_id: bool
    qualification: str


class ConfigCost(NamedTuple):
    """Observed history and a normalized daily run-rate for one config.

    ``historical_estimated_cost_usd`` is None when the config has no
    executions or any execution cannot be priced. That is not a zero-cost
    result. ``normalized_daily_run_rate_usd`` divides priced cost by the
    config's own distinct schedule dates. It is not a calendar-span average
    and it is not a forecast invoice.

    ``observed_priced_cost_usd`` still sums every execution that could be
    priced, so a partial history is not discarded.
    """

    config_id: str
    sql_path: str | None
    schedule: str | None
    frequency: str
    enabled: bool | None
    transfer_state: str | None
    destination: str | None
    execution_count: int
    succeeded_count: int
    failed_count: int
    other_state_count: int
    recorded_bytes: int | None
    estimated_billed_bytes: int | None
    observed_priced_cost_usd: Decimal
    historical_estimated_cost_usd: Decimal | None
    observed_schedule_dates: tuple[date, ...]
    period_start: date | None
    period_end: date | None
    calendar_span_days: int | None
    schedule_dates_contiguous: bool | None
    average_scanned_tib_per_observed_day: Decimal | None
    average_estimated_cost_usd_per_observed_day: Decimal | None
    normalized_daily_run_rate_usd: Decimal | None
    normalized_daily_scanned_tib: Decimal | None
    normalized_daily_billed_tib: Decimal | None
    no_observed_runs: bool
    missing_cost_evidence: bool


class SliceCost(NamedTuple):
    """Sum of config estimates that share a frequency or enabled state.

    A config with no published run-rate contributes to ``config_count`` and
    not to the normalized totals. A zero normalized total with
    ``configs_with_run_rate`` of 0 means no config in the slice was priced.
    """

    label: str
    config_count: int
    configs_with_run_rate: int
    observed_priced_cost_usd: Decimal
    normalized_daily_run_rate_usd: Decimal
    normalized_daily_scanned_tib: Decimal


class EstateCost(NamedTuple):
    """Estate totals built by summing per-config rates, not by dividing by 90.

    The normalized fields are None when any recorded execution is unpriced or
    has no schedule date. Configs that simply have no history do not clear
    those fields. ``observed_*`` totals still include every priced execution
    and every published per-config rate.
    """

    usd_per_tib: Decimal
    bytes_per_tib: int
    minimum_billed_bytes: int
    runs: tuple[RunCost, ...]
    configs: tuple[ConfigCost, ...]
    query_count: int
    observed_priced_cost_usd: Decimal
    historical_estimated_cost_usd: Decimal | None
    observed_normalized_daily_run_rate_usd: Decimal
    normalized_daily_run_rate_usd: Decimal | None
    observed_normalized_daily_scanned_tib: Decimal
    normalized_daily_scanned_tib: Decimal | None
    observed_normalized_daily_billed_tib: Decimal
    normalized_daily_billed_tib: Decimal | None
    by_frequency: tuple[SliceCost, ...]
    by_enabled: tuple[SliceCost, ...]
    configs_with_no_observed_runs: int
    configs_missing_cost_evidence: int
    execution_costs_complete: bool
    daily_rate_complete: bool
