import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from market_agent.config import Settings
from market_agent.models import (
    AboveStructureLevels,
    BalancedLevels,
    BelowStructureLevels,
    NarrativeDraft,
    NarrativeRecord,
    OpeningContext,
    SessionPerception,
)

SYSTEM_PROMPT = """You are an Auction Market Theory analyst writing a compact SPY daily plan
before trading begins.

Write like a calm Market Profile practitioner: observational, specific, and conditional.
Do not issue buy or sell commands. Use only facts and prices in the supplied
prior-session perception and opening-gap context.

Before writing any prose, call validate_levels with your chosen level map. If validation
fails, read the error and call the tool again with a corrected map. Only after validation
passes, write Markdown containing these sections in order:

## Contextual Analysis & Plan

Two or three short paragraphs. Explain how the prior RTH auction developed using
its period OHLC, initial balance, extensions, value, and closing location. Then
explain how today's opening gap changes the relevant context. End with the selected
level map in plain language.

## Levels of Interest

A one-line introduction followed by two concise bullets describing the validated map.

Choose one of these maps:
- balanced: kind, pivot, upsideTargets, downsideTargets
- above_structure: kind, upsideTrigger, upsideTargets, supportRepair
- below_structure: kind, downsideTrigger, downsideTargets, resistanceRepair

Every level must be copied exactly from allowed_references. Do not calculate, round, or
invent levels. Targets and repair ladders must be nearest first. For a true gap beyond
prior structure, continuation targets may be empty; do not fabricate one."""


VALIDATE_LEVELS_TOOL = {
    "type": "function",
    "function": {
        "name": "validate_levels",
        "description": "Validate the chosen regime-aware level map before writing the plan.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["balanced", "above_structure", "below_structure"],
                },
                "pivot": {"type": "number"},
                "upsideTrigger": {"type": "number"},
                "downsideTrigger": {"type": "number"},
                "upsideTargets": {"type": "array", "items": {"type": "number"}},
                "downsideTargets": {"type": "array", "items": {"type": "number"}},
                "supportRepair": {"type": "array", "items": {"type": "number"}},
                "resistanceRepair": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["kind"],
        },
    },
}

LEVELS_ADAPTER = TypeAdapter(AboveStructureLevels | BalancedLevels | BelowStructureLevels)


class NarrativeGenerator:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=90)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def generate(
        self,
        perception: SessionPerception,
        opening: OpeningContext,
    ) -> NarrativeRecord:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._build_prompt(perception, opening)},
        ]
        levels: AboveStructureLevels | BalancedLevels | BelowStructureLevels | None = None

        for _attempt in range(3):
            message = await self._call_model(messages, tools=[VALIDATE_LEVELS_TOOL])
            tool_calls = message.get("tool_calls") or []
            if len(tool_calls) != 1:
                messages.extend(
                    [
                        message,
                        {
                            "role": "user",
                            "content": "Call validate_levels exactly once before writing prose.",
                        },
                    ]
                )
                continue

            call = tool_calls[0]
            function = call.get("function", {})
            if function.get("name") != "validate_levels":
                messages.extend(
                    [
                        message,
                        {"role": "user", "content": "Call the validate_levels tool."},
                    ]
                )
                continue

            try:
                arguments = self._parse_json(function.get("arguments"))
                candidate = LEVELS_ADAPTER.validate_python(arguments)
                validate_level_map(candidate, perception.references())
                validation = {"valid": True, "errors": []}
                levels = candidate
            except (ValidationError, ValueError, json.JSONDecodeError) as error:
                validation = {"valid": False, "errors": [str(error)]}

            messages.extend(
                [
                    message,
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(validation, separators=(",", ":")),
                    },
                ]
            )
            if levels is not None:
                break

        if levels is None:
            raise ValueError("narrative level validation failed after 3 attempts")

        message = await self._call_model(messages)
        draft = NarrativeDraft(markdown=message.get("content"), levels=levels)
        return NarrativeRecord(
            **draft.model_dump(),
            source_session=perception.source_session,
            plan_session=opening.plan_session,
            generated_at=datetime.now(UTC),
            model=self.settings.narrative_model,
            perception=perception,
            opening_context=opening,
        )

    async def _call_model(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.settings.narrative_model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 1_200,
            "chat_template_kwargs": {"thinking": False},
        }
        if tools is not None:
            request.update({"tools": tools, "tool_choice": "auto"})
        response = await self.client.post(
            self.settings.model_url(),
            headers=self.settings.model_headers(),
            json=request,
        )
        response.raise_for_status()
        payload = response.json()
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("model provider returned no narrative message") from error
        if not isinstance(message, dict):
            raise ValueError("model provider returned an invalid narrative message")
        return message

    @staticmethod
    def _build_prompt(
        perception: SessionPerception,
        opening: OpeningContext,
    ) -> str:
        payload = {
            "prior_session": perception.model_dump(mode="json"),
            "opening_gap": opening.model_dump(mode="json"),
            "allowed_references": perception.references(),
        }
        return (
            "Choose the active level map, validate it, then write the compact daily narrative "
            "for the plan session. Treat it as context, not as a trade signal.\n\n"
            + json.dumps(payload, separators=(",", ":"))
        )

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        if not isinstance(content, str):
            raise ValueError("tool arguments must be JSON text")
        stripped = content.strip()
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must be a JSON object")
        return parsed


