import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from market_agent.agent.prompt import SYSTEM_PROMPT
from market_agent.config import Settings
from market_agent.models import (
    AgentDecision,
    AgentDecisionDraft,
    DecisionRecord,
    OptionEvidence,
    TickContext,
    ToolCallRecord,
)
from market_agent.policy import validated_option_evidence


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

        try:
            async with asyncio.timeout(self.settings.agent_loop_timeout_seconds):
                while True:
                    if validation_error is not None:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your decision failed validation. Return the full corrected "
                                    f"JSON object only. Error: {validation_error}"
                                ),
                            }
                        )
                        validation_error = None

                    message = await self._call_model(messages, tools.tools)
                    requested = message.get("tool_calls") or []
                    if requested:
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
                        draft = AgentDecisionDraft.model_validate(
                            self._parse_json(message.get("content"))
                        )
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
        except TimeoutError:
            return self._fallback_hold(
                tick_id,
                context,
                tool_calls,
                "agent_deadline",
            )

    def _fallback_hold(
        self,
        tick_id: str,
        context: TickContext,
        tool_calls: list[ToolCallRecord],
        reason: str,
    ) -> DecisionRecord:
        evaluated_at = context.evaluated_at
        return DecisionRecord(
            tick_id=tick_id,
            trading_date=context.trading_date,
            model=self.settings.agent_model,
            context=context,
            decision=AgentDecision(
                decision_id=tick_id,
                evaluated_at=evaluated_at,
                action="hold",
                auction_state="unclear",
                confidence=0,
                thesis="The model did not complete a valid decision.",
                policy_checks=[],
                hold_reasons=[reason],
            ),
            tool_calls=tool_calls,
            created_at=datetime.now(UTC),
        )

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
        if "```" in stripped:
            blocks = stripped.split("```")
            for block in blocks[1::2]:
                candidate = block.strip()
                if candidate.startswith("json"):
                    candidate = candidate[4:].lstrip()
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
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
            entry_price = float(decision.entry_price)
            invalidation_price = float(decision.invalidation_price)
            target_price = float(decision.target_price)
            if decision.action == "buy_call":
                risk = entry_price - invalidation_price
                reward = target_price - entry_price
            else:
                risk = invalidation_price - entry_price
                reward = entry_price - target_price
            if risk <= 0 or reward <= 0:
                raise ValueError(
                    "entry, invalidation, and target do not describe the selected direction"
                )
            if reward < risk:
                raise ValueError("SPY target must provide at least 1R reward:risk")
        if context.exit_reasons and decision.action != "close_position":
            raise ValueError("authoritative exit reasons require close_position")
        if decision.action == "close_position":
            protective_stops = {
                order.order_id
                for order in context.working_orders
                if order.side == "sell" and order.order_type in {"stop", "stop_limit"}
            }
            working_sells = {
                order.symbol
                for order in context.working_orders
                if order.side == "sell" and order.order_id not in protective_stops
            }
            required_closes = {
                position.symbol
                for position in context.positions
                if position.symbol not in working_sells
            }
            called_closes = {
                str(call.arguments.get("symbol_or_asset_id", ""))
                for call in tool_calls
                if call.name == "close_position" and not call.blocked
            }
            missing_closes = required_closes - called_closes
            if missing_closes:
                raise ValueError(
                    "close_position was not called for: " + ", ".join(sorted(missing_closes))
                )
        required_cancels = set(context.cancel_order_ids)
        if decision.action == "close_position":
            required_cancels.update(
                order.order_id
                for order in context.working_orders
                if order.side == "sell" and order.order_type in {"stop", "stop_limit"}
            )
        if required_cancels:
            cancelled_orders = {
                str(call.arguments.get("order_id", ""))
                for call in tool_calls
                if call.name == "cancel_order_by_id" and not call.blocked
            }
            missing_cancels = required_cancels - cancelled_orders
            if missing_cancels:
                raise ValueError(
                    "cancel_order_by_id was not called for: " + ", ".join(sorted(missing_cancels))
                )
        option_fields = (decision.option_symbol, decision.quantity, decision.limit_price)
        if any(value is not None for value in option_fields):
            if any(value is None for value in option_fields):
                raise ValueError("option symbol, quantity, and limit price must be set together")
            return validated_option_evidence(
                action=decision.action,
                symbol=decision.option_symbol,
                quantity=decision.quantity,
                limit_price=decision.limit_price,
                tool_calls=tool_calls,
            )
        return None
