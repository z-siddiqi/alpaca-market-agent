import json
import math
import os
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import ValidationError

from alpaca_market_agent.config import Settings
from alpaca_market_agent.models import (
    AgentDecision,
    OptionOrderProposal,
    OptionOrderValidation,
    OptionValidationCheck,
    TickContext,
    ToolCallRecord,
)
from alpaca_market_agent.policy import validate_option_order

VALIDATE_OPTION_ORDER = "validate_option_order"
VALIDATE_OPTION_ORDER_TOOL = {
    "type": "function",
    "function": {
        "name": VALIDATE_OPTION_ORDER,
        "description": (
            "Validate one proposed SPY option entry against fresh contract metadata, quote, "
            "Greeks, account state, and the competition policy. This read-only tool does not "
            "select a contract. A successful validation is required before place_option_order, "
            "and the submitted symbol, quantity, and limit price must match exactly."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["buy_call", "buy_put"]},
                "symbol": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
                "limit_price": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["action", "symbol", "quantity", "limit_price"],
        },
    },
}

READ_TOOLS = {
    "get_open_position",
    "get_option_chain",
    "get_option_contract",
    "get_option_contracts",
    "get_option_latest_quote",
    "get_option_snapshot",
    "get_order_by_client_id",
    "get_order_by_id",
}
WRITE_TOOLS = {
    "cancel_order_by_id",
    "close_position",
    "replace_order_by_id",
}
ALLOWED_TOOLS = READ_TOOLS | WRITE_TOOLS


