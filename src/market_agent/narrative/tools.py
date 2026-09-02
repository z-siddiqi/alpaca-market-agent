import json
from typing import Any

from pydantic import TypeAdapter

from market_agent.models import (
    AboveStructureLevels,
    BalancedLevels,
    BelowStructureLevels,
)

LevelMap = AboveStructureLevels | BalancedLevels | BelowStructureLevels

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

LEVELS_ADAPTER = TypeAdapter(LevelMap)


def validate_level_arguments(content: Any, references: dict[str, float]) -> LevelMap:
    if not isinstance(content, str):
        raise ValueError("tool arguments must be JSON text")
    parsed = json.loads(content.strip())
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    levels = LEVELS_ADAPTER.validate_python(parsed)
    validate_level_map(levels, references)
    return levels


def validate_level_map(levels: LevelMap, references: dict[str, float]) -> None:
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
