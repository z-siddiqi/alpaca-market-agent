import math
import re
from datetime import datetime
from typing import Any

from alpaca_market_agent.models import OptionEvidence, TickContext, ToolCallRecord

OPTION_SYMBOL = re.compile(r"^SPY(?P<expiration>\d{6})(?P<right>[CP])\d{8}$")


def validate_option_candidate(
    *,
    symbol: str,
    quantity: int,
    limit_price: float,
    context: TickContext,
    tool_calls: list[ToolCallRecord],
) -> OptionEvidence:
    if context.entry_blockers:
        raise ValueError("option candidate is invalid while context has entry blockers")

    match = OPTION_SYMBOL.fullmatch(symbol)
    if match is None:
        raise ValueError("option candidate must be a SPY OCC symbol")
    expiration = datetime.strptime(match.group("expiration"), "%y%m%d").date()
    days_to_expiration = (expiration - context.trading_date).days
    if not 1 <= days_to_expiration <= 5:
        raise ValueError("option candidate must have 1-5 calendar DTE")

    snapshot, checked_at = _find_snapshot(symbol, tool_calls)
    greeks = snapshot.get("greeks") or {}
    delta_value = greeks.get("delta")
    if delta_value is None:
        raise ValueError("option snapshot is missing delta")
    delta = float(delta_value)
    if not 0.55 <= abs(delta) <= 0.65:
        raise ValueError(f"option delta {delta} is outside the 0.55-0.65 band")

    quote = snapshot.get("latestQuote") or {}
    bid = float(quote.get("bp") or 0)
    ask = float(quote.get("ap") or 0)
    if bid <= 0 or ask <= 0 or bid > ask:
        raise ValueError("option quote must be positive, two-sided, and not crossed")
    midpoint = (bid + ask) / 2
    spread = ask - bid
    spread_percent = spread / midpoint
    if spread > 0.15 or spread_percent > 0.05:
        raise ValueError("option quote exceeds the spread limit")

    quote_timestamp = quote.get("t")
    if quote_timestamp is None:
        raise ValueError("option quote is missing its timestamp")
    quote_at = datetime.fromisoformat(str(quote_timestamp).replace("Z", "+00:00"))
    if checked_at is not None and max(0, (checked_at - quote_at).total_seconds()) > 5:
        raise ValueError("option quote is stale")
    if not bid <= limit_price <= ask:
        raise ValueError("option limit price must be between the current bid and ask")

    debit = limit_price * 100
    planned_loss = debit * 0.35
    account = context.account
    maximum_quantity = math.floor(
        min(
            account.session_starting_equity * 0.05 / debit,
            account.session_starting_equity * 0.02 / planned_loss,
            account.daily_loss_headroom / planned_loss,
            account.options_buying_power / debit,
        )
    )
    if quantity > maximum_quantity:
        raise ValueError(
            f"option quantity {quantity} exceeds maximum quantity {maximum_quantity}"
        )

    return OptionEvidence(
        symbol=symbol,
        expiration=expiration,
        right="call" if match.group("right") == "C" else "put",
        delta=delta,
        bid=bid,
        ask=ask,
        midpoint=midpoint,
        spread=spread,
        spread_percent=spread_percent,
        quote_at=quote_at,
        maximum_quantity=maximum_quantity,
    )


def _find_snapshot(
    symbol: str,
    tool_calls: list[ToolCallRecord],
) -> tuple[dict[str, Any], datetime | None]:
    for call in reversed(tool_calls):
        if call.name != "get_option_snapshot" or call.blocked:
            continue
        result = call.result if isinstance(call.result, dict) else {}
        structured = result.get("structuredContent") or {}
        data = structured.get("data") or {}
        snapshots = data.get("snapshots") or {}
        snapshot = snapshots.get(symbol)
        if isinstance(snapshot, dict):
            return snapshot, call.called_at
    raise ValueError("selected option is missing a successful MCP snapshot")
