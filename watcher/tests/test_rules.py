from datetime import UTC, datetime

from position_watcher.models import Position, TradePlan
from position_watcher.rules import (
    breakeven_trigger_price,
    loss_stop_price,
    profit_target_price,
)


def test_option_exit_prices_come_from_the_durable_plan() -> None:
    position = Position(
        symbol="SPY260902P00650000",
        quantity=15,
        average_entry_price=2.35,
    )
    plan = TradePlan(
        decision_id="2026-09-01-1000",
        option_symbol=position.symbol,
        created_at=datetime(2026, 9, 1, 14, tzinfo=UTC),
        premium_loss_fraction=0.35,
        breakeven_trigger_fraction=0.20,
        profit_target_fraction=0.50,
    )

    assert loss_stop_price(position, plan) == 1.53
    assert breakeven_trigger_price(position, plan) == 2.82
    assert profit_target_price(position, plan) == 3.53
