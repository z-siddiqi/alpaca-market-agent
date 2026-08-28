from collections import defaultdict
from datetime import date, time
from decimal import ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from alpaca_market_agent.models import Bar, OpeningContext, PeriodSummary, SessionPerception

ET = ZoneInfo("America/New_York")
ROW_CENTS = 10


def _row_cents(price: float) -> int:
    cents = Decimal(str(price)) * 100
    return int((cents / ROW_CENTS).to_integral_value(rounding=ROUND_FLOOR)) * ROW_CENTS


def _period_index(bar: Bar) -> int:
    local = bar.timestamp.astimezone(ET)
    return ((local.hour * 60 + local.minute) - (9 * 60 + 30)) // 30


def _period_label(index: int) -> str:
    return chr(ord("A") + index)


def _summarize_periods(bars: list[Bar]) -> list[PeriodSummary]:
    grouped: dict[int, list[Bar]] = defaultdict(list)
    for bar in bars:
        grouped[_period_index(bar)].append(bar)

    periods: list[PeriodSummary] = []
    for index in sorted(grouped):
        period_bars = sorted(grouped[index], key=lambda bar: bar.timestamp)
        periods.append(
            PeriodSummary(
                label=_period_label(index),
                start=period_bars[0].timestamp,
                open=period_bars[0].open,
                high=max(bar.high for bar in period_bars),
                low=min(bar.low for bar in period_bars),
                close=period_bars[-1].close,
            )
        )
    return periods


def _build_tpo_value(bars: list[Bar]) -> tuple[float, float, float]:
    periods_by_row: dict[int, set[int]] = defaultdict(set)
    for bar in bars:
        period = _period_index(bar)
        low_row = _row_cents(bar.low)
        high_row = _row_cents(bar.high)
        for row in range(low_row, high_row + ROW_CENTS, ROW_CENTS):
            periods_by_row[row].add(period)

    counts = {row: len(periods) for row, periods in periods_by_row.items()}
    if not counts:
        raise ValueError("cannot build a TPO profile without price rows")

    session_midpoint = (
        _row_cents(min(bar.low for bar in bars)) + _row_cents(max(bar.high for bar in bars))
    ) / 2
    poc_row = min(counts, key=lambda row: (-counts[row], abs(row - session_midpoint), row))

    selected = {poc_row}
    selected_tpos = counts[poc_row]
    target_tpos = sum(counts.values()) * 0.70
    next_above = poc_row + ROW_CENTS
    next_below = poc_row - ROW_CENTS
    minimum_row = min(counts)
    maximum_row = max(counts)

    while selected_tpos < target_tpos:
        above_count = counts.get(next_above, -1) if next_above <= maximum_row else -1
        below_count = counts.get(next_below, -1) if next_below >= minimum_row else -1
        if above_count < 0 and below_count < 0:
            break
        if above_count > below_count:
            selected.add(next_above)
            selected_tpos += max(above_count, 0)
            next_above += ROW_CENTS
        elif below_count > above_count:
            selected.add(next_below)
            selected_tpos += max(below_count, 0)
            next_below -= ROW_CENTS
        else:
            if above_count >= 0:
                selected.add(next_above)
                selected_tpos += above_count
                next_above += ROW_CENTS
            if below_count >= 0:
                selected.add(next_below)
                selected_tpos += below_count
                next_below -= ROW_CENTS

    return poc_row / 100, (max(selected) + ROW_CENTS) / 100, min(selected) / 100


def build_session_perception(
    bars: list[Bar],
    source_session: date,
    *,
    expected_bar_count: int = 390,
) -> SessionPerception:
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    if not ordered:
        raise ValueError("prior session has no bars")

    session_open = ordered[0].open
    session_high = max(bar.high for bar in ordered)
    session_low = min(bar.low for bar in ordered)
    session_close = ordered[-1].close
    session_range = session_high - session_low
    ib_bars = [bar for bar in ordered if _period_index(bar) in (0, 1)]
    if not ib_bars:
        raise ValueError("prior session has no initial-balance bars")

    ib_high = max(bar.high for bar in ib_bars)
    ib_low = min(bar.low for bar in ib_bars)
    poc, vah, val = _build_tpo_value(ordered)

    return SessionPerception(
        source_session=source_session,
        open=session_open,
        high=session_high,
        low=session_low,
        close=session_close,
        range=session_range,
        midpoint=(session_high + session_low) / 2,
        poc=poc,
        vah=vah,
        val=val,
        ib_high=ib_high,
        ib_low=ib_low,
        ib_range=ib_high - ib_low,
        extended_up=session_high > ib_high,
        extended_down=session_low < ib_low,
        extension_up=max(0, session_high - ib_high),
        extension_down=max(0, ib_low - session_low),
        close_location=(session_close - session_low) / session_range if session_range else 0.5,
        periods=_summarize_periods(ordered),
        bar_count=len(ordered),
        expected_bar_count=expected_bar_count,
        complete=len(ordered) >= expected_bar_count,
    )


def aggregate_first_five_minutes(bars: list[Bar]) -> Bar | None:
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    first_five = [
        bar for bar in ordered if time(9, 30) <= bar.timestamp.astimezone(ET).time() < time(9, 35)
    ]
    if len(first_five) < 5:
        return None
    return Bar(
        timestamp=first_five[0].timestamp,
        open=first_five[0].open,
        high=max(bar.high for bar in first_five),
        low=min(bar.low for bar in first_five),
        close=first_five[-1].close,
        volume=sum(bar.volume for bar in first_five),
    )


def build_opening_context(
    plan_session: date,
    perception: SessionPerception,
    opening_bars: list[Bar],
) -> OpeningContext:
    ordered = sorted(opening_bars, key=lambda bar: bar.timestamp)
    if not ordered:
        raise ValueError("plan session has no opening bars")
    current_open = ordered[0].open
    gap_points = current_open - perception.close
    gap_percent = gap_points / perception.close
    absolute_gap = abs(gap_percent)
    if absolute_gap < 0.0015:
        classification = "normal"
    elif absolute_gap < 0.0035:
        classification = "elevated"
    elif absolute_gap < 0.007:
        classification = "large"
    else:
        classification = "extreme"

    if current_open > perception.high:
        location = "true_gap_up"
    elif current_open > perception.vah:
        location = "above_value"
    elif current_open >= perception.val:
        location = "inside_value"
    elif current_open >= perception.low:
        location = "below_value"
    else:
        location = "true_gap_down"

    return OpeningContext(
        plan_session=plan_session,
        open=current_open,
        prior_close=perception.close,
        gap_points=gap_points,
        gap_percent=gap_percent,
        gap_classification=classification,
        location=location,
        first_five_minute=aggregate_first_five_minutes(ordered),
    )
