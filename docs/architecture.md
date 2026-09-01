# Architecture

Status: Draft

## System diagram

```mermaid
flowchart LR
    scheduler["Cloud Scheduler"] --> runtime["Private Cloud Run service"]
    scheduler --> watcher["Position watcher job"]
    runtime --> context["Preloaded turn context"]
    state[("Durable state and audit")] --> context
    context --> agent["DeepSeek V4 Flash agent"]
    agent <--> mcp["Alpaca MCP<br/>option data and orders"]
    mcp <--> account["Competition paper account"]
    runtime <--> account
    watcher <--> account
    watcher --> state
    runtime --> state
    backstop["Kill switch and forced close"] --> mcp
```

## Preloaded turn context

Before invoking the model, the runtime assembles one compact, timestamped turn
context from durable state and fresh Alpaca reconciliation reads. It contains:

- market clock, session window, and evaluation timestamp
- session-starting equity, current equity, buying power, realized and unrealized
  P&L, and 10% daily loss headroom
- current positions, working orders, most recent close, and cooldown state
- the prior-session profile, compact daily narrative, and active reference levels
- current SPY price, session range, initial balance, recent completed bars, and
  feed freshness
- any unresolved thesis, decision, or order lifecycle state
- the applicable trading mandate, kill-switch state, and provenance timestamps

The snapshot is evidence, not a precomputed decision. Option-chain data is not
preloaded: it is larger, changes quickly, and is irrelevant to most turns.

## Daily narrative generation

The daily narrative is generated once after the first five-minute SPY bar closes
and before entries become eligible at 09:40 ET. Its only market inputs are:

- the most recent completed SPY regular session from historical SIP minute bars
- the current IEX regular-session opening print and its observed gap from the
  historical SIP prior close
- the opening location relative to prior range and value
- the first completed five-minute SPY bar from IEX

The generator calculates the prior-session profile, then DeepSeek V4 Pro chooses a
`balanced`, `above_structure`, or `below_structure` map through a validation tool.
Invalid shapes, ordering, or invented prices are returned to the model for correction.
After its chosen map passes, the model writes the familiar `Contextual Analysis &
Plan` and `Levels of Interest` Markdown. The completed narrative and structured
levels are cached and preloaded on each trading turn.

This slice does not load older sessions, retrieve similar narratives, or build a
knowledge corpus. Level selection may be corrected twice after an invalid response; if it
still fails, the agent receives the calculated profile without generated prose.

## Agent loop

The model receives the preloaded context, AMT mandate, trading policy, and tools.
It can record a hold without a tool call. After forming a directional thesis, it
may inspect a narrow option chain through Alpaca MCP, select a qualifying
contract, and pass the proposed order to the model-visible
`validate_option_order` tool. It uses targeted reads only when preloaded state is
stale or insufficient.

`validate_option_order` is a read-only local tool rather than a contract selector.
It accepts the intended action, option symbol, quantity, and limit price; fetches
fresh metadata and a snapshot for that exact contract; and returns each policy
check, the normalized quote and contract evidence, total debit, loss at the 35%
premium backstop, buying-power state, daily-loss state, and precise rejection
reasons. It does not rank contracts, choose strikes, infer direction, or modify
the proposal.

A successful result authorizes only the exact action, symbol, quantity, and limit
price validated during that model turn. The model then returns its structured
decision. Before submission, the runtime writes a durable trade plan containing
the SPY thesis, entry, invalidation and target plus the selected option and its
premium exit parameters. Only then does it submit the exact validated order
through Alpaca MCP. A rejection is returned to the model so it may inspect
another candidate.

The normal entry sequence is:

1. form a SPY thesis from the preloaded auction context
2. inspect a narrowed option chain through Alpaca MCP
3. choose a contract and call `validate_option_order`
4. revise the candidate when validation returns a rejection
5. return the complete structured entry decision
6. persist its durable trade plan
7. submit the exact validated order through Alpaca MCP
8. reconcile the fill and submit a broker-native 35% premium-loss stop

The separately deployed position watcher then owns the active option between
reasoning ticks. It polls executable option bids and SPY, enforces the stored
structural target, moves the stop to break-even after +20%, and latches a profit
exit after +50%. It reads the durable trade plan but does not run a model, MCP
server, narrative generation, or contract selection.

Alpaca-reported orders and positions remain execution truth. Later ticks preload
the trade plan for the active option, so a model restart or malformed response
cannot erase the original thesis or exits.