class AlpacaMcpClient:
    def __init__(self, settings: Settings, context: TickContext | None = None) -> None:
        self.settings = settings
        self.context = context
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []
        self._validated_entry: OptionOrderProposal | None = None

    async def __aenter__(self) -> "AlpacaMcpClient":
        if self.settings.alpaca_api_key is None or self.settings.alpaca_secret_key is None:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")

        environment = {
            "PATH": os.environ.get("PATH", ""),
            "ALPACA_API_KEY": self.settings.alpaca_api_key.get_secret_value(),
            "ALPACA_SECRET_KEY": self.settings.alpaca_secret_key.get_secret_value(),
            "ALPACA_PAPER_TRADE": str(self.settings.alpaca_paper_trade).lower(),
            "ALPACA_TOOLSETS": "trading,assets,options-data",
        }
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(
            stdio_client(StdioServerParameters(command="alpaca-mcp-server", env=environment))
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        listed = await self._session.list_tools()
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            }
            for tool in listed.tools
            if tool.name in ALLOWED_TOOLS
        ]
        self._tools.append(VALIDATE_OPTION_ORDER_TOOL)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[Any, bool]:
        if name == VALIDATE_OPTION_ORDER:
            return await self._validate_option_order(arguments), False
        if name not in ALLOWED_TOOLS:
            return {"error": f"tool {name} is not available"}, True
        if name in WRITE_TOOLS and not self.settings.order_submission_enabled:
            return {
                "error": "order submission is disabled",
                "submissionEnabled": False,
            }, True
        return await self._call_raw(name, arguments), False

    async def submit_validated_entry(self, decision: AgentDecision) -> ToolCallRecord:
        if (
            decision.option_symbol is None
            or decision.quantity is None
            or decision.limit_price is None
        ):
            raise ValueError("entry decision is missing option order fields")
        arguments: dict[str, Any] = {
            "symbol": decision.option_symbol,
            "side": "buy",
            "qty": str(decision.quantity),
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(decision.limit_price),
            "position_intent": "buy_to_open",
            "client_order_id": f"augur-entry-{decision.decision_id}",
        }
        error = self._entry_authorization_error(arguments)
        if error is not None:
            raise ValueError(error)
        result = await self._call_raw("place_option_order", arguments)
        self._validated_entry = None
        return ToolCallRecord(
            name="place_option_order",
            arguments=arguments,
            result=result,
            blocked=False,
            called_at=datetime.now(UTC),
        )

    async def _call_raw(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("Alpaca MCP client is not connected")
        result = await self._session.call_tool(name, arguments)
        return result.model_dump(mode="json", by_alias=True)

    async def _validate_option_order(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._validated_entry = None
        try:
            proposal = OptionOrderProposal.model_validate(arguments)
        except ValidationError as error:
            return {
                "valid": False,
                "rejectionReasons": [f"invalid validation request: {error}"],
            }
        if self.context is None:
            return self._validation_failure(proposal, "tick context is unavailable")

        contract_result = await self._call_raw(
            "get_option_contract",
            {"symbol_or_id": proposal.symbol},
        )
        contract = _find_contract(contract_result, proposal.symbol)
        if contract is None:
            return self._validation_failure(
                proposal,
                "Alpaca did not return contract metadata for the proposed symbol",
            )

        snapshot_result = await self._call_raw(
            "get_option_snapshot",
            {"symbols": proposal.symbol},
        )
        checked_at = datetime.now(UTC)
        snapshot = _find_snapshot(snapshot_result, proposal.symbol)
        if snapshot is None:
            return self._validation_failure(
                proposal,
                "Alpaca did not return a snapshot for the proposed symbol",
            )

        validation = validate_option_order(
            proposal=proposal,
            context=self.context,
            contract=contract,
            snapshot=snapshot,
            checked_at=checked_at,
        )
        if validation.valid:
            self._validated_entry = proposal
        return validation.model_dump(mode="json", by_alias=True)

    def _validation_failure(
        self,
        proposal: OptionOrderProposal,
        reason: str,
    ) -> dict[str, Any]:
        if self.context is None:
            options_buying_power = 0
            daily_loss_headroom = 0
        else:
            options_buying_power = self.context.account.options_buying_power
            daily_loss_headroom = self.context.account.daily_loss_headroom
        return OptionOrderValidation(
            **proposal.model_dump(),
            valid=False,
            checks=[OptionValidationCheck(name="alpaca_data", passed=False, detail=reason)],
            rejection_reasons=[reason],
            options_buying_power=options_buying_power,
            daily_loss_headroom=daily_loss_headroom,
        ).model_dump(mode="json", by_alias=True)

    def _entry_authorization_error(self, arguments: dict[str, Any]) -> str | None:
        proposal = self._validated_entry
        if proposal is None:
            return "call validate_option_order successfully before placing a new option entry"
        if arguments.get("legs") is not None or arguments.get("order_class") == "mleg":
            return "validated entries must be single-leg option orders"
        if arguments.get("symbol") != proposal.symbol:
            return "order symbol does not match the validated option proposal"
        if arguments.get("side") != "buy":
            return "validated long-option entries must use side=buy"
        if arguments.get("type") != "limit":
            return "validated option entries must use type=limit"
        if arguments.get("time_in_force", "day") != "day":
            return "validated option entries must use time_in_force=day"
        if arguments.get("position_intent") not in (None, "buy_to_open"):
            return "validated option entries must use position_intent=buy_to_open"
        try:
            quantity = float(arguments.get("qty"))
        except (TypeError, ValueError):
            return "order quantity is invalid"
        if not quantity.is_integer() or int(quantity) != proposal.quantity:
            return "order quantity does not match the validated option proposal"
        try:
            limit_price = float(arguments.get("limit_price"))
        except (TypeError, ValueError):
            return "order limit price is invalid"
        if not math.isclose(limit_price, proposal.limit_price, rel_tol=0, abs_tol=1e-9):
            return "order limit price does not match the validated option proposal"
        return None


def _find_contract(result: Any, symbol: str) -> dict[str, Any] | None:
    for value in _walk_payloads(result):
        if value.get("symbol") == symbol and (
            "underlying_symbol" in value or "expiration_date" in value
        ):
            return value
    return None


def _find_snapshot(result: Any, symbol: str) -> dict[str, Any] | None:
    for value in _walk_payloads(result):
        snapshots = value.get("snapshots")
        if isinstance(snapshots, dict) and isinstance(snapshots.get(symbol), dict):
            return snapshots[symbol]
        if "greeks" in value and "latestQuote" in value:
            return value
    return None


def _walk_payloads(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_payloads(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_payloads(item)
    elif isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(value.lstrip())
        except json.JSONDecodeError:
            return
        yield from _walk_payloads(parsed)
