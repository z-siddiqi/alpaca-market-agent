from datetime import UTC, datetime, timedelta

import pytest

from alpaca_market_agent.models import Bar, MarketClockState
from alpaca_market_agent.tick import (
    build_account_state,
    build_entry_window,
    build_live_market_state,
    tick_id,
)


def test_entry_window_uses_reported_close() -> None:
    clock = MarketClockState(
        timestamp=datetime(2026, 8, 28, 14, 0, tzinfo=UTC),
        is_open=True,
        next_open=datetime(2026, 8, 31, 13, 30, tzinfo=UTC),
        next_close=datetime(2026, 8, 28, 17, 0, tzinfo=UTC),
    )

    window = build_entry_window(clock)

    assert window.state == "eligible"
    assert window.entry_closes_at.astimezone(UTC) == datetime(
        2026, 8, 28, 16, 45, tzinfo=UTC
    )


def test_market_state_uses_only_completed_bars() -> None:
    as_of = datetime(2026, 8, 28, 14, 40, tzinfo=UTC)
    bars = [
        Bar(
            timestamp=datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
            + timedelta(minutes=5 * index),
            open=100 + index,
            high=101 + index,
            low=99 + index,
            close=100.5 + index,
            volume=100,
        )
        for index in range(15)
    ]
    snapshot = {
        "latestTrade": {"t": as_of.isoformat(), "p": 115.25, "s": 10},
        "latestQuote": {"t": as_of.isoformat(), "bp": 115.24, "ap": 115.26, "bs": 5, "as": 6},
    }

    state = build_live_market_state(as_of=as_of, bars=bars, snapshot=snapshot)

    assert len(state.recent_completed_bars) == 12
    assert state.recent_completed_bars[-1].timestamp == bars[13].timestamp
    assert state.five_minute_atr14 == pytest.approx(2)
    assert state.latest_price == 115.25
    assert state.five_minute_bars_current


def test_account_state_calculates_daily_loss_headroom() -> None:
    state = build_account_state(
        {
            "status": "ACTIVE",
            "currency": "USD",
            "equity": "97000",
            "last_equity": "100000",
            "buying_power": "10000",
            "options_buying_power": "9000",
            "cash": "8000",
        }
    )

    assert state.daily_equity_pnl == -3000
    assert state.daily_loss_floor == 95000
    assert state.daily_loss_headroom == 2000


def test_tick_id_floors_to_five_minutes() -> None:
    evaluated_at = datetime(2026, 8, 28, 14, 13, 59, tzinfo=UTC)

    assert tick_id(evaluated_at) == "2026-08-28-1010"