## Minimal operational controls

The experiment remains confined to the dedicated competition paper account:

- `ALPACA_PAPER_TRADE` must be true.
- Order submission defaults to disabled and requires an explicit enable switch.
- A durable kill switch prevents new model turns and cancels working entries.
- The forced-close backstop begins fifteen minutes before the Alpaca-reported
  session close if a position remains open.
- Every new order uses a deterministic `client_order_id` derived from its decision
  ID and action.

These controls protect operation of the demo. They do not form a second trading
policy or approval layer around the model.

## Order lifecycle

Alpaca reads reconcile state at the start of every turn and after each
account-changing call. The runtime looks up a deterministic client order ID
before retrying a lost submission. Partial fills become the authoritative
position immediately, and a position is not flat until Alpaca says it is. A
durable lease prevents concurrent turns from managing the account.

When positioned, the reasoning runtime makes sure the initial protective sell
stop exists before invoking the model. The watcher continuously reconciles that
stop, break-even, profit, daily-loss, and session-close rules. The scheduled tick
retains premium-loss, daily-loss, and close checks as slower failure backstops. A
thesis-driven model exit must cancel the protective stop before liquidation.

## Token economy

Complete API responses remain outside model context. The runtime compacts their
current state into the turn context; targeted tools return small structured
results, while raw responses remain available in the audit store.

An ordinary hold should require no tool calls. Context includes at most twelve
recent five-minute bars and eight narrowed option candidates, not an unfiltered
chain or prior conversation transcript. The agent has no fixed model-round or
tool-call count limit. A 240-second evaluation deadline prevents a stalled turn
from overlapping the next scheduled tick indefinitely.

Option chain, quote, and snapshot calls are normalized to Alpaca's indicative
feed because the competition paper account does not include real-time OPRA data.

## Failure behavior

- A failed or stale Alpaca market read requires the agent to hold new entries.
- A failed account reconciliation prevents another account-changing tool call.
- A timeout after submission is resolved by client order ID and order lookup.
- A restart begins by reconciling Alpaca positions and working orders.
- The session-close backstop can flatten without waiting for another model turn.

## Deployment and scheduling

The system has two independently built containers: the private FastAPI reasoning
service and a small Cloud Run Job for position watching. It does not use Cloud
Functions. Cloud Scheduler invokes the HTTP
endpoints with an OIDC token from a service account that has only the Cloud Run
Invoker role. The service uses its own runtime identity for Firestore access.

Scheduler uses the `America/New_York` timezone. Its weekday schedules deliberately
cover a slightly wider window than the strategy. Every scheduled endpoint must
check the Alpaca calendar and clock before doing work, so holidays, early closes,
delayed requests, and manual invocations do not depend on cron being market-aware.

| Purpose | Schedule | Endpoint | Status |
| --- | --- | --- | --- |
| Daily narrative | 09:36 ET weekdays | `POST /narratives/generate` | Provisioned |
| Agent evaluation | Every five minutes during the RTH window | `POST /ticks/evaluate` | Enabled |
| Position watcher | 09:35 ET weekdays | Cloud Run Job execution | Enabled |
| Normal-session close backstop | 15:45 ET weekdays | `POST /positions/flatten` | Enabled |

The tick endpoint will acquire a Firestore lease keyed by trading date and
scheduled evaluation time before invoking the model. A retry or duplicate
Scheduler delivery therefore returns the existing decision instead of starting
a second turn. Narrative generation is similarly idempotent because its
Firestore document is keyed by trading date and created only once.

Each tick is self-contained: reconcile Alpaca account, order, position, clock,
and market state; load the narrative and durable session state; run the model;
persist the outcome; then end the request. The tick also compares the
Alpaca-reported close with the current time, so the forced-close path works on
early-close days. The separate 15:45 request is an additional normal-session
backstop.

The reasoning service does not keep Alpaca WebSockets alive between requests.
The watcher starts once each trading day, polls the active option quote and
reconciles broker state until the session ends. Its separate image copies only
`src/risk_watcher` and installs HTTP and Firestore runtime dependencies.

The Python service exposes an HTTP generation endpoint and listens on the
platform-provided `PORT`, making it suitable for Cloud Run. Firestore stores one
immutable narrative document per trading date and will later hold decisions,
order events, leases, and current runtime state.
