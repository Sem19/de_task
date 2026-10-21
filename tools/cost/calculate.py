"""On-demand price of one recorded execution.

The assignment states on-demand pricing of approximately $6.25 per TiB
scanned, with a minimum of 10 MB per query. TiB is 1024^4 bytes. The
assignment says MB, so the minimum is the decimal megabyte, 10,000,000 bytes,
not 10 MiB.

The minimum is applied only to a successful execution whose recorded byte
count is greater than zero. A failed execution is not given the minimum, and
a successful execution that recorded zero bytes is not given the minimum
either. Both are estimated at zero billed bytes. ``total_bytes_processed``
remains the recorded figure either way. This is not an invoice.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext

from cost.models import RunCost

USD_PER_TIB = Decimal("6.25")
BYTES_PER_TIB = 1024**4
MINIMUM_BILLED_BYTES = 10_000_000
# Day counts such as 90 make byte/TiB ratios repeating decimals. Each published
# quotient keeps this many significant digits. Sums of those quotients use a
# wider precision so the estate total matches the per-config figures exactly.
CALCULATION_PRECISION = 50
_PUBLISH = Context(prec=CALCULATION_PRECISION, rounding=ROUND_HALF_UP)
_SUM = Context(prec=CALCULATION_PRECISION + 20, rounding=ROUND_HALF_UP)

_SUCCEEDED = "SUCCEEDED"
_FAILED = "FAILED"


def bytes_to_tib(num_bytes: int) -> Decimal:
    """Convert a byte count to tebibytes without rounding.

    Args:
        num_bytes: Byte count. Negative values are rejected.

    Returns:
        The exact Decimal number of TiB.

    Raises:
        ValueError: ``num_bytes`` is negative.
    """
    if num_bytes < 0:
        raise ValueError("num_bytes must be >= 0")
    return divide(num_bytes, BYTES_PER_TIB)


def cost_usd(billed_bytes: int) -> Decimal:
    """Price billed bytes at ``USD_PER_TIB`` without rounding.

    Args:
        billed_bytes: Estimated billed bytes. Negative values are rejected.

    Returns:
        Estimated USD for those bytes.

    Raises:
        ValueError: ``billed_bytes`` is negative.
    """
    if billed_bytes < 0:
        raise ValueError("billed_bytes must be >= 0")
    with localcontext(_PUBLISH):
        return bytes_to_tib(billed_bytes) * USD_PER_TIB


def present_usd(amount: Decimal, places: int = 2) -> Decimal:
    """Round a USD amount for display. Calculations themselves stay exact.

    Args:
        amount: Unrounded USD amount.
        places: Decimal places. Defaults to cents.

    Returns:
        ``amount`` quantized with half-up rounding.

    Raises:
        ValueError: ``places`` is negative.
    """
    if places < 0:
        raise ValueError("places must be >= 0")
    with localcontext(Context(prec=max(CALCULATION_PRECISION, places + 20))):
        quantum = Decimal(1).scaleb(-places)
        return amount.quantize(quantum, rounding=ROUND_HALF_UP)


def estimate_billed_bytes(state: str, recorded_bytes: int) -> tuple[int, str]:
    """Estimate billed bytes for one execution with usable recorded bytes.

    Args:
        state: Execution state as stored in run history.
        recorded_bytes: Non-negative ``total_bytes_processed``.

    Returns:
        Estimated billed bytes and a qualification. The qualification is
        empty when a successful scan is already at or above the minimum.
        A failed execution, a successful zero-byte execution, and any other
        state are estimated at zero billed bytes.

    Raises:
        ValueError: ``recorded_bytes`` is negative.
    """
    if recorded_bytes < 0:
        raise ValueError("recorded_bytes must be >= 0")
    if state == _SUCCEEDED and recorded_bytes > 0:
        if recorded_bytes < MINIMUM_BILLED_BYTES:
            return MINIMUM_BILLED_BYTES, "minimum billing applied"
        return recorded_bytes, ""
    if state == _SUCCEEDED:
        return 0, "successful zero-byte execution; minimum billing not applied"
    if state == _FAILED:
        return 0, "failed execution; not billed"
    return 0, "unexpected state; not billed"


def price_run(
    *,
    config_id: str,
    run_id: str,
    schedule_time: str,
    start_time: str,
    end_time: str,
    state: str,
    total_bytes_processed: str,
    duplicate_run_id: bool,
) -> RunCost:
    """Price one history row. The row is returned even when it cannot be priced.

    Timestamp text is kept as supplied. A schedule date is the calendar date
    of that text, with no timezone conversion.

    Args:
        config_id: Scheduled-query id from the history row.
        run_id: Run id. Blank becomes None.
        schedule_time: Schedule timestamp text.
        start_time: Start timestamp text.
        end_time: End timestamp text.
        state: Execution state.
        total_bytes_processed: Recorded byte-count text.
        duplicate_run_id: True when this run id occurs more than once.

    Returns:
        The priced execution, including a qualification for anything that
        limits the estimate.
    """
    notes: list[str] = []
    if duplicate_run_id:
        notes.append("duplicate run_id")
    schedule_day, schedule_note = _schedule_date(schedule_time)
    _append(notes, schedule_note)
    _append(notes, _clock_note("start_time", start_time))
    _append(notes, _clock_note("end_time", end_time))

    recorded, byte_note = _parse_bytes(total_bytes_processed)
    billed: int | None = None
    cost: Decimal | None = None
    chargeable = False
    if byte_note:
        notes.append(byte_note)
    elif recorded is not None:
        billed, billing_note = estimate_billed_bytes(state, recorded)
        cost = cost_usd(billed)
        chargeable = billed > 0
        _append(notes, billing_note)

    return RunCost(
        config_id=config_id,
        run_id=run_id.strip() or None,
        schedule_time=schedule_time.strip() or None,
        start_time=start_time.strip() or None,
        end_time=end_time.strip() or None,
        schedule_date=schedule_day,
        state=state,
        recorded_bytes=recorded,
        estimated_billed_bytes=billed,
        estimated_cost_usd=cost,
        chargeable=chargeable,
        duplicate_run_id=duplicate_run_id,
        qualification="; ".join(notes),
    )


def _parse_bytes(value: str) -> tuple[int | None, str]:
    """Parse a recorded byte count, or explain why it cannot be used.

    Args:
        value: ``total_bytes_processed`` text. Surrounding space is ignored.

    Returns:
        The non-negative count and an empty note, or ``None`` and a note
        for a blank, non-integer, or negative cell.
    """
    text = value.strip()
    if not text:
        return None, "missing total_bytes_processed"
    try:
        parsed = int(text)
    except ValueError:
        return None, "malformed total_bytes_processed"
    if parsed < 0:
        return None, "negative total_bytes_processed"
    return parsed, ""


def _schedule_date(value: str) -> tuple[date | None, str]:
    """Read the calendar date from a schedule timestamp.

    Args:
        value: ``schedule_time`` text. Surrounding space is ignored.

    Returns:
        The date component and an empty note, or ``None`` and a note when
        the cell is blank or not ISO. An offset is not converted.
    """
    text = value.strip()
    if not text:
        return None, "missing schedule_time"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, "invalid schedule_time"
    return parsed.date(), ""


def _clock_note(label: str, value: str) -> str:
    """Return a note when a clock cell is blank or not ISO.

    Args:
        label: Field name written into the note.
        value: Timestamp text. Surrounding space is ignored.

    Returns:
        An empty string when the text is a valid ISO timestamp.
    """
    text = value.strip()
    if not text:
        return f"missing {label}"
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return f"invalid {label}"
    return ""


def divide(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    """Divide and keep ``CALCULATION_PRECISION`` significant digits.

    Args:
        numerator: Integer or already published Decimal.
        denominator: Non-zero integer or Decimal.

    Returns:
        The quotient at the calculation precision, not a display rounding.
    """
    with localcontext(_PUBLISH):
        return Decimal(numerator) / Decimal(denominator)


def sum_published(amounts: Iterable[Decimal]) -> Decimal:
    """Sum published amounts without rounding the total.

    Args:
        amounts: Decimals previously produced at ``CALCULATION_PRECISION``.

    Returns:
        Their exact sum.
    """
    with localcontext(_SUM):
        return sum(amounts, Decimal(0))


def _append(notes: list[str], note: str) -> None:
    """Append ``note`` when it is not empty.

    Args:
        notes: Mutable qualification notes.
        note: One limit. An empty string is skipped.
    """
    if note:
        notes.append(note)
