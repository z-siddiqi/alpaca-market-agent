from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from market_agent.config import Settings
from market_agent.models import Bar

ET = ZoneInfo("America/New_York")


class AlpacaClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def previous_session(self, plan_date: date) -> tuple[date, time]:
        start = plan_date - timedelta(days=14)
        response = await self.client.get(
            f"{self.settings.alpaca_trading_url}/v2/calendar",
            headers=self.settings.alpaca_headers(),
            params={"start": start.isoformat(), "end": plan_date.isoformat()},
        )
        response.raise_for_status()
        sessions = [
            item for item in response.json() if date.fromisoformat(item["date"]) < plan_date
        ]
        if not sessions:
            raise ValueError(f"no prior trading session found before {plan_date}")
        session = sessions[-1]
        return date.fromisoformat(session["date"]), time.fromisoformat(session["close"])

    async def stock_bars(
        self,
        *,
        start: datetime,
        end: datetime,
        feed: str,
        timeframe: str = "1Min",
    ) -> list[Bar]:
        params: dict[str, Any] = {
            "symbols": "SPY",
            "timeframe": timeframe,
            "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "feed": feed,
            "limit": 10_000,
            "sort": "asc",
        }
        bars: list[Bar] = []
        while True:
            response = await self.client.get(
                f"{self.settings.alpaca_data_url}/v2/stocks/bars",
                headers=self.settings.alpaca_headers(),
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
            bars.extend(self._parse_bar(item) for item in payload.get("bars", {}).get("SPY", []))
            page_token = payload.get("next_page_token")
            if not page_token:
                return bars
            params["page_token"] = page_token

    async def trading_clock(self) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.settings.alpaca_trading_url}/v2/clock",
            headers=self.settings.alpaca_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def account(self) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.settings.alpaca_trading_url}/v2/account",
            headers=self.settings.alpaca_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def positions(self) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"{self.settings.alpaca_trading_url}/v2/positions",
            headers=self.settings.alpaca_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def open_orders(self) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"{self.settings.alpaca_trading_url}/v2/orders",
            headers=self.settings.alpaca_headers(),
            params={"status": "open", "limit": 500, "nested": "true"},
        )
        response.raise_for_status()
        return response.json()

    async def closed_orders(self, *, after: datetime) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"{self.settings.alpaca_trading_url}/v2/orders",
            headers=self.settings.alpaca_headers(),
            params={
                "status": "closed",
                "after": after.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "direction": "desc",
                "limit": 500,
                "nested": "true",
            },
        )
        response.raise_for_status()
        return response.json()

    async def cancel_order(self, order_id: str) -> None:
        response = await self.client.delete(
            f"{self.settings.alpaca_trading_url}/v2/orders/{order_id}",
            headers=self.settings.alpaca_headers(),
        )
        if response.status_code != 404:
            response.raise_for_status()

    async def close_position(self, symbol: str) -> dict[str, Any] | None:
        response = await self.client.delete(
            f"{self.settings.alpaca_trading_url}/v2/positions/{symbol}",
            headers=self.settings.alpaca_headers(),
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def submit_option_stop(
        self,
        *,
        symbol: str,
        quantity: int,
        stop_price: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.settings.alpaca_trading_url}/v2/orders",
            headers=self.settings.alpaca_headers(),
            json={
                "symbol": symbol,
                "qty": str(quantity),
                "side": "sell",
                "type": "stop",
                "time_in_force": "day",
                "stop_price": f"{stop_price:.2f}",
                "position_intent": "sell_to_close",
                "client_order_id": client_order_id,
            },
        )
        response.raise_for_status()
        return response.json()

    async def stock_snapshot(self) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.settings.alpaca_data_url}/v2/stocks/snapshots",
            headers=self.settings.alpaca_headers(),
            params={"symbols": "SPY", "feed": "iex"},
        )
        response.raise_for_status()
        return response.json().get("SPY", {})

    async def narrative_bars(
        self,
        plan_date: date,
    ) -> tuple[date, int, list[Bar], list[Bar]]:
        source_date, source_close = await self.previous_session(plan_date)
        source_open_at = datetime.combine(source_date, time(9, 30), ET)
        source_close_at = datetime.combine(source_date, source_close, ET)
        expected_bars = int((source_close_at - source_open_at).total_seconds() // 60)
        prior_bars = await self.stock_bars(start=source_open_at, end=source_close_at, feed="sip")
        prior_bars = [
            bar for bar in prior_bars if source_open_at <= bar.timestamp < source_close_at
        ]

        opening_start = datetime.combine(plan_date, time(9, 30), ET)
        opening_end = datetime.combine(plan_date, time(9, 35), ET)
        opening_bars = await self.stock_bars(start=opening_start, end=opening_end, feed="iex")
        opening_bars = [bar for bar in opening_bars if opening_start <= bar.timestamp < opening_end]
        return source_date, expected_bars, prior_bars, opening_bars

    @staticmethod
    def _parse_bar(item: dict[str, Any]) -> Bar:
        return Bar(
            timestamp=datetime.fromisoformat(item["t"].replace("Z", "+00:00")),
            open=item["o"],
            high=item["h"],
            low=item["l"],
            close=item["c"],
            volume=item.get("v", 0),
        )
