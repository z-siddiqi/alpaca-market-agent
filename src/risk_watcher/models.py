from dataclasses import dataclass
from datetime import datetime


def number(value: object) -> float:
    return float(value or 0)


@dataclass(frozen=True)
class Clock:
    timestamp: datetime
    is_open: bool
    next_close: datetime

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "Clock":
        return cls(
            timestamp=datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00")),
            is_open=bool(payload["is_open"]),
            next_close=datetime.fromisoformat(str(payload["next_close"]).replace("Z", "+00:00")),
        )


@dataclass(frozen=True)
class Account:
    equity: float
    session_starting_equity: float

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "Account":
        return cls(
            equity=number(payload.get("equity")),
            session_starting_equity=number(payload.get("last_equity")),
        )

    @property
    def daily_loss_fraction(self) -> float:
        if self.session_starting_equity <= 0:
            return 0
        return (self.session_starting_equity - self.equity) / self.session_starting_equity


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    average_entry_price: float

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "Position":
        quantity = number(payload.get("qty"))
        if quantity <= 0 or not quantity.is_integer():
            raise ValueError(f"invalid option quantity for {payload.get('symbol')}: {quantity}")
        return cls(
            symbol=str(payload["symbol"]),
            quantity=int(quantity),
            average_entry_price=number(payload.get("avg_entry_price")),
        )


@dataclass(frozen=True)
class Order:
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: int
    stop_price: float | None
    limit_price: float | None

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "Order":
        return cls(
            order_id=str(payload["id"]),
            symbol=str(payload.get("symbol", "")),
            side=str(payload.get("side", "")),
            order_type=str(payload.get("type", "")),
            quantity=int(number(payload.get("qty"))),
            stop_price=(
                number(payload.get("stop_price"))
                if payload.get("stop_price") is not None
                else None
            ),
            limit_price=(
                number(payload.get("limit_price"))
                if payload.get("limit_price") is not None
                else None
            ),
        )

    @property
    def protective_stop(self) -> bool:
        return self.side == "sell" and self.order_type in {"stop", "stop_limit"}


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    bid_size: float
    timestamp: datetime

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "Quote":
        return cls(
            bid=number(payload.get("bp")),
            ask=number(payload.get("ap")),
            bid_size=number(payload.get("bs")),
            timestamp=datetime.fromisoformat(str(payload["t"]).replace("Z", "+00:00")),
        )


@dataclass(frozen=True)
class TradePlan:
    decision_id: str
    option_symbol: str
    created_at: datetime
    premium_loss_fraction: float
    breakeven_trigger_fraction: float
    profit_target_fraction: float

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "TradePlan":
        return cls(
            decision_id=str(payload["decisionId"]),
            option_symbol=str(payload["optionSymbol"]),
            created_at=datetime.fromisoformat(str(payload["createdAt"]).replace("Z", "+00:00")),
            premium_loss_fraction=number(payload["premiumLossFraction"]),
            breakeven_trigger_fraction=number(payload["breakevenTriggerFraction"]),
            profit_target_fraction=number(payload["profitTargetFraction"]),
        )
