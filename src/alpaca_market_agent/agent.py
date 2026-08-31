import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from alpaca_market_agent.config import Settings
from alpaca_market_agent.models import (
    AgentDecision,
    AgentDecisionDraft,
    DecisionRecord,
    OptionEvidence,
    TickContext,
    ToolCallRecord,
)
from alpaca_market_agent.policy import validate_option_candidate

SYSTEM_PROMPT = """You are an autonomous SPY options agent operating a dedicated Alpaca
paper account. Treat trade as a two-sided auction. Inside established value normally
requires patience. Acceptance outside value may indicate discovery; rejection back into
value invalidates continuation. Prefer a clear structural location, nearby invalidation,
and reachable reference. Hold when evidence is incomplete or contradictory.

Classify the auction from completed-bar evidence, then form a falsifiable SPY thesis. Only
inspect the option chain after a directional thesis exists. An entry requires at least 0.50
confidence, at least 1R underlying reward:risk, no context entry blockers, one position
maximum, and a 1-5 DTE long call or put with absolute delta 0.55-0.65. The quote must be
fresh and two-sided, with width at most $0.15 and 5% of midpoint. Entries are limit-only.
Premium allocation is at most 5% of session-starting equity; planned loss at the 35%
premium circuit breaker is at most 2%; quantity must also fit daily loss headroom and
options buying power. Never invent option data.

Alpaca MCP tools are raw paper-account tools. Use reads narrowly. Account-changing calls
are your responsibility, but a blocked tool result means no order occurred. If submission
is disabled, you may inspect a candidate but the final action must be hold and include
order_submission_disabled in holdReasons.

The supplied entryWindow.state and entryBlockers are authoritative policy facts. Do not
recalculate the session window from timestamp strings or invent a blocker that is absent.
The preloaded account, positions, and workingOrders are fresh and authoritative for this
turn; do not repeat those reads through MCP. Leave option fields null unless you inspected
a current snapshot and found a candidate that satisfies every contract rule.

When entryWindow.state is closing_only, do not open a position. Cancel any working entry,
close any open position through Alpaca MCP, and verify the account is flat. Do not leave a
position or working order for the next session.

Return only a JSON object matching the supplied decision schema. Record concise evidence,
policy checks, and hold reasons. Prices for entry, invalidation, and target are SPY prices;
limitPrice is option premium."""


class ToolClient(Protocol):
    @property
    def tools(self) -> list[dict[str, Any]]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[Any, bool]: ...


