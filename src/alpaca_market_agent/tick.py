import asyncio
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from alpaca_market_agent.alpaca import AlpacaClient
from alpaca_market_agent.models import (
    AccountState,
    Bar,
    EntryWindow,
    LatestQuoteState,
    LatestTradeState,
    LiveMarketState,
    MarketClockState,
    OrderState,
    PositionState,
    TickContext,
    TradePlan,
)
from alpaca_market_agent.policy import (
    DAILY_LOSS_FRACTION,
    PREMIUM_BREAKER_FRACTION,
)
from alpaca_market_agent.storage import NarrativeStore, TradePlanStore

ET = ZoneInfo("America/New_York")
POST_CLOSE_COOLDOWN = timedelta(minutes=10)
ENTRY_ORDER_LIFETIME = timedelta(seconds=30)


def _number(value: Any) -> float:
    return float(value or 0)


def tick_id(evaluated_at: datetime) -> str:
    local = evaluated_at.astimezone(ET)
    minute = local.minute - local.minute % 5
    return f"{local:%Y-%m-%d}-{local.hour:02d}{minute:02d}"


def parse_clock(payload: dict[str, Any]) -> MarketClockState:
    return MarketClockState(
        timestamp=payload["timestamp"],
        is_open=payload["is_open"],
        next_open=payload["next_open"],
        next_close=payload["next_close"],
    )


def build_account_state(payload: dict[str, Any]) -> AccountState:
    equity = _number(payload.get("equity"))
    starting_equity = _number(payload.get("last_equity"))
    daily_pnl = equity - starting_equity
    loss_floor = starting_equity * (1 - DAILY_LOSS_FRACTION)
    return AccountState(
        status=str(payload.get("status", "unknown")),
        currency=str(payload.get("currency", "USD")),
        equity=equity,
        session_starting_equity=starting_equity,
        daily_equity_pnl=daily_pnl,
        daily_equity_pnl_percent=daily_pnl / starting_equity if starting_equity else 0,
        daily_loss_floor=loss_floor,
        daily_loss_headroom=equity - loss_floor,
        buying_power=_number(payload.get("buying_power")),
        options_buying_power=_number(payload.get("options_buying_power")),
        cash=_number(payload.get("cash")),
        account_blocked=bool(payload.get("account_blocked")),
        trading_blocked=bool(payload.get("trading_blocked")),
        trade_suspended_by_user=bool(payload.get("trade_suspended_by_user")),
    )


def build_positions(payloads: list[dict[str, Any]]) -> list[PositionState]:
    return [
        PositionState(
            symbol=str(payload["symbol"]),
            asset_class=str(payload.get("asset_class", "unknown")),
            side=str(payload.get("side", "unknown")),
            quantity=_number(payload.get("qty")),
            average_entry_price=_number(payload.get("avg_entry_price")),
            current_price=_number(payload.get("current_price")),
            market_value=_number(payload.get("market_value")),
            cost_basis=_number(payload.get("cost_basis")),
            unrealized_pnl=_number(payload.get("unrealized_pl")),
            unrealized_pnl_percent=_number(payload.get("unrealized_plpc")),
        )
        for payload in payloads
    ]


def build_orders(payloads: list[dict[str, Any]]) -> list[OrderState]:
    return [
        OrderState(
            order_id=str(payload["id"]),
            client_order_id=str(payload.get("client_order_id", "")),
            symbol=str(payload.get("symbol", "")),
            asset_class=str(payload.get("asset_class", "unknown")),
            side=str(payload.get("side", "unknown")),
            order_type=str(payload.get("type", "unknown")),
            time_in_force=str(payload.get("time_in_force", "unknown")),
            quantity=_number(payload.get("qty")),
            filled_quantity=_number(payload.get("filled_qty")),
            limit_price=(
                _number(payload["limit_price"]) if payload.get("limit_price") is not None else None
            ),
            stop_price=(
                _number(payload["stop_price"]) if payload.get("stop_price") is not None else None
            ),
            position_intent=payload.get("position_intent"),
            status=str(payload.get("status", "unknown")),
            submitted_at=payload.get("submitted_at"),
        )
        for payload in payloads
    ]


def most_recent_position_close(payloads: list[dict[str, Any]]) -> datetime | None:
    closes = [
        datetime.fromisoformat(str(payload["filled_at"]).replace("Z", "+00:00"))
        for payload in payloads
        if payload.get("asset_class") == "us_option"
        and payload.get("side") == "sell"
        and payload.get("status") == "filled"
        and payload.get("filled_at")
    ]
    return max(closes, default=None)


