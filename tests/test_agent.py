import asyncio
import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from market_agent.agent import AgentEvaluator
from market_agent.alpaca import AlpacaClient
from market_agent.config import Settings
from market_agent.mcp import AlpacaMcpClient
from market_agent.models import (
    AccountState,
    AgentDecision,
    EntryWindow,
    LiveMarketState,
    MarketClockState,
    TickContext,
)


class FakeMcpResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return self.payload


class FakeMcpSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> FakeMcpResult:
        self.calls.append((name, arguments))
        symbol = "SPY260831P00771000"
        if name == "get_option_chain":
            return FakeMcpResult({"structuredContent": {"data": {"snapshots": {}}}})
        if name == "get_option_contract":
            return FakeMcpResult(
                {
                    "structuredContent": {
                        "data": {
                            "symbol": symbol,
                            "underlying_symbol": "SPY",
                            "status": "active",
                            "tradable": True,
                            "type": "put",
                            "expiration_date": "2026-08-31",
                        }
                    }
                }
            )
        if name == "get_option_snapshot":
            return FakeMcpResult(
                {
                    "structuredContent": {
                        "data": {
                            "snapshots": {
                                symbol: {
                                    "greeks": {"delta": -0.60},
                                    "latestQuote": {
                                        "bp": 2.47,
                                        "ap": 2.56,
                                        "t": datetime.now(UTC).isoformat(),
                                    },
                                }
                            }
                        }
                    }
                }
            )
        if name == "place_option_order":
            return FakeMcpResult({"structuredContent": {"id": "order-1"}})
        raise AssertionError(f"unexpected tool call: {name}")


def make_context() -> TickContext:
    now = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    return TickContext(
        evaluated_at=now,
        trading_date=date(2026, 8, 28),
        clock=MarketClockState(
            timestamp=now,
            is_open=True,
            next_open=datetime(2026, 8, 31, 13, 30, tzinfo=UTC),
            next_close=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
        ),
        entry_window=EntryWindow(
            state="eligible",
            entry_opens_at=datetime(2026, 8, 28, 13, 40, tzinfo=UTC),
            entry_closes_at=datetime(2026, 8, 28, 19, 45, tzinfo=UTC),
            session_closes_at=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
        ),
        entry_blockers=[],
        account=AccountState(
            status="ACTIVE",
            currency="USD",
            equity=100_000,
            session_starting_equity=100_000,
            daily_equity_pnl=0,
            daily_equity_pnl_percent=0,
            daily_loss_floor=90_000,
            daily_loss_headroom=10_000,
            buying_power=100_000,
            options_buying_power=100_000,
            cash=100_000,
            account_blocked=False,
            trading_blocked=False,
            trade_suspended_by_user=False,
        ),
        positions=[],
        working_orders=[],
        narrative=None,
        market=LiveMarketState(
            as_of=now,
            latest_trade=None,
            latest_quote=None,
            trade_freshness_seconds=None,
            regular_session_open=100,
            regular_session_high=101,
            regular_session_low=99,
            latest_price=100,
            premarket_high=None,
            premarket_low=None,
            initial_balance_high=None,
            initial_balance_low=None,
            initial_balance_complete=False,
            extension_up=None,
            extension_down=None,
            five_minute_atr14=None,
            latest_completed_bar_at=None,
            five_minute_bars_current=True,
            recent_completed_bars=[],
        ),
    )


