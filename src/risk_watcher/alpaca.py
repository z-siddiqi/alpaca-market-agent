from datetime import datetime
from typing import Any

import httpx

from risk_watcher.config import Settings
from risk_watcher.models import Account, Clock, Order, Position, Quote, Trade


class AlpacaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.Client(headers=settings.headers(), timeout=10)

    def close(self) -> None:
        self.client.close()

    def clock(self) -> Clock:
        return Clock.from_payload(self._get("/v2/clock"))

    def account(self) -> Account:
        return Account.from_payload(self._get("/v2/account"))

    def positions(self) -> list[Position]:
        payloads = self._get("/v2/positions")
        return [
            Position.from_payload(payload)
            for payload in payloads
            if payload.get("asset_class") == "us_option"
        ]

    def open_orders(self) -> list[Order]:
        payloads = self._get(
            "/v2/orders",
            params={"status": "open", "limit": 500, "nested": "true"},
        )
        return [Order.from_payload(payload) for payload in payloads]

    def latest_quote(self, symbol: str) -> Quote:
        response = self.client.get(
            f"{self.settings.alpaca_data_url}/v1beta1/options/quotes/latest",
            params={"symbols": symbol},
        )
        response.raise_for_status()
        payload = response.json().get("quotes", {}).get(symbol)
        if not payload:
            raise ValueError(f"Alpaca returned no quote for {symbol}")
        return Quote.from_payload(payload)

    def latest_stock_trade(self, symbol: str) -> Trade:
        response = self.client.get(
            f"{self.settings.alpaca_data_url}/v2/stocks/trades/latest",
            params={"symbols": symbol, "feed": "iex"},
        )
        response.raise_for_status()
        payload = response.json().get("trades", {}).get(symbol)
        if not payload:
            raise ValueError(f"Alpaca returned no trade for {symbol}")
        return Trade.from_payload(payload)

    def place_stop(
        self,
        position: Position,
        stop_price: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        return self._post(
            "/v2/orders",
            {
                "symbol": position.symbol,
                "qty": str(position.quantity),
                "side": "sell",
                "type": "stop",
                "time_in_force": "day",
                "stop_price": f"{stop_price:.2f}",
                "position_intent": "sell_to_close",
                "client_order_id": client_order_id,
            },
        )

    def replace_stop(self, order_id: str, stop_price: float) -> dict[str, Any]:
        return self._patch(
            f"/v2/orders/{order_id}",
            {"stop_price": f"{stop_price:.2f}"},
        )

    def place_profit_exit(
        self,
        position: Position,
        limit_price: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        return self._post(
            "/v2/orders",
            {
                "symbol": position.symbol,
                "qty": str(position.quantity),
                "side": "sell",
                "type": "limit",
                "time_in_force": "day",
                "limit_price": f"{limit_price:.2f}",
                "position_intent": "sell_to_close",
                "client_order_id": client_order_id,
            },
        )

    def replace_limit(self, order_id: str, limit_price: float) -> dict[str, Any]:
        return self._patch(
            f"/v2/orders/{order_id}",
            {"limit_price": f"{limit_price:.2f}"},
        )

    def cancel_order(self, order_id: str) -> None:
        response = self.client.delete(f"{self.settings.alpaca_trading_url}/v2/orders/{order_id}")
        if response.status_code != 404:
            response.raise_for_status()

    def close_position(self, symbol: str) -> dict[str, Any] | None:
        response = self.client.delete(f"{self.settings.alpaca_trading_url}/v2/positions/{symbol}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _get(self, path: str, params: dict[str, object] | None = None) -> Any:
        response = self.client.get(f"{self.settings.alpaca_trading_url}{path}", params=params)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        response = self.client.post(f"{self.settings.alpaca_trading_url}{path}", json=payload)
        response.raise_for_status()
        return response.json()

    def _patch(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        response = self.client.patch(f"{self.settings.alpaca_trading_url}{path}", json=payload)
        response.raise_for_status()
        return response.json()


def quote_is_fresh(quote: Quote, now: datetime, maximum_age_seconds: float = 5) -> bool:
    return quote.bid > 0 and 0 <= (now - quote.timestamp).total_seconds() <= maximum_age_seconds


def trade_is_fresh(trade: Trade, now: datetime, maximum_age_seconds: float = 5) -> bool:
    return trade.price > 0 and 0 <= (now - trade.timestamp).total_seconds() <= maximum_age_seconds
