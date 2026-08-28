from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class Bar(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


class PeriodSummary(BaseModel):
    label: str
    start: datetime
    open: float
    high: float
    low: float
    close: float


class SessionPerception(BaseModel):
    symbol: Literal["SPY"] = "SPY"
    source_session: date
    open: float
    high: float
    low: float
    close: float
    range: float
    midpoint: float
    poc: float
    vah: float
    val: float
    ib_high: float
    ib_low: float
    ib_range: float
    extended_up: bool
    extended_down: bool
    extension_up: float
    extension_down: float
    close_location: float
    periods: list[PeriodSummary]
    bar_count: int
    expected_bar_count: int
    complete: bool

    def references(self) -> dict[str, float]:
        return {
            "session_open": self.open,
            "session_high": self.high,
            "session_low": self.low,
            "session_close": self.close,
            "poc": self.poc,
            "vah": self.vah,
            "val": self.val,
            "ib_high": self.ib_high,
            "ib_low": self.ib_low,
        }


GapClassification = Literal["normal", "elevated", "large", "extreme"]
OpeningLocation = Literal[
    "true_gap_up",
    "above_value",
    "inside_value",
    "below_value",
    "true_gap_down",
]


class OpeningContext(BaseModel):
    plan_session: date
    open: float
    open_feed: Literal["iex"] = "iex"
    prior_close: float
    prior_close_feed: Literal["sip"] = "sip"
    gap_points: float
    gap_percent: float
    gap_classification: GapClassification
    location: OpeningLocation
    first_five_minute: Bar | None


class LevelMap(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class BalancedLevels(LevelMap):
    kind: Literal["balanced"]
    pivot: float
    upside_targets: list[float] = Field(min_length=1, max_length=4)
    downside_targets: list[float] = Field(min_length=1, max_length=4)


class AboveStructureLevels(LevelMap):
    kind: Literal["above_structure"]
    upside_trigger: float
    upside_targets: list[float] = Field(max_length=4)
    support_repair: list[float] = Field(min_length=1, max_length=4)


class BelowStructureLevels(LevelMap):
    kind: Literal["below_structure"]
    downside_trigger: float
    downside_targets: list[float] = Field(max_length=4)
    resistance_repair: list[float] = Field(min_length=1, max_length=4)


NarrativeLevels = Annotated[
    BalancedLevels | AboveStructureLevels | BelowStructureLevels,
    Field(discriminator="kind"),
]


class NarrativeDraft(BaseModel):
    markdown: str = Field(min_length=40, max_length=4_000)
    levels: NarrativeLevels

    @field_validator("markdown")
    @classmethod
    def validate_sections(cls, value: str) -> str:
        context_heading = "## Contextual Analysis & Plan"
        levels_heading = "## Levels of Interest"
        if context_heading not in value or levels_heading not in value:
            raise ValueError("narrative must contain both Augur Markdown sections")
        if value.index(context_heading) > value.index(levels_heading):
            raise ValueError("Contextual Analysis & Plan must precede Levels of Interest")
        return value


class NarrativeRecord(NarrativeDraft):
    source_session: date
    plan_session: date
    generated_at: datetime
    model: str
    perception: SessionPerception
    opening_context: OpeningContext


class GenerateNarrativeRequest(BaseModel):
    plan_date: date | None = None
