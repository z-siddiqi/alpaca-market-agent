import hashlib
import logging
import os
import signal
import time
from datetime import UTC, datetime, timedelta

import httpx

from risk_watcher.alpaca import AlpacaClient, quote_is_fresh, trade_is_fresh
from risk_watcher.config import Settings
from risk_watcher.models import Order, Position, TradePlan
from risk_watcher.rules import (
    breakeven_trigger_price,
    loss_stop_price,
    profit_target_price,
    spy_target_reached,
)
from risk_watcher.store import Store

DAILY_LOSS_FRACTION = 0.10
CLOSING_BUFFER = timedelta(minutes=15)
LOG = logging.getLogger("risk_watcher")


class PositionWatcher:
    def __init__(self, settings: Settings) -> None:
        owner = os.environ.get("CLOUD_RUN_EXECUTION", f"local-{os.getpid()}")
        self.settings = settings
        self.alpaca = AlpacaClient(settings)
        self.store = Store(settings.gcp_project_id, owner, settings.lease_seconds)
        self.running = True
        self.plans: dict[str, TradePlan] = {}
        self.triggered_exits: dict[str, str] = {}
        self.last_account_refresh = 0.0
        self.last_clock_refresh = 0.0
        self.last_heartbeat = 0.0
        self.account = None
        self.clock = None

    def stop(self, *_args: object) -> None:
        self.running = False

    def run(self) -> None:
        try:
            self.clock = self.alpaca.clock()
            if not self.clock.is_open:
                LOG.info("market is closed; watcher exiting")
                return
            trading_date = self.clock.timestamp.date().isoformat()
            if not self.store.acquire_lease(trading_date):
                LOG.info("another position watcher owns the session lease")
                return
            LOG.info("position watcher started for %s", trading_date)
            while self.running:
                started_at = time.monotonic()
                try:
                    self._cycle()
                except (httpx.HTTPError, ValueError) as error:
                    LOG.warning("watcher cycle failed: %s", error)
                elapsed = time.monotonic() - started_at
                time.sleep(max(0, self.settings.poll_seconds - elapsed))
        finally:
            self.alpaca.close()

    def _cycle(self) -> None:
        monotonic_now = time.monotonic()
        if monotonic_now - self.last_clock_refresh >= self.settings.clock_refresh_seconds:
            self.clock = self.alpaca.clock()
            self.last_clock_refresh = monotonic_now
        if self.clock is None or not self.clock.is_open:
            self.running = False
            return
        if monotonic_now - self.last_account_refresh >= self.settings.account_refresh_seconds:
            self.account = self.alpaca.account()
            self.last_account_refresh = monotonic_now
        if monotonic_now - self.last_heartbeat >= self.settings.lease_seconds / 3:
            if not self.store.heartbeat():
                LOG.error("position watcher lost its session lease")
                self.running = False
                return
            self.last_heartbeat = monotonic_now

        positions = self.alpaca.positions()
        orders = self.alpaca.open_orders()
        forced_exit = (
            self.clock.timestamp >= self.clock.next_close - CLOSING_BUFFER
            or (
                self.account is not None
                and self.account.daily_loss_fraction >= DAILY_LOSS_FRACTION
            )
        )
        if forced_exit:
            self._flatten(positions, orders)
            return

        for position in positions:
            position_orders = [order for order in orders if order.symbol == position.symbol]
            self._manage(position, position_orders)

    def _manage(self, position: Position, orders: list[Order]) -> None:
        plan = self.plans.get(position.symbol)
        if plan is None:
            plan = self.store.trade_plan(position.symbol)
            if plan is None:
                LOG.warning("no durable trade plan exists for %s", position.symbol)
                return
            self.plans[position.symbol] = plan

        quote = self.alpaca.latest_quote(position.symbol)
        spy_trade = self.alpaca.latest_stock_trade("SPY")
        now = datetime.now(UTC)
        if not quote_is_fresh(quote, now):
            raise ValueError(f"stale option quote for {position.symbol}")
        if not trade_is_fresh(spy_trade, now):
            raise ValueError("stale SPY trade")

        protective_stops = [order for order in orders if order.protective_stop]
        working_exits = [
            order for order in orders if order.side == "sell" and not order.protective_stop
        ]
        if working_exits:
            self._maintain_exit(working_exits[0], quote.bid)
            return

        target_reached = spy_target_reached(spy_trade.price, plan)
        if target_reached and position.symbol not in self.triggered_exits:
            self.triggered_exits[position.symbol] = "spy_target"
            LOG.info(
                "triggered SPY target for %s at %.2f",
                position.symbol,
                spy_trade.price,
            )
        if quote.bid >= profit_target_price(position, plan):
            self.triggered_exits.setdefault(position.symbol, "premium_target")
        if position.symbol in self.triggered_exits:
            self._start_exit(
                position,
                protective_stops,
                quote.bid,
                plan,
                self.triggered_exits[position.symbol],
            )
            return

        desired_stop = loss_stop_price(position, plan)
        if quote.bid >= breakeven_trigger_price(position, plan):
            desired_stop = max(desired_stop, position.average_entry_price)
        if protective_stops:
            existing = protective_stops[0]
            desired_stop = max(desired_stop, existing.stop_price or 0)
            if existing.stop_price != desired_stop:
                self.alpaca.replace_stop(existing.order_id, desired_stop)
                LOG.info("raised %s stop to %.2f", position.symbol, desired_stop)
            return

        client_order_id = self._client_order_id("stop", plan.decision_id)
        self.alpaca.place_stop(position, desired_stop, client_order_id)
        LOG.info("placed %s stop at %.2f", position.symbol, desired_stop)

    def _start_exit(
        self,
        position: Position,
        protective_stops: list[Order],
        bid: float,
        plan: TradePlan,
        reason: str,
    ) -> None:
        for order in protective_stops:
            self.alpaca.cancel_order(order.order_id)
        if protective_stops:
            return
        client_order_id = self._client_order_id(reason, plan.decision_id)
        self.alpaca.place_profit_exit(position, bid, client_order_id)
        LOG.info("submitted %s %s exit at %.2f", position.symbol, reason, bid)

    def _maintain_exit(self, order: Order, bid: float) -> None:
        if bid <= 0 or order.limit_price == bid:
            return
        self.alpaca.replace_limit(order.order_id, bid)
        LOG.info("repriced %s exit to %.2f", order.symbol, bid)

    def _flatten(self, positions: list[Position], orders: list[Order]) -> None:
        for order in orders:
            if order.side == "buy" or order.protective_stop:
                self.alpaca.cancel_order(order.order_id)
        active_exits = {
            order.symbol for order in orders if order.side == "sell" and not order.protective_stop
        }
        for position in positions:
            if position.symbol not in active_exits:
                self.alpaca.close_position(position.symbol)
                LOG.info("requested mandatory close for %s", position.symbol)

    @staticmethod
    def _client_order_id(kind: str, decision_id: str) -> str:
        digest = hashlib.sha256(f"{kind}:{decision_id}".encode()).hexdigest()[:20]
        return f"augur-{kind}-{digest}"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    watcher = PositionWatcher(Settings.from_environment())
    signal.signal(signal.SIGTERM, watcher.stop)
    signal.signal(signal.SIGINT, watcher.stop)
    watcher.run()


if __name__ == "__main__":
    main()
