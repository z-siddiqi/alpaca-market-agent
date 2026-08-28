import asyncio
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from alpaca_market_agent.agent import AgentEvaluator
from alpaca_market_agent.config import Settings
from alpaca_market_agent.mcp import AlpacaMcpClient
from alpaca_market_agent.models import (
    AccountState,
    EntryWindow,
    LiveMarketState,
    MarketClockState,
    TickContext,
    ToolCallRecord,
)
from alpaca_market_agent.policy import validate_option_candidate


class FakeResponse:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": self.message}]}


class FakeModelClient:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    async def post(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
        return FakeResponse(self.messages.pop(0))


class FakeTools:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_option_chain",
                "description": "Get a chain",
                "parameters": {"type": "object"},
            },
        }
    ]

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[Any, bool]:
        return {"name": name, "arguments": arguments, "contracts": []}, False


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
            daily_loss_floor=95_000,
            daily_loss_headroom=5_000,
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


def test_agent_records_tool_call_and_hold() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "get_option_chain",
                        "arguments": json.dumps({"underlying_symbol": "SPY"}),
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "action": "hold",
                    "auctionState": "unclear",
                    "confidence": 0.3,
                    "thesis": "No confirmed directional auction.",
                    "activeReferences": [],
                    "evidence": ["No completed-bar confirmation."],
                    "policyChecks": ["paper account"],
                    "holdReasons": ["order_submission_disabled"],
                }
            ),
        },
    ]
    client = FakeModelClient(messages)
    settings = Settings(
        cloudflare_account_id="account",
        ai_gateway_token="token",
        order_submission_enabled=False,
    )
    evaluator = AgentEvaluator(settings, client=client)  # type: ignore[arg-type]

    record = asyncio.run(evaluator.evaluate("2026-08-28-1000", make_context(), FakeTools()))

    assert record.decision.action == "hold"
    assert record.tool_calls[0].name == "get_option_chain"
    assert not record.tool_calls[0].blocked


def test_mcp_write_is_blocked_when_submission_is_disabled() -> None:
    client = AlpacaMcpClient(Settings(order_submission_enabled=False))

    result, blocked = asyncio.run(
        client.call("place_option_order", {"symbol": "SPY260831C00700000", "qty": 1})
    )

    assert blocked
    assert result["submissionEnabled"] is False


def test_option_candidate_rejects_delta_outside_band() -> None:
    checked_at = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    snapshot_call = ToolCallRecord(
        name="get_option_snapshot",
        arguments={"symbol_or_symbols": "SPY260831P00771000"},
        result={
            "structuredContent": {
                "data": {
                    "snapshots": {
                        "SPY260831P00771000": {
                            "greeks": {"delta": -0.5268},
                            "latestQuote": {
                                "bp": 2.47,
                                "ap": 2.56,
                                "t": checked_at.isoformat(),
                            },
                        }
                    }
                }
            }
        },
        called_at=checked_at,
    )

    with pytest.raises(ValueError, match="outside the 0.55-0.65 band"):
        validate_option_candidate(
            symbol="SPY260831P00771000",
            quantity=19,
            limit_price=2.52,
            context=make_context(),
            tool_calls=[snapshot_call],
        )
