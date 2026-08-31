from datetime import UTC, datetime

from alpaca_market_agent.models import EntryWindow, MarketClockState, OrderState, PositionState
from alpaca_market_agent.tick import (
    build_account_state,
    build_exit_reasons,
    mandatory_order_cancellations,
    most_recent_position_close,
)


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
    assert state.daily_loss_floor == 90000
    assert state.daily_loss_headroom == 7000


def test_exit_controls_use_account_and_alpaca_state() -> None:
    now = datetime(2026, 8, 31, 19, 46, tzinfo=UTC)
    account = build_account_state(
        {
            "equity": "89999",
            "last_equity": "100000",
        }
    )
    position = PositionState(
        symbol="SPY260901C00650000",
        asset_class="us_option",
        side="long",
        quantity=15,
        average_entry_price=4,
        current_price=2.5,
        market_value=3750,
        cost_basis=6000,
        unrealized_pnl=-2250,
        unrealized_pnl_percent=-0.375,
    )
    clock = MarketClockState(
        timestamp=now,
        is_open=True,
        next_open=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
        next_close=datetime(2026, 8, 31, 20, 0, tzinfo=UTC),
    )
    window = EntryWindow(
        state="closing_only",
        entry_opens_at=datetime(2026, 8, 31, 13, 40, tzinfo=UTC),
        entry_closes_at=datetime(2026, 8, 31, 19, 45, tzinfo=UTC),
        session_closes_at=datetime(2026, 8, 31, 20, 0, tzinfo=UTC),
    )

    assert build_exit_reasons(
        clock=clock,
        window=window,
        account=account,
        positions=[position],
    ) == [
        "daily_loss_limit",
        "session_close",
        "premium_loss_limit:SPY260901C00650000",
    ]
    assert most_recent_position_close(
        [
            {
                "asset_class": "us_option",
                "side": "sell",
                "status": "filled",
                "filled_at": "2026-08-31T19:30:00Z",
            }
        ]
    ) == datetime(2026, 8, 31, 19, 30, tzinfo=UTC)
    order = OrderState(
        order_id="entry-order",
        client_order_id="entry-client-order",
        symbol=position.symbol,
        asset_class="us_option",
        side="buy",
        order_type="limit",
        time_in_force="day",
        quantity=15,
        filled_quantity=0,
        limit_price=4,
        status="accepted",
        submitted_at=datetime(2026, 8, 31, 19, 45, tzinfo=UTC),
    )
    assert mandatory_order_cancellations(
        evaluated_at=now,
        window=window,
        account=account,
        orders=[order],
    ) == ["entry-order"]
