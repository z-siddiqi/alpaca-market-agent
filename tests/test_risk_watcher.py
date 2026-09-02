from datetime import UTC, datetime

from risk_watcher.models import Position, TradePlan
from risk_watcher.rules import (
    breakeven_trigger_price,
    loss_stop_price,
    profit_target_price,
    spy_target_reached,
)


def test_option_exit_prices_come_from_the_durable_plan() -> None:
    position = Position(
        symbol="SPY260902P00650000",
        quantity=15,
        average_entry_price=2.35,
    )
    plan = TradePlan(
        decision_id="2026-09-01-1000",
        action="buy_put",
        created_at=datetime(2026, 9, 1, 14, tzinfo=UTC),
        target_price=647,
        premium_loss_fraction=0.35,
        breakeven_trigger_fraction=0.20,
        profit_target_fraction=0.50,
    )

    assert loss_stop_price(position, plan) == 1.53
    assert breakeven_trigger_price(position, plan) == 2.82
    assert profit_target_price(position, plan) == 3.53
    assert spy_target_reached(647, plan)
    assert not spy_target_reached(650, plan)
