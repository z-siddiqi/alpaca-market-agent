from datetime import date, datetime
from typing import Annotated, Any, Literal

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


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MarketClockState(CamelModel):
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class AccountState(CamelModel):
    status: str
    currency: str
    equity: float
    session_starting_equity: float
    daily_equity_pnl: float
    daily_equity_pnl_percent: float
    daily_loss_floor: float
    daily_loss_headroom: float
    buying_power: float
    options_buying_power: float
    cash: float
    account_blocked: bool
    trading_blocked: bool
    trade_suspended_by_user: bool


class PositionState(CamelModel):
    symbol: str
    asset_class: str
    side: str
    quantity: float
    average_entry_price: float
    current_price: float
    market_value: float
    cost_basis: float
    unrealized_pnl: float
    unrealized_pnl_percent: float


class OrderState(CamelModel):
    order_id: str
    client_order_id: str
    symbol: str
    asset_class: str
    side: str
    order_type: str
    time_in_force: str
    quantity: float
    filled_quantity: float
    limit_price: float | None
    status: str
    submitted_at: datetime | None


class LatestTradeState(CamelModel):
    timestamp: datetime
    price: float
    size: float


class LatestQuoteState(CamelModel):
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float
    ask_size: float


class LiveMarketState(CamelModel):
    symbol: Literal["SPY"] = "SPY"
    feed: Literal["iex"] = "iex"
    as_of: datetime
    latest_trade: LatestTradeState | None
    latest_quote: LatestQuoteState | None
    trade_freshness_seconds: float | None
    regular_session_open: float | None
    regular_session_high: float | None
    regular_session_low: float | None
    latest_price: float | None
    premarket_high: float | None
    premarket_low: float | None
    initial_balance_high: float | None
    initial_balance_low: float | None
    initial_balance_complete: bool
    extension_up: float | None
    extension_down: float | None
    five_minute_atr14: float | None
    latest_completed_bar_at: datetime | None
    five_minute_bars_current: bool
    recent_completed_bars: list[Bar]


EntryWindowState = Literal[
    "market_closed",
    "entry_delay",
    "eligible",
    "closing_only",
]


class EntryWindow(CamelModel):
    state: EntryWindowState
    entry_opens_at: datetime
    entry_closes_at: datetime
    session_closes_at: datetime


class TickContext(CamelModel):
    evaluated_at: datetime
    trading_date: date
    paper_account: Literal[True] = True
    clock: MarketClockState
    entry_window: EntryWindow
    entry_blockers: list[str]
    exit_reasons: list[str] = Field(default_factory=list)
    cancel_order_ids: list[str] = Field(default_factory=list)
    last_position_closed_at: datetime | None = None
    cooldown_ends_at: datetime | None = None
    account: AccountState
    positions: list[PositionState]
    working_orders: list[OrderState]
    narrative: NarrativeRecord | None
    market: LiveMarketState


AgentAction = Literal["hold", "buy_call", "buy_put", "close_position"]
AuctionState = Literal["balance", "discovery_up", "discovery_down", "unclear"]


class AgentDecisionDraft(CamelModel):
    action: AgentAction
    auction_state: AuctionState
    confidence: float = Field(ge=0, le=1)
    thesis: str = Field(min_length=1, max_length=1_000)
    active_references: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    entry_price: float | None = None
    invalidation_price: float | None = None
    target_price: float | None = None
    option_symbol: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    limit_price: float | None = Field(default=None, gt=0)
    policy_checks: list[str] = Field(default_factory=list, max_length=20)
    hold_reasons: list[str] = Field(default_factory=list, max_length=8)


class OptionEvidence(CamelModel):
    symbol: str
    expiration: date
    right: Literal["call", "put"]
    active: bool
    tradable: bool
    delta: float
    bid: float
    ask: float
    midpoint: float
    spread: float
    spread_percent: float
    quote_at: datetime
    quantity: int
    total_debit: float
    breaker_loss: float


class OptionValidationCheck(CamelModel):
    name: str
    passed: bool
    detail: str


class OptionOrderProposal(CamelModel):
    action: Literal["buy_call", "buy_put"]
    symbol: str
    quantity: int = Field(ge=1)
    limit_price: float = Field(gt=0)


class OptionOrderValidation(OptionOrderProposal):
    valid: bool
    checks: list[OptionValidationCheck]
    rejection_reasons: list[str]
    evidence: OptionEvidence | None = None
    options_buying_power: float
    daily_loss_headroom: float


class AgentDecision(AgentDecisionDraft):
    decision_id: str
    evaluated_at: datetime
    option_evidence: OptionEvidence | None = None


class ToolCallRecord(CamelModel):
    name: str
    arguments: dict[str, Any]
    result: Any
    blocked: bool = False
    called_at: datetime | None = None


class DecisionRecord(CamelModel):
    tick_id: str
    trading_date: date
    model: str
    context: TickContext
    decision: AgentDecision
    tool_calls: list[ToolCallRecord]
    created_at: datetime
