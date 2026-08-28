from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException
from httpx import HTTPError

from alpaca_market_agent.alpaca import AlpacaClient
from alpaca_market_agent.config import Settings
from alpaca_market_agent.models import GenerateNarrativeRequest, NarrativeRecord
from alpaca_market_agent.narrative import NarrativeGenerator, narrative_date
from alpaca_market_agent.profile import build_opening_context, build_session_perception
from alpaca_market_agent.storage import NarrativeStore

settings = Settings()
alpaca = AlpacaClient(settings)
generator = NarrativeGenerator(settings)
store = NarrativeStore(settings.gcp_project_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await alpaca.close()
    await generator.close()
    store.close()


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
        raise HTTPException(status_code=502, detail=detail) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    return await store.put(narrative)
