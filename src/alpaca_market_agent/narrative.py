import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import ValidationError

from alpaca_market_agent.config import Settings
from alpaca_market_agent.models import (
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

Return JSON with exactly two keys: markdown and levels.

markdown must contain these sections in order:

## Contextual Analysis & Plan

Two or three short paragraphs. Explain how the prior RTH auction developed using
its period OHLC, initial balance, extensions, value, and closing location. Then
explain how today's opening gap changes the relevant context. End with the selected
level map in plain language.

## Levels of Interest

A one-line introduction followed by two concise bullets describing the selected map.

levels must use one of the existing Augur map shapes:
- balanced: kind, pivot, upsideTargets, downsideTargets
- above_structure: kind, upsideTrigger, upsideTargets, supportRepair
- below_structure: kind, downsideTrigger, downsideTargets, resistanceRepair

Every level must exactly match a price in allowed_references. Do not calculate,
round, or invent levels. Targets must be nearest first. For a true gap beyond prior
structure, continuation targets may be empty; do not fabricate a target."""


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
        prompt = self._build_prompt(perception, opening)
        errors: list[str] = []
        for _attempt in range(2):
            content = await self._call_model(prompt, errors)
            try:
                draft = NarrativeDraft.model_validate(self._parse_json(content))
                validate_levels(draft, perception.references())
            except (ValidationError, ValueError, json.JSONDecodeError) as error:
                errors.append(str(error))
                continue
            return NarrativeRecord(
                **draft.model_dump(),
                source_session=perception.source_session,
                plan_session=opening.plan_session,
                generated_at=datetime.now(UTC),
                model=self.settings.narrative_model,
                perception=perception,
                opening_context=opening,
            )
        raise ValueError(f"narrative generation failed validation: {'; '.join(errors)}")

    async def _call_model(self, prompt: str, errors: list[str]) -> str:
        correction = ""
        if errors:
            correction = (
                "\n\nYour previous response failed validation. Correct these errors and return "
                "the full "
                "JSON object again:\n- " + "\n- ".join(errors)
            )
        response = await self.client.post(
            self.settings.gateway_url(),
            headers=self.settings.gateway_headers(),
            json={
                "model": self.settings.narrative_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt + correction},
                ],
                "temperature": 0.2,
                "max_tokens": 1_500,
            },
        )
        response.raise_for_status()
        payload = response.json()
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("AI Gateway returned no narrative content") from error

    @staticmethod
    def _build_prompt(perception: SessionPerception, opening: OpeningContext) -> str:
        payload = {
            "prior_session": perception.model_dump(mode="json"),
            "opening_gap": opening.model_dump(mode="json"),
            "allowed_references": perception.references(),
        }
        return (
            "Write the compact daily narrative for the plan session from this JSON. "
            "Treat the narrative as context, not as a trade signal.\n\n"
            + json.dumps(payload, separators=(",", ":"))
        )

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        stripped = content.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0]
            if stripped.lstrip().startswith("json"):
                stripped = stripped.lstrip()[4:].lstrip()
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError("narrative response must be a JSON object")
        return parsed


def validate_levels(draft: NarrativeDraft, references: dict[str, float]) -> None:
    allowed = list(references.values())

    def known(value: float) -> bool:
        return any(abs(value - candidate) < 0.005 for candidate in allowed)

    levels = draft.levels
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
