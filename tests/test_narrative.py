from datetime import UTC, date, datetime, timedelta

import pytest

from alpaca_market_agent.models import BalancedLevels, Bar, NarrativeDraft
from alpaca_market_agent.narrative import validate_levels
from alpaca_market_agent.profile import build_opening_context, build_session_perception


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
        validate_levels(draft, {"poc": 100, "vah": 101, "val": 99})
