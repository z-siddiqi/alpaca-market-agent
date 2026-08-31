# Alpaca Market Agent

An autonomous, explainable SPY options agent for the Alpaca AI Trading Agents
Hackathon.

The agent interprets the live SPY auction using Auction Market Theory (AMT),
forms a falsifiable directional thesis, and expresses qualified ideas through a
single long call or put in an Alpaca competition paper account.

The model gathers evidence, interprets the market, selects and sizes an option,
and calls Alpaca MCP trading tools directly. A compact trading mandate keeps its
risk decisions explicit, while the dedicated paper account contains the
experiment.

## Status

The project is in early implementation. Account access, market-data paths,
option-chain retrieval, Featherless inference, Alpaca MCP, and a paper option
order lifecycle have been verified. The Python service generates and stores the
prior-session Kimi narrative, assembles live turn context, and runs a validated DeepSeek
evaluation with targeted Alpaca MCP tools. Decisions are stored in Firestore.
Five-minute weekday evaluations are scheduled in GCP. Paper order submission
is controlled by a deployment kill switch.

## Initial scope

- SPY and its listed options
- long calls and puts with 1–5 days to expiration
- policy-sized contract quantity and one open position at a time
- five-minute or event-triggered agent evaluations
- a preloaded turn context with account state, P&L, daily plan, and live auction state
- targeted option-chain inspection through Alpaca MCP
- explicit trading policy and direct Alpaca MCP paper execution
- persistent, replayable decision and order traces

The initial version excludes 0DTE contracts, short premium, multiple underlyings, autonomous
rolls, and live-money trading.

## Architecture

The runtime gives the model a timestamped snapshot of the state it needs on every
turn, plus Alpaca MCP market and trading tools. The model can hold without making
tool calls, inspect the option chain only after forming a thesis, and submit,
replace, cancel, or close paper orders itself.

Kimi K3 generates the daily narrative and DeepSeek V4 Flash runs trading evaluations
through Featherless. Alpaca remains authoritative for market and account state;
Alpaca MCP is the model's interface for targeted option data and paper trading.

## Run the narrative slice

```bash
uv sync
gcloud auth application-default login
uv run uvicorn main:app --reload
```

After the first five-minute SPY bar completes, generate the session narrative:

```bash
curl -X POST http://localhost:8000/narratives/generate \
  -H 'content-type: application/json' \
  -d '{}'
```

Pass `{"plan_date":"YYYY-MM-DD"}` to replay a completed session. The service
uses historical SIP bars for the prior RTH profile, IEX bars for the opening gap,
and Kimi K3 through Featherless for the Augur-style narrative and level
map. Firestore uses Application Default Credentials locally and the Cloud Run
service account when deployed. Create the project's default Firestore database
in Native mode before running the endpoint, and set `GCP_PROJECT_ID` when it is
not the project selected by your local credentials.

Run one agent evaluation with:

```bash
curl -X POST http://localhost:8000/ticks/evaluate
```

The endpoint assembles its context internally, allows targeted Alpaca MCP reads,
and stores one immutable decision per five-minute slot. MCP order tools are
model-visible but rejected locally unless `ORDER_SUBMISSION_ENABLED=true`.

## Documentation

- [Product vision](docs/vision.md)
- [Strategy](docs/strategy.md)
- [Policy](docs/policy.md)
- [Architecture](docs/architecture.md)
- [Discovery findings](docs/discovery.md)

## Safety

This project is designed for the dedicated Alpaca competition paper account. It
is experimental software and is not intended for live-money trading or investment
advice.
