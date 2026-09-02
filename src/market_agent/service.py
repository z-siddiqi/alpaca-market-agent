import asyncio
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException
from httpx import HTTPError

from market_agent.agent import AgentEvaluator, AlpacaMcpClient
from market_agent.alpaca import AlpacaClient
from market_agent.config import Settings
from market_agent.models import (
    DecisionRecord,
    GenerateNarrativeRequest,
    NarrativeRecord,
    TradePlan,
)
from market_agent.narrative import (
    NarrativeGenerator,
    build_opening_context,
    build_session_perception,
    narrative_date,
)
from market_agent.policy import (
    PREMIUM_BREAKER_FRACTION,
    PREMIUM_BREAKEVEN_TRIGGER_FRACTION,
    PREMIUM_PROFIT_TARGET_FRACTION,
)
from market_agent.risk import PositionRiskManager, forced_exit_record
from market_agent.storage import DecisionStore, NarrativeStore, TradePlanStore
from market_agent.tick import TickContextBuilder, build_entry_window, parse_clock, tick_id

settings = Settings()
alpaca = AlpacaClient(settings)
generator = NarrativeGenerator(settings)
evaluator = AgentEvaluator(settings)
store = NarrativeStore(settings.gcp_project_id)
decision_store = DecisionStore(settings.gcp_project_id)
trade_plan_store = TradePlanStore(settings.gcp_project_id)
tick_context_builder = TickContextBuilder(alpaca, store, trade_plan_store)
risk_manager = PositionRiskManager(settings, alpaca)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await alpaca.close()
    await generator.close()
    await evaluator.close()
    store.close()
    decision_store.close()
    trade_plan_store.close()


app = FastAPI(title="Alpaca Market Agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/narratives/{plan_date}", response_model=NarrativeRecord)
async def get_narrative(plan_date: date) -> NarrativeRecord:
    narrative = await store.get(plan_date)
    if narrative is None:
        raise HTTPException(status_code=404, detail="narrative not found")
    return narrative


@app.post("/narratives/generate", response_model=NarrativeRecord)
async def generate_narrative(request: GenerateNarrativeRequest) -> NarrativeRecord:
    plan_date = request.plan_date or narrative_date()
    existing = await store.get(plan_date)
    if existing is not None:
        return existing

    try:
        source_date, expected_bars, prior_bars, opening_bars = await alpaca.narrative_bars(
            plan_date
        )
        perception = build_session_perception(
            prior_bars,
            source_date,
            expected_bar_count=expected_bars,
        )
        if not perception.complete:
            completeness = f"{perception.bar_count}/{perception.expected_bar_count}"
            raise ValueError(f"prior session is incomplete: {completeness} bars")
        opening = build_opening_context(plan_date, perception, opening_bars)
        if opening.first_five_minute is None:
            raise ValueError("the first five-minute SPY bar is not complete")
        narrative = await generator.generate(perception, opening)
    except HTTPError as error:
        detail = error.response.text if error.response is not None else str(error)
        status_code = error.response.status_code if error.response is not None else 502
        raise HTTPException(status_code=status_code, detail=detail) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    return await store.put(narrative)


