import asyncio
from datetime import UTC, date, datetime, time, timedelta

import pytest

from market_agent.alpaca import AlpacaClient
from market_agent.config import Settings
from market_agent.models import BalancedLevels, Bar, NarrativeDraft
from market_agent.narrative import (
    build_opening_context,
    build_session_perception,
)
from market_agent.narrative.tools import validate_level_map


def make_bar(minute: int, base: float, *, day: int = 27) -> Bar:
    return Bar(
        timestamp=datetime(2026, 8, day, 13, 30, tzinfo=UTC) + timedelta(minutes=minute),
        open=base,
        high=base + 0.10,
        low=base - 0.10,
        close=base + 0.05,
        volume=100,
    )


def test_prior_profile_and_opening_gap() -> None:
    prior = [make_bar(minute, 100 + minute * 0.01) for minute in range(390)]
    opening = [make_bar(minute, 105 + minute * 0.02, day=28) for minute in range(5)]

    perception = build_session_perception(prior, date(2026, 8, 27))
    context = build_opening_context(date(2026, 8, 28), perception, opening)

    assert perception.complete
    assert len(perception.periods) == 13
    assert context.location == "true_gap_up"
    assert context.first_five_minute is not None


def test_missing_sip_minutes_fall_back_to_iex() -> None:
    prior_date = date(2026, 8, 31)
    plan_date = date(2026, 9, 1)

    class FakeAlpacaClient(AlpacaClient):
        async def previous_session(self, _plan_date: date) -> tuple[date, time]:
            return prior_date, time(16)

        async def stock_bars(
            self,
            *,
            start: datetime,
            end: datetime,
            feed: str,
            timeframe: str = "1Min",
        ) -> list[Bar]:
            del end, timeframe
            if start.date() == plan_date:
                return [
                    make_bar(minute, 105).model_copy(
                        update={
                            "timestamp": datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
                            + timedelta(minutes=minute)
                        }
                    )
                    for minute in range(5)
                ]
            bars = [make_bar(minute, 100, day=31) for minute in range(390)]
            if feed == "sip":
                return [bar for minute, bar in enumerate(bars) if minute not in {17, 18}]
            return [make_bar(minute, 200, day=31) for minute in range(390)]

    client = FakeAlpacaClient(Settings())
    _source, expected, prior, _opening = asyncio.run(client.narrative_bars(plan_date))
    asyncio.run(client.close())

    assert len(prior) == expected == 390
    assert prior[16].open == 100
    assert prior[17].open == 200
    assert prior[18].open == 200


def test_narrative_cannot_invent_a_level() -> None:
    draft = NarrativeDraft(
        markdown=(
            "## Contextual Analysis & Plan\n\nPrior session.\n\n"
            "## Levels of Interest\n\n- Above\n- Below"
        ),
        levels=BalancedLevels(
            kind="balanced",
            pivot=100,
            upside_targets=[101.25],
            downside_targets=[99],
        ),
    )

    with pytest.raises(ValueError, match="outside allowed_references"):
        validate_level_map(draft.levels, {"poc": 100, "vah": 101, "val": 99})