def validate_levels(draft: NarrativeDraft, references: dict[str, float]) -> None:
    validate_level_map(draft.levels, references)


def validate_level_map(
    levels: AboveStructureLevels | BalancedLevels | BelowStructureLevels,
    references: dict[str, float],
) -> None:
    allowed = list(references.values())

    def known(value: float) -> bool:
        return any(abs(value - candidate) < 0.005 for candidate in allowed)

    if isinstance(levels, BalancedLevels):
        values = [levels.pivot, *levels.upside_targets, *levels.downside_targets]
        if levels.upside_targets != sorted(levels.upside_targets):
            raise ValueError("upside_targets must be ascending")
        if levels.downside_targets != sorted(levels.downside_targets, reverse=True):
            raise ValueError("downside_targets must be descending")
        if any(target <= levels.pivot for target in levels.upside_targets):
            raise ValueError("upside targets must be above the pivot")
        if any(target >= levels.pivot for target in levels.downside_targets):
            raise ValueError("downside targets must be below the pivot")
    elif isinstance(levels, AboveStructureLevels):
        values = [levels.upside_trigger, *levels.upside_targets, *levels.support_repair]
        if levels.upside_targets != sorted(levels.upside_targets):
            raise ValueError("upside_targets must be ascending")
        if levels.support_repair != sorted(levels.support_repair, reverse=True):
            raise ValueError("support_repair must be descending")
        if any(target <= levels.upside_trigger for target in levels.upside_targets):
            raise ValueError("upside targets must be above the trigger")
        if any(repair >= levels.upside_trigger for repair in levels.support_repair):
            raise ValueError("support repair must be below the trigger")
    elif isinstance(levels, BelowStructureLevels):
        values = [levels.downside_trigger, *levels.downside_targets, *levels.resistance_repair]
        if levels.downside_targets != sorted(levels.downside_targets, reverse=True):
            raise ValueError("downside_targets must be descending")
        if levels.resistance_repair != sorted(levels.resistance_repair):
            raise ValueError("resistance_repair must be ascending")
        if any(target >= levels.downside_trigger for target in levels.downside_targets):
            raise ValueError("downside targets must be below the trigger")
        if any(repair <= levels.downside_trigger for repair in levels.resistance_repair):
            raise ValueError("resistance repair must be above the trigger")
    else:
        raise ValueError("unknown narrative level map")

    unknown = [value for value in values if not known(value)]
    if unknown:
        raise ValueError(f"narrative contains levels outside allowed_references: {unknown}")


def narrative_date() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).date()