@app.post("/ticks/evaluate", response_model=DecisionRecord)
async def evaluate_tick() -> DecisionRecord:
    try:
        context = await tick_context_builder.build()
        current_tick_id = tick_id(context.evaluated_at)
        reconciliation = await risk_manager.reconcile(context)
        context = reconciliation.context
        existing = await decision_store.get(current_tick_id)
        if existing is not None:
            return existing
        if reconciliation.mandatory_exit:
            return await decision_store.put(
                forced_exit_record(
                    tick_id=current_tick_id,
                    reconciliation=reconciliation,
                )
            )
        async with AlpacaMcpClient(settings, context) as tools:
            record = await evaluator.evaluate(current_tick_id, context, tools)
            if record.decision.action in {"buy_call", "buy_put"}:
                decision = record.decision
                if (
                    decision.entry_price is None
                    or decision.invalidation_price is None
                    or decision.target_price is None
                    or decision.option_symbol is None
                    or decision.quantity is None
                    or decision.limit_price is None
                ):
                    raise ValueError("validated entry is missing its durable trade plan")
                await trade_plan_store.put(
                    TradePlan(
                        decision_id=decision.decision_id,
                        trading_date=record.trading_date,
                        created_at=record.created_at,
                        action=decision.action,
                        thesis=decision.thesis,
                        entry_price=decision.entry_price,
                        invalidation_price=decision.invalidation_price,
                        target_price=decision.target_price,
                        option_symbol=decision.option_symbol,
                        quantity=decision.quantity,
                        intended_limit_price=decision.limit_price,
                        premium_loss_fraction=PREMIUM_BREAKER_FRACTION,
                        profit_target_fraction=PREMIUM_PROFIT_TARGET_FRACTION,
                        breakeven_trigger_fraction=PREMIUM_BREAKEVEN_TRIGGER_FRACTION,
                    )
                )
                entry_action = await tools.submit_validated_entry(decision)
                record = record.model_copy(
                    update={"tool_calls": [*record.tool_calls, entry_action]}
                )
        post_model_risk_actions = []
        if record.decision.action in {"buy_call", "buy_put"} and record.decision.option_symbol:
            settled = await risk_manager.settle_entry(context, record.decision.option_symbol)
            context = settled.context
            post_model_risk_actions = settled.actions
        record = record.model_copy(
            update={
                "context": context,
                "tool_calls": [
                    *reconciliation.actions,
                    *record.tool_calls,
                    *post_model_risk_actions,
                ],
            }
        )
        return await decision_store.put(record)
    except HTTPError as error:
        detail = error.response.text if error.response is not None else str(error)
        status_code = error.response.status_code if error.response is not None else 502
        raise HTTPException(status_code=status_code, detail=detail) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/positions/flatten")
async def flatten_positions() -> dict[str, object]:
    if not settings.alpaca_paper_trade:
        raise HTTPException(status_code=409, detail="forced flatten is paper-account only")
    if not settings.order_submission_enabled:
        raise HTTPException(status_code=409, detail="order submission is disabled")

    try:
        window = build_entry_window(parse_clock(await alpaca.trading_clock()))
        if window.state != "closing_only":
            return {
                "status": "outside_closing_window",
                "ordersCancelled": [],
                "positionsClosed": [],
            }

        positions, orders = await asyncio.gather(alpaca.positions(), alpaca.open_orders())
        protective_stops = [
            order
            for order in orders
            if order.get("side") == "sell" and order.get("type") in {"stop", "stop_limit"}
        ]
        working_sells = {
            str(order.get("symbol", ""))
            for order in orders
            if order.get("side") == "sell" and order.get("type") not in {"stop", "stop_limit"}
        }
        buy_orders = [order for order in orders if order.get("side") == "buy"]
        cancelled_orders = [*buy_orders, *protective_stops]
        await asyncio.gather(*(alpaca.cancel_order(str(order["id"])) for order in cancelled_orders))
        close_results = await asyncio.gather(
            *(
                alpaca.close_position(str(position["symbol"]))
                for position in positions
                if str(position["symbol"]) not in working_sells
            )
        )
        return {
            "status": "flatten_requested",
            "ordersCancelled": [str(order["id"]) for order in cancelled_orders],
            "positionsClosed": [
                str(position["symbol"])
                for position in positions
                if str(position["symbol"]) not in working_sells
            ],
            "alpacaResponses": [result for result in close_results if result is not None],
        }
    except HTTPError as error:
        detail = error.response.text if error.response is not None else str(error)
        status_code = error.response.status_code if error.response is not None else 502
        raise HTTPException(status_code=status_code, detail=detail) from error
