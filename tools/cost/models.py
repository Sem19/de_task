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
    """One execution, kept even when its bytes or timestamps cannot be priced.

    Attributes:
        config_id: History config id.
        run_id: Run id, or ``None`` when the cell is blank.
        schedule_time: Schedule timestamp text, or ``None`` when blank.
        start_time: Start timestamp text, or ``None`` when blank.
        end_time: End timestamp text, or ``None`` when blank.
        schedule_date: Calendar date of ``schedule_time``, with no timezone
            conversion. ``None`` when the text is missing or not ISO.
        state: Execution state as stored. An empty cell stays empty.
        recorded_bytes: Parsed ``total_bytes_processed``, or ``None`` when
            the cell is missing, not an integer, or negative.
        estimated_billed_bytes: Billed-byte estimate, or ``None`` when the
            recorded count cannot be used.
        estimated_cost_usd: Price of the billed bytes, or ``None`` when the
            execution is not priced.
        chargeable: ``True`` when the billed-byte estimate is greater than zero.
        duplicate_run_id: ``True`` when this non-blank run id occurs more than once.
        qualification: Semicolon-joined limits on the estimate. Empty when
            nothing limited it.
    """

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

    Attributes:
        config_id: Scheduled-query id, or a history id with no paired query.
        sql_path: SQL path relative to the estate root, or ``None``.
        schedule: Transfer schedule, or ``None`` when the query is unpaired.
        frequency: ``hourly``, ``daily``, or ``other``.
        enabled: Derived enabled flag, or ``None`` when it is unknown.
        transfer_state: Stored ``state``, or ``None`` when the query is unpaired.
        destination: ``dataset.table``, or ``None``.
        execution_count: History rows for this config.
        succeeded_count: Rows whose state is ``SUCCEEDED``.
        failed_count: Rows whose state is ``FAILED``.
        other_state_count: Rows with any other state, including a blank state.
        recorded_bytes: Sum of recorded bytes, or ``None`` when any row lacks them.
        estimated_billed_bytes: Sum of billed bytes, or ``None`` when any row
            is unpriced.
        observed_priced_cost_usd: Sum of the executions that could be priced.
            Zero when none could.
        historical_estimated_cost_usd: That sum when every execution is priced.
            ``None`` when any execution is unpriced or there are no executions.
        observed_schedule_dates: Distinct schedule dates, sorted.
        period_start: Earliest of those dates, or ``None``.
        period_end: Latest of those dates, or ``None``.
        calendar_span_days: Inclusive days from the first date to the last,
            or ``None`` when there are no dates.
        schedule_dates_contiguous: ``True`` when that span equals the number
            of distinct dates. ``None`` when there are no dates.
        average_scanned_tib_per_observed_day: Recorded TiB per distinct date,
            or ``None`` when the daily rate is not published.
        average_estimated_cost_usd_per_observed_day: Priced cost per distinct
            date. Same readiness as the run-rate.
        normalized_daily_run_rate_usd: That per-date cost. Not a forecast.
        normalized_daily_scanned_tib: Recorded TiB per distinct date.
        normalized_daily_billed_tib: Billed TiB per distinct date.
        no_observed_runs: ``True`` when this config has no history rows.
        missing_cost_evidence: ``True`` when there are no runs or the daily
            rate cannot be published.
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

    Attributes:
        label: Frequency or enabled-state name.
        config_count: Configs in the slice, including those with no rate.
        configs_with_run_rate: Configs whose daily run-rate is not ``None``.
        observed_priced_cost_usd: Sum of observed priced cost, including
            configs with no rate.
        normalized_daily_run_rate_usd: Sum of published run-rates. Zero when
            none of the configs has one.
        normalized_daily_scanned_tib: Sum of published scanned-TiB rates.
            Zero when none of the configs has one.
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

    Attributes:
        usd_per_tib: On-demand price used for every estimate.
        bytes_per_tib: Bytes in one tebibyte, ``1024**4``.
        minimum_billed_bytes: Minimum applied to a successful positive scan.
        runs: Priced executions, ordered by config id, schedule time, then run id.
        configs: Per-config estimates. Unpriced configs follow priced ones,
            then descending daily rate, then config id.
        query_count: Paired scheduled queries. A history id with no query
            is not included.
        observed_priced_cost_usd: Sum of per-config observed priced cost.
        historical_estimated_cost_usd: That sum when every execution is priced.
            ``None`` when any execution is unpriced.
        observed_normalized_daily_run_rate_usd: Sum of published per-config rates.
        normalized_daily_run_rate_usd: That sum when every execution has a
            price and a schedule date. ``None`` otherwise. An empty history
            leaves the sum published.
        observed_normalized_daily_scanned_tib: Sum of published scanned rates.
        normalized_daily_scanned_tib: That sum under the same completeness rule.
        observed_normalized_daily_billed_tib: Sum of published billed rates.
        normalized_daily_billed_tib: That sum under the same completeness rule.
        by_frequency: Slices for ``daily``, ``hourly``, and ``other``.
        by_enabled: Slices for ``enabled``, ``disabled``, and ``unknown``.
        configs_with_no_observed_runs: Configs with an empty history.
        configs_missing_cost_evidence: Configs with no runs or no published rate.
        execution_costs_complete: Every recorded execution has a price. An
            empty history is complete.
        daily_rate_complete: Every recorded execution has a price and a
            schedule date. An empty history is complete.
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
