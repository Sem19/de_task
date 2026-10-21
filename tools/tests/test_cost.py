"""Pricing and daily-normalization tests for the cost engine."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from cost import (
    BYTES_PER_TIB,
    MINIMUM_BILLED_BYTES,
    USD_PER_TIB,
    bytes_to_tib,
    calculate_estate,
    cost_usd,
    estimate_billed_bytes,
    present_usd,
    sum_published,
)
from estate import load_inventory
from estate.sources import CATALOG_COLUMNS, HISTORY_COLUMNS, USAGE_COLUMNS

_REPO = Path(__file__).resolve().parents[2]
_TIB = BYTES_PER_TIB


def test_bytes_to_tib_uses_binary_tebibyte() -> None:
    assert bytes_to_tib(_TIB) == Decimal(1)
    assert bytes_to_tib(_TIB // 2) == Decimal("0.5")


def test_one_tib_costs_six_dollars_twenty_five_cents() -> None:
    assert cost_usd(_TIB) == Decimal("6.25")
    assert USD_PER_TIB == Decimal("6.25")
    assert BYTES_PER_TIB == 1024**4


def test_fractional_tib_is_not_rounded() -> None:
    assert cost_usd(_TIB // 2) == Decimal("3.125")
    assert cost_usd(_TIB // 2) != present_usd(cost_usd(_TIB // 2), places=2)


def test_minimum_billing_is_ten_decimal_megabytes_for_a_small_success() -> None:
    billed, note = estimate_billed_bytes("SUCCEEDED", 1)
    assert MINIMUM_BILLED_BYTES == 10_000_000
    assert billed == 10_000_000
    assert note == "minimum billing applied"
    at_floor, floor_note = estimate_billed_bytes("SUCCEEDED", 10_000_000)
    assert at_floor == 10_000_000
    assert floor_note == ""
    above, above_note = estimate_billed_bytes("SUCCEEDED", 10_000_001)
    assert above == 10_000_001
    assert above_note == ""


def test_failed_execution_is_not_charged_the_minimum(tmp_path: Path) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [_query("sq_fail")],
            [_run("sq_fail", "r1", "2026-06-01T03:00:00+00:00", "FAILED", 5_000)],
        )
    )
    run = estate.runs[0]
    assert run.recorded_bytes == 5_000
    assert run.estimated_billed_bytes == 0
    assert run.estimated_cost_usd == Decimal(0)
    assert run.chargeable is False
    assert "not billed" in run.qualification
    config = estate.configs[0]
    assert config.historical_estimated_cost_usd == Decimal(0)
    assert config.recorded_bytes == 5_000
    assert config.normalized_daily_billed_tib == Decimal(0)


def test_successful_zero_byte_execution_is_not_charged(tmp_path: Path) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [_query("sq_zero")],
            [_run("sq_zero", "r1", "2026-06-01T03:00:00+00:00", "SUCCEEDED", 0)],
        )
    )
    run = estate.runs[0]
    config = estate.configs[0]
    assert run.estimated_billed_bytes == 0
    assert run.estimated_cost_usd == Decimal(0)
    assert "zero-byte" in run.qualification
    assert config.no_observed_runs is False
    assert config.missing_cost_evidence is False
    assert config.normalized_daily_run_rate_usd == Decimal(0)


def test_runs_on_the_same_schedule_date_share_one_denominator(tmp_path: Path) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [_query("sq_same")],
            [
                _run("sq_same", "r1", "2026-06-01T03:00:00+00:00", "SUCCEEDED", _TIB),
                _run("sq_same", "r2", "2026-06-01T15:00:00+00:00", "SUCCEEDED", _TIB),
            ],
        )
    )
    config = estate.configs[0]
    assert config.observed_schedule_dates == (date(2026, 6, 1),)
    assert config.historical_estimated_cost_usd == Decimal("12.50")
    assert config.normalized_daily_run_rate_usd == Decimal("12.50")
    assert config.normalized_daily_scanned_tib == Decimal(2)


def test_daily_rate_uses_observed_dates_not_the_calendar_span(tmp_path: Path) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [_query("sq_daily", schedule="every day 03:00")],
            [
                _run("sq_daily", "r1", "2026-06-01T03:00:00+00:00", "SUCCEEDED", _TIB),
                _run("sq_daily", "r2", "2026-06-03T03:00:00+00:00", "SUCCEEDED", _TIB),
            ],
        )
    )
    config = estate.configs[0]
    assert config.frequency == "daily"
    assert config.calendar_span_days == 3
    assert config.schedule_dates_contiguous is False
    assert len(config.observed_schedule_dates) == 2
    assert config.normalized_daily_scanned_tib == Decimal(1)
    assert config.normalized_daily_run_rate_usd == Decimal("6.25")


def test_hourly_rate_averages_a_day_of_runs_over_observed_dates(tmp_path: Path) -> None:
    runs = []
    for day, hour_a, hour_b in (
        ("2026-06-01", "01:00:00", "02:00:00"),
        ("2026-06-02", "01:00:00", "02:00:00"),
        ("2026-06-03", "01:00:00", "02:00:00"),
    ):
        runs.append(
            _run("sq_hour", f"{day}a", f"{day}T{hour_a}+00:00", "SUCCEEDED", _TIB)
        )
        runs.append(
            _run("sq_hour", f"{day}b", f"{day}T{hour_b}+00:00", "SUCCEEDED", _TIB)
        )
    estate = calculate_estate(
        _load(tmp_path, [_query("sq_hour", schedule="every 1 hours")], runs)
    )
    config = estate.configs[0]
    assert config.frequency == "hourly"
    assert config.execution_count == 6
    assert len(config.observed_schedule_dates) == 3
    assert config.schedule_dates_contiguous is True
    assert config.normalized_daily_scanned_tib == Decimal(2)
    assert config.normalized_daily_run_rate_usd == Decimal("12.50")


def test_estate_rate_sums_each_configs_own_window(tmp_path: Path) -> None:
    daily_runs = [
        _run(
            "sq_daily",
            f"d{day}",
            f"2026-06-{day:02d}T03:00:00+00:00",
            "SUCCEEDED",
            _TIB,
        )
        for day in range(1, 5)
    ]
    hourly_runs = [
        _run(
            "sq_hour",
            f"h{slot}",
            f"2026-07-{day:02d}T0{hour}:00:00+00:00",
            "SUCCEEDED",
            _TIB,
        )
        for day in (1, 2)
        for hour, slot in ((1, f"{day}a"), (2, f"{day}b"))
    ]
    estate = calculate_estate(
        _load(
            tmp_path,
            [
                _query("sq_daily", schedule="every day 03:00"),
                _query("sq_hour", schedule="every 1 hours"),
            ],
            [*daily_runs, *hourly_runs],
        )
    )
    by_id = {config.config_id: config for config in estate.configs}
    assert by_id["sq_daily"].normalized_daily_run_rate_usd == Decimal("6.25")
    assert by_id["sq_hour"].normalized_daily_run_rate_usd == Decimal("12.50")
    assert len(by_id["sq_daily"].observed_schedule_dates) == 4
    assert len(by_id["sq_hour"].observed_schedule_dates) == 2
    assert estate.normalized_daily_run_rate_usd == Decimal("18.75")
    assert estate.normalized_daily_scanned_tib == Decimal(3)
    pooled_over_daily_window = (
        Decimal(8 * _TIB) / Decimal(4) / Decimal(_TIB) * Decimal("6.25")
    )
    assert estate.normalized_daily_run_rate_usd != pooled_over_daily_window


def test_config_with_no_history_is_not_given_a_zero_cost(tmp_path: Path) -> None:
    estate = calculate_estate(_load(tmp_path, [_query("sq_new")], []))
    config = estate.configs[0]
    assert config.no_observed_runs is True
    assert config.missing_cost_evidence is True
    assert config.historical_estimated_cost_usd is None
    assert config.normalized_daily_run_rate_usd is None
    assert config.observed_priced_cost_usd == Decimal(0)
    assert estate.configs_with_no_observed_runs == 1
    assert estate.normalized_daily_run_rate_usd == Decimal(0)
    assert estate.query_count == 1


def test_disabled_config_is_priced_from_history_and_kept_when_absent(
    tmp_path: Path,
) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [
                _query("sq_off", state="DISABLED", disabled=True),
                _query("sq_idle", state="DISABLED", disabled=True),
            ],
            [_run("sq_off", "r1", "2026-06-01T03:00:00+00:00", "SUCCEEDED", _TIB)],
        )
    )
    by_id = {config.config_id: config for config in estate.configs}
    assert by_id["sq_off"].enabled is False
    assert by_id["sq_off"].normalized_daily_run_rate_usd == Decimal("6.25")
    assert by_id["sq_idle"].enabled is False
    assert by_id["sq_idle"].normalized_daily_run_rate_usd is None
    disabled = estate.by_enabled[1]
    assert disabled.label == "disabled"
    assert disabled.config_count == 2
    assert disabled.configs_with_run_rate == 1
    assert disabled.normalized_daily_run_rate_usd == Decimal("6.25")


def test_invalid_byte_counts_stay_in_the_result(tmp_path: Path) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [_query("sq_bad"), _query("sq_worse")],
            [
                _run("sq_bad", "r1", "2026-06-01T03:00:00+00:00", "SUCCEEDED", -1),
                _run(
                    "sq_worse", "r2", "2026-06-01T03:00:00+00:00", "SUCCEEDED", "12.5"
                ),
                _run("sq_worse", "r3", "2026-06-02T03:00:00+00:00", "SUCCEEDED", _TIB),
            ],
        )
    )
    by_id = {config.config_id: config for config in estate.configs}
    negative = next(run for run in estate.runs if run.run_id == "r1")
    malformed = next(run for run in estate.runs if run.run_id == "r2")
    assert negative.recorded_bytes is None
    assert "negative" in negative.qualification
    assert malformed.estimated_cost_usd is None
    assert "malformed" in malformed.qualification
    assert by_id["sq_bad"].historical_estimated_cost_usd is None
    assert by_id["sq_bad"].missing_cost_evidence is True
    assert by_id["sq_worse"].observed_priced_cost_usd == Decimal("6.25")
    assert by_id["sq_worse"].normalized_daily_run_rate_usd is None
    assert estate.historical_estimated_cost_usd is None
    assert estate.execution_costs_complete is False
    assert len(estate.runs) == 3


def test_duplicate_run_ids_are_both_kept(tmp_path: Path) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [_query("sq_dup")],
            [
                _run("sq_dup", "same", "2026-06-01T03:00:00+00:00", "SUCCEEDED", _TIB),
                _run("sq_dup", "same", "2026-06-02T03:00:00+00:00", "SUCCEEDED", _TIB),
            ],
        )
    )
    assert len(estate.runs) == 2
    assert all(run.duplicate_run_id for run in estate.runs)
    assert all("duplicate run_id" in run.qualification for run in estate.runs)
    assert estate.configs[0].historical_estimated_cost_usd == Decimal("12.50")
    assert estate.configs[0].normalized_daily_run_rate_usd == Decimal("6.25")


def test_missing_schedule_time_does_not_drop_the_execution(tmp_path: Path) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [_query("sq_time")],
            [
                {
                    **_run(
                        "sq_time", "r1", "2026-06-01T03:00:00+00:00", "SUCCEEDED", _TIB
                    ),
                    "schedule_time": "not-a-timestamp",
                    "start_time": "",
                    "end_time": "yesterday",
                }
            ],
        )
    )
    run = estate.runs[0]
    assert run.schedule_time == "not-a-timestamp"
    assert run.schedule_date is None
    assert run.estimated_cost_usd == Decimal("6.25")
    assert "invalid schedule_time" in run.qualification
    assert "missing start_time" in run.qualification
    assert "invalid end_time" in run.qualification
    assert estate.configs[0].normalized_daily_run_rate_usd is None
    assert estate.daily_rate_complete is False
    assert estate.configs[0].sql_path == "scheduled_queries/sq_time__marts__out.sql"


def test_ranking_is_by_daily_rate_then_config_id(tmp_path: Path) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [
                _query("sq_b"),
                _query("sq_a"),
                _query("sq_z"),
                _query("sq_m"),
                _query("sq_0"),
            ],
            [
                _run("sq_b", "b", "2026-06-01T03:00:00+00:00", "SUCCEEDED", _TIB),
                _run("sq_a", "a", "2026-06-01T03:00:00+00:00", "SUCCEEDED", _TIB),
                _run("sq_z", "z", "2026-06-01T03:00:00+00:00", "SUCCEEDED", _TIB * 2),
            ],
        )
    )
    assert [config.config_id for config in estate.configs] == [
        "sq_z",
        "sq_a",
        "sq_b",
        "sq_0",
        "sq_m",
    ]
    again = calculate_estate(load_inventory(tmp_path))
    assert [config.config_id for config in again.configs] == [
        config.config_id for config in estate.configs
    ]


def test_presentation_rounding_does_not_change_the_stored_estimate() -> None:
    billed, _note = estimate_billed_bytes("SUCCEEDED", 1)
    stored = cost_usd(billed)
    assert stored == cost_usd(MINIMUM_BILLED_BYTES)
    assert stored > cost_usd(1)
    assert present_usd(stored) != stored
    assert present_usd(Decimal("1.005")) == Decimal("1.01")
    assert present_usd(Decimal("6.25")) == Decimal("6.25")


def test_unexpected_state_is_kept_and_not_billed(tmp_path: Path) -> None:
    estate = calculate_estate(
        _load(
            tmp_path,
            [_query("sq_odd")],
            [_run("sq_odd", "r1", "2026-06-01T03:00:00+00:00", "CANCELLED", _TIB)],
        )
    )
    run = estate.runs[0]
    assert run.state == "CANCELLED"
    assert run.estimated_billed_bytes == 0
    assert "unexpected state" in run.qualification
    assert estate.configs[0].other_state_count == 1
    assert estate.configs[0].normalized_daily_run_rate_usd == Decimal(0)


def test_real_estate_reconciles_without_a_shared_ninety_day_divisor() -> None:
    root = _REPO / "task2_estate"
    before = _digest(root)
    inventory = load_inventory(root)
    estate = calculate_estate(inventory)
    assert _digest(root) == before

    assert estate.query_count == 129
    assert len(estate.configs) == 129
    assert {config.config_id for config in estate.configs} == {
        query.config_id for query in inventory.queries
    }
    assert estate.execution_costs_complete is True
    assert estate.daily_rate_complete is True
    assert estate.historical_estimated_cost_usd == sum_published(
        config.historical_estimated_cost_usd or Decimal(0) for config in estate.configs
    )
    assert estate.normalized_daily_run_rate_usd == sum_published(
        config.normalized_daily_run_rate_usd
        for config in estate.configs
        if config.normalized_daily_run_rate_usd is not None
    )
    assert estate.normalized_daily_scanned_tib == sum_published(
        slice_cost.normalized_daily_scanned_tib for slice_cost in estate.by_frequency
    )
    assert estate.normalized_daily_run_rate_usd == sum_published(
        slice_cost.normalized_daily_run_rate_usd for slice_cost in estate.by_enabled
    )
    rated = [
        config
        for config in estate.configs
        if config.normalized_daily_run_rate_usd is not None
    ]
    rates = [config.normalized_daily_run_rate_usd for config in rated]
    assert rates == sorted(rates, reverse=True)
    for earlier, later in zip(rated, rated[1:]):
        if earlier.normalized_daily_run_rate_usd == later.normalized_daily_run_rate_usd:
            assert earlier.config_id < later.config_id

    hourly_days = [
        len(config.observed_schedule_dates)
        for config in estate.configs
        if config.frequency == "hourly" and config.observed_schedule_dates
    ]
    daily_days = [
        len(config.observed_schedule_dates)
        for config in estate.configs
        if config.frequency == "daily" and config.observed_schedule_dates
    ]
    assert hourly_days and daily_days
    assert max(hourly_days) < min(daily_days)

    idle = next(config for config in estate.configs if config.config_id == "sq_5019")
    assert idle.no_observed_runs is True
    assert idle.normalized_daily_run_rate_usd is None
    assert idle.enabled is False
    assert estate.configs_missing_cost_evidence == 1

    independent = _independent_rates(inventory)
    for config in estate.configs:
        if config.config_id not in independent:
            assert config.no_observed_runs is True
            continue
        recorded, days = independent[config.config_id]
        assert config.recorded_bytes == recorded
        assert len(config.observed_schedule_dates) == days


def _independent_rates(inventory) -> dict[str, tuple[int, int]]:
    """Reproduce the per-config byte and date totals directly from history rows."""
    recorded: dict[str, int] = {}
    dates: dict[str, set[str]] = {}
    for row in inventory.history:
        config_id = row.config_id
        recorded[config_id] = recorded.get(config_id, 0) + int(
            row.raw["total_bytes_processed"]
        )
        dates.setdefault(config_id, set()).add(row.raw["schedule_time"][:10])
    return {
        config_id: (recorded[config_id], len(dates[config_id]))
        for config_id in recorded
    }


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _query(
    config_id: str,
    *,
    schedule: str = "every day 03:00",
    state: str = "ENABLED",
    disabled: bool = False,
) -> dict[str, object]:
    return {
        "name": f"projects/acme/locations/eu/transferConfigs/{config_id}",
        "displayName": config_id,
        "destinationDatasetId": "marts",
        "schedule": schedule,
        "scheduleOptionsV2": {"timeZone": "Europe/Kyiv"},
        "state": state,
        "disabled": disabled,
        "params": {
            "query": "SELECT 1 AS value\n",
            "destination_table_name_template": "out",
            "write_disposition": "WRITE_TRUNCATE",
        },
        "_meta_sql_file": f"scheduled_queries/{config_id}__marts__out.sql",
    }


def _run(
    config_id: str,
    run_id: str,
    schedule_time: str,
    state: str,
    total_bytes: int | str,
) -> dict[str, str]:
    return {
        "run_id": run_id,
        "config_id": config_id,
        "destination": "marts.out",
        "schedule_time": schedule_time,
        "start_time": schedule_time,
        "end_time": schedule_time,
        "state": state,
        "error_message": "" if state == "SUCCEEDED" else "failed",
        "total_bytes_processed": str(total_bytes),
        "rows_written": "0",
    }


def _load(root: Path, queries: list[dict[str, object]], runs: list[dict[str, str]]):
    sql_dir = root / "scheduled_queries"
    sql_dir.mkdir(parents=True)
    for query in queries:
        config_id = str(query["displayName"])
        (sql_dir / f"{config_id}__marts__out.sql").write_text(
            "SELECT 1 AS value\n", encoding="utf-8"
        )
    (root / "transfer_configs.json").write_text(
        json.dumps({"transferConfigs": queries}), encoding="utf-8"
    )
    with (root / "run_history.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_COLUMNS)
        writer.writeheader()
        writer.writerows(runs)
    (root / "downstream_usage.csv").write_text(
        ",".join(USAGE_COLUMNS) + "\n", encoding="utf-8"
    )
    (root / "table_catalog.csv").write_text(
        ",".join(CATALOG_COLUMNS) + "\n", encoding="utf-8"
    )
    (root / "schemas.json").write_text("{}", encoding="utf-8")
    return load_inventory(root)
