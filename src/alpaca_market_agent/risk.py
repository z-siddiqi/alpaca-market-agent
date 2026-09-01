import asyncio
import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, time

from alpaca_market_agent.alpaca import AlpacaClient
from alpaca_market_agent.config import Settings
from alpaca_market_agent.models import (
    AgentDecision,
    DecisionRecord,
    OrderState,
    PositionState,
    TickContext,
    ToolCallRecord,
)
from alpaca_market_agent.policy import (
    PREMIUM_BREAKER_FRACTION,
)
from alpaca_market_agent.tick import ET, build_orders, build_positions


@dataclass(frozen=True)
class RiskReconciliation:
    context: TickContext
    actions: list[ToolCallRecord]
    mandatory_exit: bool = False


def protective_stop_price(
    position: PositionState,
    existing_stop_price: float | None = None,
    loss_fraction: float = PREMIUM_BREAKER_FRACTION,
) -> float:
    stop_price = position.average_entry_price * (1 - loss_fraction)
    return max(0.01, round(stop_price, 2), existing_stop_price or 0)


def is_protective_stop(order: OrderState) -> bool:
    return (
        order.side == "sell"
        and order.order_type in {"stop", "stop_limit"}
        and order.position_intent in {None, "sell_to_close"}
    )


class PositionRiskManager:
    def __init__(self, settings: Settings, alpaca: AlpacaClient) -> None:
        self.settings = settings
        self.alpaca = alpaca

    async def reconcile(self, context: TickContext) -> RiskReconciliation:
        if not self.settings.order_submission_enabled:
            return RiskReconciliation(context=context, actions=[])
        if not self.settings.alpaca_paper_trade:
            raise ValueError("position risk manager is paper-account only")

        actions: list[ToolCallRecord] = []
        cancelled: set[str] = set()
        orders = context.working_orders

        for order_id in context.cancel_order_ids:
            await self.alpaca.cancel_order(order_id)
            cancelled.add(order_id)
            actions.append(self._action("cancel_order_by_id", {"order_id": order_id}))

        if cancelled:
            orders = build_orders(await self.alpaca.open_orders())
            context = context.model_copy(update={"working_orders": orders, "cancel_order_ids": []})

        if context.exit_reasons and context.positions:
            symbols = {position.symbol for position in context.positions}
            for order in orders:
                if (
                    order.symbol in symbols
                    and order.order_id not in cancelled
                    and (order.side == "buy" or is_protective_stop(order))
                ):
                    await self.alpaca.cancel_order(order.order_id)
                    cancelled.add(order.order_id)
                    actions.append(self._action("cancel_order_by_id", {"order_id": order.order_id}))

            if cancelled:
                orders = build_orders(await self.alpaca.open_orders())

            active_exits = {
                order.symbol
                for order in orders
                if order.side == "sell" and not is_protective_stop(order)
            }
            for position in context.positions:
                if position.symbol in active_exits:
                    continue
                result = await self.alpaca.close_position(position.symbol)
                actions.append(
                    self._action(
                        "close_position",
                        {"symbol_or_asset_id": position.symbol},
                        result or {"status": "already_flat"},
                    )
                )

            orders = build_orders(await self.alpaca.open_orders())
            return RiskReconciliation(
                context=context.model_copy(update={"working_orders": orders}),
                actions=actions,
                mandatory_exit=True,
            )

        for position in context.positions:
            actions.extend(await self._ensure_stop(context, position, orders))
            if actions:
                orders = build_orders(await self.alpaca.open_orders())
                context = context.model_copy(update={"working_orders": orders})

        return RiskReconciliation(context=context, actions=actions)

    async def settle_entry(self, context: TickContext, symbol: str) -> RiskReconciliation:
        if not self.settings.order_submission_enabled:
            return RiskReconciliation(context=context, actions=[])

        await asyncio.sleep(1)
        actions: list[ToolCallRecord] = []
        for attempt in range(16):
            position_payloads, order_payloads = await asyncio.gather(
                self.alpaca.positions(),
                self.alpaca.open_orders(),
            )
            matching_buys = [
                order
                for order in order_payloads
                if order.get("symbol") == symbol and order.get("side") == "buy"
            ]
            if not matching_buys:
                break
            if attempt == 15:
                for order in matching_buys:
                    order_id = str(order["id"])
                    await self.alpaca.cancel_order(order_id)
                    actions.append(self._action("cancel_order_by_id", {"order_id": order_id}))
                position_payloads, order_payloads = await asyncio.gather(
                    self.alpaca.positions(),
                    self.alpaca.open_orders(),
                )
                break
            await asyncio.sleep(2)

        updated = context.model_copy(
            update={
                "positions": build_positions(position_payloads),
                "working_orders": build_orders(order_payloads),
            }
        )
        reconciled = await self.reconcile(updated)
        return RiskReconciliation(
            context=reconciled.context,
            actions=[*actions, *reconciled.actions],
            mandatory_exit=reconciled.mandatory_exit,
        )

    async def _ensure_stop(
        self,
        context: TickContext,
        position: PositionState,
        orders: list[OrderState],
    ) -> list[ToolCallRecord]:
        symbol_orders = [order for order in orders if order.symbol == position.symbol]
        if any(order.side == "buy" for order in symbol_orders):
            return []
        if any(order.side == "sell" and not is_protective_stop(order) for order in symbol_orders):
            return []

        quantity = int(position.quantity)
        if quantity <= 0 or not math.isclose(position.quantity, quantity):
            raise ValueError(f"option position quantity is invalid: {position.quantity}")
        protective_orders = [order for order in symbol_orders if is_protective_stop(order)]
        existing_stop_price = max(
            (order.stop_price or 0 for order in protective_orders),
            default=0,
        )
        plan = context.trade_plan
        stop_price = protective_stop_price(
            position,
            existing_stop_price,
            loss_fraction=(
                plan.premium_loss_fraction
                if plan is not None and plan.option_symbol == position.symbol
                else PREMIUM_BREAKER_FRACTION
            ),
        )
        if any(
            order.quantity == quantity
            and order.stop_price is not None
            and math.isclose(order.stop_price, stop_price, rel_tol=0, abs_tol=1e-9)
            for order in protective_orders
        ):
            return []

        actions: list[ToolCallRecord] = []
        for order in protective_orders:
            await self.alpaca.cancel_order(order.order_id)
            actions.append(self._action("cancel_order_by_id", {"order_id": order.order_id}))

        entry_order_id = await self._entry_order_id(context, position.symbol)
        digest = hashlib.sha256(
            f"{entry_order_id}:{position.symbol}:{quantity}".encode()
        ).hexdigest()[:20]
        client_order_id = f"augur-stop-{digest}"
        result = await self.alpaca.submit_option_stop(
            symbol=position.symbol,
            quantity=quantity,
            stop_price=stop_price,
            client_order_id=client_order_id,
        )
        actions.append(
            self._action(
                "place_protective_stop",
                {
                    "symbol": position.symbol,
                    "qty": quantity,
                    "side": "sell",
                    "type": "stop",
                    "stop_price": stop_price,
                    "time_in_force": "day",
                    "position_intent": "sell_to_close",
                    "client_order_id": client_order_id,
                },
                result,
            )
        )
        return actions

    async def _entry_order_id(self, context: TickContext, symbol: str) -> str:
        market_start = datetime.combine(context.trading_date, time(4), ET)
        closed = await self.alpaca.closed_orders(after=market_start)
        entries = [
            order
            for order in closed
            if order.get("symbol") == symbol
            and order.get("side") == "buy"
            and float(order.get("filled_qty") or 0) > 0
        ]
        entries.sort(
            key=lambda order: str(order.get("filled_at") or order.get("submitted_at") or ""),
            reverse=True,
        )
        if entries:
            return str(entries[0]["id"])
        return (
            f"{context.trading_date.isoformat()}:{symbol}:"
            f"{context.evaluated_at.astimezone(UTC).isoformat()}"
        )

    @staticmethod
    def _action(
        name: str,
        arguments: dict[str, object],
        result: object | None = None,
    ) -> ToolCallRecord:
        return ToolCallRecord(
            name=name,
            arguments=arguments,
            result=result or {"status": "requested"},
            blocked=False,
            called_at=datetime.now(UTC),
        )


def forced_exit_record(
    *,
    tick_id: str,
    reconciliation: RiskReconciliation,
) -> DecisionRecord:
    context = reconciliation.context
    reasons = context.exit_reasons
    decision = AgentDecision(
        decision_id=tick_id,
        evaluated_at=context.evaluated_at,
        action="close_position",
        auction_state="unclear",
        confidence=1,
        thesis="Mandatory runtime exit: " + ", ".join(reasons),
        active_references=[position.symbol for position in context.positions],
        evidence=["mandatory risk controls execute before the model"],
        policy_checks=reasons,
    )
    return DecisionRecord(
        tick_id=tick_id,
        trading_date=context.trading_date,
        model="runtime-risk-controller",
        context=context,
        decision=decision,
        tool_calls=reconciliation.actions,
        created_at=datetime.now(UTC),
    )