class AgentEvaluator:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=120)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def evaluate(
        self,
        tick_id: str,
        context: TickContext,
        tools: ToolClient,
    ) -> DecisionRecord:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._prompt(context)},
        ]
        tool_calls: list[ToolCallRecord] = []
        validation_error: str | None = None

        for _attempt in range(self.settings.max_agent_tool_calls + 2):
            if validation_error is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your decision failed validation. Return the full corrected JSON "
                            f"object only. Error: {validation_error}"
                        ),
                    }
                )
                validation_error = None

            message = await self._call_model(messages, tools.tools)
            requested = message.get("tool_calls") or []
            if requested:
                if len(tool_calls) + len(requested) > self.settings.max_agent_tool_calls:
                    raise ValueError("agent exceeded the tool-call limit")
                messages.append(message)
                for call in requested:
                    function = call.get("function", {})
                    name = str(function.get("name", ""))
                    arguments = self._arguments(function.get("arguments", "{}"))
                    result, blocked = await tools.call(name, arguments)
                    tool_calls.append(
                        ToolCallRecord(
                            name=name,
                            arguments=arguments,
                            result=result,
                            blocked=blocked,
                            called_at=datetime.now(UTC),
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result, separators=(",", ":")),
                        }
                    )
                continue

            try:
                draft = AgentDecisionDraft.model_validate(self._parse_json(message.get("content")))
                option_evidence = self._validate_decision(draft, context, tool_calls)
            except (ValidationError, ValueError, json.JSONDecodeError) as error:
                validation_error = str(error)
                continue

            evaluated_at = context.evaluated_at
            decision = AgentDecision(
                **draft.model_dump(),
                decision_id=tick_id,
                evaluated_at=evaluated_at,
                option_evidence=option_evidence,
            )
            return DecisionRecord(
                tick_id=tick_id,
                trading_date=context.trading_date,
                model=self.settings.agent_model,
                context=context,
                decision=decision,
                tool_calls=tool_calls,
                created_at=datetime.now(UTC),
            )

        raise ValueError("agent did not return a valid decision")

    async def _call_model(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        response: httpx.Response | None = None
        for attempt in range(2):
            response = await self.client.post(
                self.settings.model_url(),
                headers=self.settings.model_headers(),
                json={
                    "model": self.settings.agent_model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.1,
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                retryable = error.response.status_code == 429 or error.response.status_code >= 500
                if attempt == 0 and retryable:
                    retry_after = error.response.headers.get("retry-after", "")
                    try:
                        delay = min(30, max(1, float(retry_after)))
                    except ValueError:
                        delay = 5 if error.response.status_code == 429 else 1
                    await asyncio.sleep(delay)
                    continue
                raise
            break
        if response is None:
            raise ValueError("model provider returned no response")
        payload = response.json()
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("model provider returned no agent message") from error
        if not isinstance(message, dict):
            raise ValueError("model provider returned an invalid agent message")
        return message

    def _prompt(self, context: TickContext) -> str:
        schema = AgentDecisionDraft.model_json_schema(by_alias=True)
        payload = {
            "orderSubmissionEnabled": self.settings.order_submission_enabled,
            "context": context.model_dump(mode="json", by_alias=True),
            "decisionSchema": schema,
        }
        return json.dumps(payload, separators=(",", ":"))

    @staticmethod
    def _arguments(value: Any) -> dict[str, Any]:
        parsed = json.loads(value) if isinstance(value, str) else value
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must be a JSON object")
        return parsed

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        if not isinstance(content, str):
            raise ValueError("agent response must contain JSON text")
        stripped = content.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0]
            if stripped.lstrip().startswith("json"):
                stripped = stripped.lstrip()[4:].lstrip()
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError("agent response must be a JSON object")
        return parsed

    def _validate_decision(
        self,
        decision: AgentDecisionDraft,
        context: TickContext,
        tool_calls: list[ToolCallRecord],
    ) -> OptionEvidence | None:
        entry = decision.action in {"buy_call", "buy_put"}
        decision_text = json.dumps(decision.model_dump(mode="json")).lower()
        invalid_window_claims = (
            "entry_window_closed",
            "entry window closed",
            "now closed",
            "after entry closes",
        )
        if context.entry_window.state == "eligible" and any(
            claim in decision_text for claim in invalid_window_claims
        ):
            raise ValueError("decision contradicts the authoritative eligible entry window")
        if entry and context.entry_blockers:
            raise ValueError("an entry action is invalid while context has entry blockers")
        if entry and not self.settings.order_submission_enabled:
            raise ValueError("an entry action is invalid while order submission is disabled")
        if not self.settings.order_submission_enabled and decision.action != "hold":
            raise ValueError("only hold is valid while order submission is disabled")
        if entry:
            required = {
                "entryPrice": decision.entry_price,
                "invalidationPrice": decision.invalidation_price,
                "targetPrice": decision.target_price,
                "optionSymbol": decision.option_symbol,
                "quantity": decision.quantity,
                "limitPrice": decision.limit_price,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(f"entry decision is missing: {', '.join(missing)}")
        option_fields = (decision.option_symbol, decision.quantity, decision.limit_price)
        if any(value is not None for value in option_fields):
            if any(value is None for value in option_fields):
                raise ValueError("option symbol, quantity, and limit price must be set together")
            return validate_option_candidate(
                symbol=decision.option_symbol,
                quantity=decision.quantity,
                limit_price=decision.limit_price,
                context=context,
                tool_calls=tool_calls,
            )
        return None
