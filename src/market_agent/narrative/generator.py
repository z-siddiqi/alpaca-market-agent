import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import ValidationError

from market_agent.config import Settings
from market_agent.models import (
    NarrativeDraft,
    NarrativeRecord,
    OpeningContext,
    SessionPerception,
)
from market_agent.narrative.prompt import SYSTEM_PROMPT
from market_agent.narrative.tools import (
    VALIDATE_LEVELS_TOOL,
    LevelMap,
    validate_level_arguments,
)


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
        levels: LevelMap | None = None

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
                candidate = validate_level_arguments(
                    function.get("arguments"),
                    perception.references(),
                )
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

def narrative_date() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).date()
