from alpaca_market_agent.tick import build_account_state


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