def build_exit_reasons(
    *,
    clock: MarketClockState,
    window: EntryWindow,
    account: AccountState,
    positions: list[PositionState],
    trade_plan: TradePlan | None = None,
) -> list[str]:
    if not positions:
        return []

    reasons: list[str] = []
    if account.daily_loss_headroom <= 0:
        reasons.append("daily_loss_limit")
    if window.state == "closing_only":
        reasons.append("session_close")
    if clock.is_open:
        for position in positions:
            matching_plan = (
                trade_plan if trade_plan is not None and trade_plan.option_symbol == position.symbol
                else None
            )
            loss_fraction = (
                matching_plan.premium_loss_fraction
                if matching_plan is not None
                else PREMIUM_BREAKER_FRACTION
            )
            breaker_price = position.average_entry_price * (1 - loss_fraction)
            if (
                position.asset_class == "us_option"
                and position.average_entry_price > 0
                and position.current_price > 0
                and position.current_price <= breaker_price
            ):
                reasons.append(f"premium_loss_limit:{position.symbol}")
    return reasons


def mandatory_order_cancellations(
    *,
    evaluated_at: datetime,
    window: EntryWindow,
    account: AccountState,
    orders: list[OrderState],
) -> list[str]:
    cancel_all_entries = window.state == "closing_only" or account.daily_loss_headroom <= 0
    return [
        order.order_id
        for order in orders
        if order.side == "buy"
        and (
            cancel_all_entries
            or order.submitted_at is None
            or evaluated_at - order.submitted_at >= ENTRY_ORDER_LIFETIME
        )
    ]


def build_entry_window(clock: MarketClockState) -> EntryWindow:
    local = clock.timestamp.astimezone(ET)
    entry_opens = datetime.combine(local.date(), time(9, 40), ET)
    session_close = clock.next_close.astimezone(ET)
    entry_closes = session_close - timedelta(minutes=15)
    if not clock.is_open:
        state = "market_closed"
    elif local < entry_opens:
        state = "entry_delay"
    elif local >= entry_closes:
        state = "closing_only"
    else:
        state = "eligible"
    return EntryWindow(
        state=state,
        entry_opens_at=entry_opens,
        entry_closes_at=entry_closes,
        session_closes_at=session_close,
    )


def _atr14(bars: list[Bar]) -> float | None:
    if len(bars) < 14:
        return None
    true_ranges: list[float] = []
    previous_close: float | None = None
    for bar in bars:
        true_range = bar.high - bar.low
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        true_ranges.append(true_range)
        previous_close = bar.close
    return sum(true_ranges[-14:]) / 14