def test_runtime_submits_only_the_validated_option_order() -> None:
    client = AlpacaMcpClient(Settings(order_submission_enabled=True), make_context())
    client._session = FakeMcpSession()  # type: ignore[assignment]
    order = {
        "symbol": "SPY260831P00771000",
        "side": "buy",
        "qty": "15",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "2.52",
        "position_intent": "buy_to_open",
    }

    _result, blocked = asyncio.run(client.call("place_option_order", order))
    assert blocked

    validation, _blocked = asyncio.run(
        client.call(
            "validate_option_order",
            {
                "action": "buy_put",
                "symbol": order["symbol"],
                "quantity": 15,
                "limit_price": 2.52,
            },
        )
    )
    assert validation["valid"] is True

    decision = AgentDecision(
        decision_id="2026-08-28-1000",
        evaluated_at=make_context().evaluated_at,
        action="buy_put",
        auction_state="discovery_down",
        confidence=0.8,
        thesis="SPY is accepting below prior value.",
        entry_price=100,
        invalidation_price=101,
        target_price=99,
        option_symbol=order["symbol"],
        quantity=15,
        limit_price=2.53,
    )
    with pytest.raises(ValueError, match="limit price"):
        asyncio.run(client.submit_validated_entry(decision))

    action = asyncio.run(
        client.submit_validated_entry(decision.model_copy(update={"limit_price": 2.52}))
    )
    assert action.result["structuredContent"]["id"] == "order-1"
    assert action.arguments["client_order_id"] == "augur-entry-2026-08-28-1000"

    with pytest.raises(ValueError, match="validate_option_order"):
        asyncio.run(
            client.submit_validated_entry(decision.model_copy(update={"limit_price": 2.52}))
        )


def test_option_data_defaults_to_spy_and_indicative_feed() -> None:
    client = AlpacaMcpClient(Settings(), make_context())
    session = FakeMcpSession()
    client._session = session  # type: ignore[assignment]
    client._required_arguments = {"get_option_chain": {"underlying_symbol"}}
    arguments: dict[str, Any] = {}

    _result, blocked = asyncio.run(client.call("get_option_chain", arguments))

    assert not blocked
    assert arguments == {"underlying_symbol": "SPY", "feed": "indicative"}

    snapshot = {"symbols": "SPY260831P00771000", "feed": "opra"}
    _result, blocked = asyncio.run(client.call("get_option_snapshot", snapshot))

    assert not blocked
    assert snapshot["feed"] == "indicative"
    assert session.calls == [
        ("get_option_chain", {"underlying_symbol": "SPY", "feed": "indicative"}),
        (
            "get_option_snapshot",
            {"symbols": "SPY260831P00771000", "feed": "indicative"},
        ),
    ]


def test_native_option_stop_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "symbol": "SPY260901C00650000",
            "qty": "15",
            "side": "sell",
            "type": "stop",
            "time_in_force": "day",
            "stop_price": "2.60",
            "position_intent": "sell_to_close",
            "client_order_id": "augur-stop-test",
        }
        return httpx.Response(200, json={"id": "stop-order"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AlpacaClient(
        Settings(alpaca_api_key="key", alpaca_secret_key="secret"),
        client=http,
    )
    result = asyncio.run(
        client.submit_option_stop(
            symbol="SPY260901C00650000",
            quantity=15,
            stop_price=2.6,
            client_order_id="augur-stop-test",
        )
    )

    assert result["id"] == "stop-order"
    asyncio.run(http.aclose())


def test_agent_extracts_fenced_json_after_analysis() -> None:
    parsed = AgentEvaluator._parse_json('Analysis first.\n```json\n{"action":"hold"}\n```')

    assert parsed == {"action": "hold"}


def test_agent_can_continue_past_eight_tool_rounds() -> None:
    model_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal model_calls
        model_calls += 1
        if model_calls <= 9:
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": f"call-{model_calls}",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": "{}"},
                    }
                ],
            }
        else:
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "action": "hold",
                        "auctionState": "unclear",
                        "confidence": 0,
                        "thesis": "No trade is justified.",
                        "holdReasons": ["insufficient_evidence"],
                    }
                ),
            }
        return httpx.Response(200, json={"choices": [{"message": message}]})

    class Tools:
        tools: list[dict[str, Any]] = []

        async def call(self, name: str, _arguments: dict[str, Any]) -> tuple[Any, bool]:
            assert name == "inspect"
            return {"ok": True}, False

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    evaluator = AgentEvaluator(
        Settings(featherless_api_key="test", agent_loop_timeout_seconds=2),
        client=http,
    )

    record = asyncio.run(evaluator.evaluate("2026-08-28-1000", make_context(), Tools()))

    assert record.decision.hold_reasons == ["insufficient_evidence"]
    assert len(record.tool_calls) == 9
    assert model_calls == 10
    asyncio.run(http.aclose())
