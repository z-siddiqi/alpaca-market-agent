import math
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from market_agent.models import (
    OptionEvidence,
    OptionOrderProposal,
    OptionOrderValidation,
    OptionValidationCheck,
    TickContext,
    ToolCallRecord,
)

FIXED_OPTION_QUANTITY = 15
DAILY_LOSS_FRACTION = 0.10
PREMIUM_BREAKER_FRACTION = 0.35
PREMIUM_BREAKEVEN_TRIGGER_FRACTION = 0.20
PREMIUM_PROFIT_TARGET_FRACTION = 0.50
MAXIMUM_QUOTE_AGE_SECONDS = 5
OPTION_SYMBOL = re.compile(r"^SPY(?P<expiration>\d{6})(?P<right>[CP])\d{8}$")


def validate_option_order(
    *,
    proposal: OptionOrderProposal,
    context: TickContext,
    contract: dict[str, Any],
    snapshot: dict[str, Any],
    checked_at: datetime,
) -> OptionOrderValidation:
    checks: list[OptionValidationCheck] = []
    rejection_reasons: list[str] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(OptionValidationCheck(name=name, passed=passed, detail=detail))
        if not passed:
            rejection_reasons.append(detail)

    check(
        "entry_policy",
        not context.entry_blockers,
        (
            "entry policy is clear"
            if not context.entry_blockers
            else f"entry blockers are active: {', '.join(context.entry_blockers)}"
        ),
    )
    check(
        "daily_loss_limit",
        context.account.daily_loss_headroom > 0,
        (
            f"daily loss headroom is ${context.account.daily_loss_headroom:.2f}"
            if context.account.daily_loss_headroom > 0
            else "daily loss limit has been reached"
        ),
    )
    check(
        "fixed_quantity",
        proposal.quantity == FIXED_OPTION_QUANTITY,
        (
            f"quantity is the fixed {FIXED_OPTION_QUANTITY}-contract risk unit"
            if proposal.quantity == FIXED_OPTION_QUANTITY
            else f"quantity must be exactly {FIXED_OPTION_QUANTITY} contracts"
        ),
    )

    symbol_match = OPTION_SYMBOL.fullmatch(proposal.symbol)
    check(
        "spy_occ_symbol",
        symbol_match is not None,
        (
            "symbol is a SPY OCC option symbol"
            if symbol_match is not None
            else "symbol must be a SPY OCC option symbol"
        ),
    )

    expiration: date | None = None
    occ_right: str | None = None
    if symbol_match is not None:
        expiration = datetime.strptime(symbol_match.group("expiration"), "%y%m%d").date()
        occ_right = "call" if symbol_match.group("right") == "C" else "put"

    expected_right = "call" if proposal.action == "buy_call" else "put"
    check(
        "action_right",
        occ_right == expected_right,
        (
            f"{proposal.action} agrees with the OCC {occ_right} right"
            if occ_right == expected_right
            else f"{proposal.action} requires a {expected_right} contract"
        ),
    )

    days_to_expiration = (
        (expiration - context.trading_date).days if expiration is not None else None
    )
    check(
        "days_to_expiration",
        days_to_expiration is not None and 1 <= days_to_expiration <= 5,
        (
            f"contract has {days_to_expiration} calendar DTE"
            if days_to_expiration is not None and 1 <= days_to_expiration <= 5
            else "contract must have 1-5 calendar DTE"
        ),
    )

    contract_symbol = str(contract.get("symbol", ""))
    contract_status = str(contract.get("status", "")).lower()
    contract_tradable = contract.get("tradable") is True
    contract_underlying = str(contract.get("underlying_symbol", ""))
    contract_right = str(contract.get("type", "")).lower()
    contract_expiration = _parse_date(contract.get("expiration_date"))
    check(
        "contract_identity",
        contract_symbol == proposal.symbol,
        (
            "contract metadata matches the proposed symbol"
            if contract_symbol == proposal.symbol
            else "contract metadata does not match the proposed symbol"
        ),
    )
    check(
        "contract_underlying",
        contract_underlying == "SPY",
        (
            "contract underlying is SPY"
            if contract_underlying == "SPY"
            else "contract metadata must identify SPY as the underlying"
        ),
    )
    check(
        "contract_active",
        contract_status == "active",
        ("contract status is active" if contract_status == "active" else "contract must be active"),
    )
    check(
        "contract_tradable",
        contract_tradable,
        "contract is tradable" if contract_tradable else "contract must be tradable",
    )
    check(
        "contract_right",
        contract_right == expected_right,
        (
            f"contract metadata identifies a {expected_right}"
            if contract_right == expected_right
            else f"contract metadata must identify a {expected_right}"
        ),
    )
    check(
        "contract_expiration",
        contract_expiration is not None and contract_expiration == expiration,
        (
            "contract metadata agrees with the OCC expiration"
            if contract_expiration is not None and contract_expiration == expiration
            else "contract metadata expiration must agree with the OCC symbol"
        ),
    )

    greeks = snapshot.get("greeks") if isinstance(snapshot.get("greeks"), dict) else {}
    delta = _optional_float(greeks.get("delta"))
    check(
        "delta",
        delta is not None and 0.55 <= abs(delta) <= 0.65,
        (
            f"absolute delta is {abs(delta):.4f}"
            if delta is not None and 0.55 <= abs(delta) <= 0.65
            else "absolute delta must be between 0.55 and 0.65"
        ),
    )

    quote = snapshot.get("latestQuote") if isinstance(snapshot.get("latestQuote"), dict) else {}
    bid = _optional_float(quote.get("bp"))
    ask = _optional_float(quote.get("ap"))
    quote_valid = bid is not None and ask is not None and bid > 0 and ask > 0 and bid <= ask
    check(
        "two_sided_quote",
        quote_valid,
        (
            f"quote is two-sided at {bid:.2f} x {ask:.2f}"
            if quote_valid and bid is not None and ask is not None
            else "option quote must be positive, two-sided, and not crossed"
        ),
    )

    midpoint = (bid + ask) / 2 if quote_valid and bid is not None and ask is not None else None
    spread = ask - bid if quote_valid and bid is not None and ask is not None else None
    spread_percent = spread / midpoint if spread is not None and midpoint else None
    spread_valid = (
        spread is not None
        and spread_percent is not None
        and spread <= 0.15
        and spread_percent <= 0.05
    )
    check(
        "spread",
        spread_valid,
        (
            f"spread is ${spread:.2f} ({spread_percent:.2%})"
            if spread_valid and spread is not None and spread_percent is not None
            else "option spread must be no more than $0.15 and 5% of midpoint"
        ),
    )

    quote_at = _parse_datetime(quote.get("t"))
    quote_age = max(0, (checked_at - quote_at).total_seconds()) if quote_at is not None else None
    quote_fresh = quote_age is not None and quote_age <= MAXIMUM_QUOTE_AGE_SECONDS
    check(
        "quote_freshness",
        quote_fresh,
        (
            f"quote age is {quote_age:.1f}s"
            if quote_fresh and quote_age is not None
            else "option quote must be timestamped no more than five seconds before validation"
        ),
    )

    limit_valid = (
        quote_valid and bid is not None and ask is not None and bid <= proposal.limit_price <= ask
    )
    check(
        "limit_price",
        limit_valid,
        (
            "limit price is inside the current quote"
            if limit_valid
            else "option limit price must be between the current bid and ask"
        ),
    )

    limit_decimal = Decimal(str(proposal.limit_price))
    penny_aligned = limit_decimal == limit_decimal.quantize(Decimal("0.01"))
    check(
        "price_increment",
        penny_aligned,
        (
            "limit price uses a valid SPY penny increment"
            if penny_aligned
            else "option limit price must use a $0.01 increment"
        ),
    )

    total_debit = proposal.limit_price * 100 * proposal.quantity
    breaker_loss = total_debit * PREMIUM_BREAKER_FRACTION
    buying_power_valid = context.account.options_buying_power >= total_debit
    check(
        "options_buying_power",
        buying_power_valid,
        (
            f"${total_debit:.2f} debit fits ${context.account.options_buying_power:.2f} "
            "options buying power"
            if buying_power_valid
            else (
                f"${total_debit:.2f} debit exceeds "
                f"${context.account.options_buying_power:.2f} options buying power"
            )
        ),
    )

    evidence = None
    if all(
        value is not None
        for value in (
            expiration,
            occ_right,
            delta,
            bid,
            ask,
            midpoint,
            spread,
            spread_percent,
            quote_at,
        )
    ):
        evidence = OptionEvidence(
            symbol=proposal.symbol,
            expiration=expiration,
            right=occ_right,
            active=contract_status == "active",
            tradable=contract_tradable,
            delta=delta,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            spread=spread,
            spread_percent=spread_percent,
            quote_at=quote_at,
            quantity=proposal.quantity,
            total_debit=total_debit,
            breaker_loss=breaker_loss,
        )

    return OptionOrderValidation(
        **proposal.model_dump(),
        valid=not rejection_reasons,
        checks=checks,
        rejection_reasons=rejection_reasons,
        evidence=evidence,
        options_buying_power=context.account.options_buying_power,
        daily_loss_headroom=context.account.daily_loss_headroom,
    )


def validated_option_evidence(
    *,
    action: str,
    symbol: str,
    quantity: int,
    limit_price: float,
    tool_calls: list[ToolCallRecord],
) -> OptionEvidence:
    for call in reversed(tool_calls):
        if call.name != "validate_option_order" or call.blocked:
            continue
        try:
            result = OptionOrderValidation.model_validate(call.result)
        except ValueError:
            continue
        if (
            result.valid
            and result.action == action
            and result.symbol == symbol
            and result.quantity == quantity
            and math.isclose(result.limit_price, limit_price, rel_tol=0, abs_tol=1e-9)
            and result.evidence is not None
        ):
            return result.evidence
    raise ValueError("entry is missing matching successful same-turn option validation")


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
