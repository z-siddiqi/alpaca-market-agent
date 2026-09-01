from decimal import ROUND_HALF_UP, Decimal

from position_watcher.models import Position, TradePlan


def cents(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def loss_stop_price(position: Position, plan: TradePlan) -> float:
    return max(0.01, cents(position.average_entry_price * (1 - plan.premium_loss_fraction)))


def breakeven_trigger_price(position: Position, plan: TradePlan) -> float:
    return cents(
        position.average_entry_price * (1 + plan.breakeven_trigger_fraction),
    )


def profit_target_price(position: Position, plan: TradePlan) -> float:
    return cents(position.average_entry_price * (1 + plan.profit_target_fraction))