def build_live_market_state(
    *,
    as_of: datetime,
    bars: list[Bar],
    snapshot: dict[str, Any],
) -> LiveMarketState:
    local_as_of = as_of.astimezone(ET)
    completed = sorted(
        (bar for bar in bars if bar.timestamp + timedelta(minutes=5) <= as_of),
        key=lambda bar: bar.timestamp,
    )
    premarket = [bar for bar in completed if bar.timestamp.astimezone(ET).time() < time(9, 30)]
    regular = [
        bar for bar in completed if time(9, 30) <= bar.timestamp.astimezone(ET).time() < time(16)
    ]
    initial_balance = [bar for bar in regular if bar.timestamp.astimezone(ET).time() < time(10, 30)]

    trade_payload = snapshot.get("latestTrade")
    latest_trade = (
        LatestTradeState(
            timestamp=trade_payload["t"],
            price=_number(trade_payload.get("p")),
            size=_number(trade_payload.get("s")),
        )
        if trade_payload
        else None
    )
    quote_payload = snapshot.get("latestQuote")
    latest_quote = (
        LatestQuoteState(
            timestamp=quote_payload["t"],
            bid=_number(quote_payload.get("bp")),
            ask=_number(quote_payload.get("ap")),
            bid_size=_number(quote_payload.get("bs")),
            ask_size=_number(quote_payload.get("as")),
        )
        if quote_payload
        else None
    )
    ib_high = max((bar.high for bar in initial_balance), default=None)
    ib_low = min((bar.low for bar in initial_balance), default=None)
    session_high = max((bar.high for bar in regular), default=None)
    session_low = min((bar.low for bar in regular), default=None)
    ib_complete = bool(regular) and local_as_of.time() >= time(10, 30)
    session_open_at = datetime.combine(local_as_of.date(), time(9, 30), ET)
    completed_intervals = max(0, int((local_as_of - session_open_at).total_seconds() // 300))
    expected_latest_start = (
        session_open_at + timedelta(minutes=5 * (completed_intervals - 1))
        if completed_intervals > 0
        else None
    )
    latest_completed_at = regular[-1].timestamp if regular else None
    bars_current = expected_latest_start is None or (
        latest_completed_at is not None and latest_completed_at >= expected_latest_start
    )

    return LiveMarketState(
        as_of=as_of,
        latest_trade=latest_trade,
        latest_quote=latest_quote,
        trade_freshness_seconds=(
            max(0, (as_of - latest_trade.timestamp).total_seconds()) if latest_trade else None
        ),
        regular_session_open=regular[0].open if regular else None,
        regular_session_high=session_high,
        regular_session_low=session_low,
        latest_price=(
            latest_trade.price if latest_trade else (regular[-1].close if regular else None)
        ),
        premarket_high=max((bar.high for bar in premarket), default=None),
        premarket_low=min((bar.low for bar in premarket), default=None),
        initial_balance_high=ib_high,
        initial_balance_low=ib_low,
        initial_balance_complete=ib_complete,
        extension_up=(
            max(0, session_high - ib_high)
            if ib_complete and session_high is not None and ib_high is not None
            else None
        ),
        extension_down=(
            max(0, ib_low - session_low)
            if ib_complete and session_low is not None and ib_low is not None
            else None
        ),
        five_minute_atr14=_atr14(regular),
        latest_completed_bar_at=latest_completed_at,
        five_minute_bars_current=bars_current,
        recent_completed_bars=regular[-12:],
    )


class TickContextBuilder:
    def __init__(
        self,
        alpaca: AlpacaClient,
        store: NarrativeStore,
        trade_plan_store: TradePlanStore,
    ) -> None:
        self.alpaca = alpaca
        self.store = store
        self.trade_plan_store = trade_plan_store

    async def build(self) -> TickContext:
        clock = parse_clock(await self.alpaca.trading_clock())
        evaluated_at = clock.timestamp
        trading_date = evaluated_at.astimezone(ET).date()
        market_start = datetime.combine(trading_date, time(4), ET)

        account_task = asyncio.create_task(self.alpaca.account())
        positions_task = asyncio.create_task(self.alpaca.positions())
        orders_task = asyncio.create_task(self.alpaca.open_orders())
        closed_orders_task = asyncio.create_task(self.alpaca.closed_orders(after=market_start))
        snapshot_task = asyncio.create_task(self.alpaca.stock_snapshot())
        narrative_task = asyncio.create_task(self.store.get(trading_date))
        bars_task = (
            asyncio.create_task(
                self.alpaca.stock_bars(
                    start=market_start,
                    end=evaluated_at,
                    feed="iex",
                    timeframe="5Min",
                )
            )
            if evaluated_at > market_start
            else None
        )

        (
            account_payload,
            position_payloads,
            order_payloads,
            closed_order_payloads,
            snapshot,
            narrative,
        ) = await asyncio.gather(
            account_task,
            positions_task,
            orders_task,
            closed_orders_task,
            snapshot_task,
            narrative_task,
        )
        bars = await bars_task if bars_task is not None else []
        account = build_account_state(account_payload)
        positions = build_positions(position_payloads)
        orders = build_orders(order_payloads)
        trade_plan = await self.trade_plan_store.get_for_symbols(
            [position.symbol for position in positions]
            + [order.symbol for order in orders if order.side == "buy"]
        )
        market = build_live_market_state(as_of=evaluated_at, bars=bars, snapshot=snapshot)
        window = build_entry_window(clock)
        last_position_closed_at = most_recent_position_close(closed_order_payloads)
        cooldown_ends_at = (
            last_position_closed_at + POST_CLOSE_COOLDOWN
            if last_position_closed_at is not None
            else None
        )
        exit_reasons = build_exit_reasons(
            clock=clock,
            window=window,
            account=account,
            positions=positions,
            trade_plan=trade_plan,
        )
        cancel_order_ids = mandatory_order_cancellations(
            evaluated_at=evaluated_at,
            window=window,
            account=account,
            orders=orders,
        )

        blockers: list[str] = []
        if window.state != "eligible":
            blockers.append(window.state)
        if narrative is None:
            blockers.append("missing_narrative")
        if account.account_blocked or account.trading_blocked or account.trade_suspended_by_user:
            blockers.append("account_blocked")
        if positions:
            blockers.append("position_open")
        if orders:
            blockers.append("working_order")
        if cooldown_ends_at is not None and evaluated_at < cooldown_ends_at:
            blockers.append("post_close_cooldown")
        if market.latest_price is None:
            blockers.append("missing_market_data")
        elif market.trade_freshness_seconds is None or market.trade_freshness_seconds > 10:
            blockers.append("stale_market_data")
        if clock.is_open and not market.five_minute_bars_current:
            blockers.append("stale_five_minute_bars")
        if account.daily_loss_headroom <= 0:
            blockers.append("daily_loss_limit")

        return TickContext(
            evaluated_at=evaluated_at,
            trading_date=trading_date,
            clock=clock,
            entry_window=window,
            entry_blockers=blockers,
            exit_reasons=exit_reasons,
            cancel_order_ids=cancel_order_ids,
            last_position_closed_at=last_position_closed_at,
            cooldown_ends_at=cooldown_ends_at,
            account=account,
            positions=positions,
            working_orders=orders,
            trade_plan=trade_plan,
            narrative=narrative,
            market=market,
        )
