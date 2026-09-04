# Augur — Autonomous SPY Options Agent

Augur is an autonomous trading agent that interprets the SPY auction, forms a
falsifiable directional thesis, expresses qualified ideas through long SPY
options, and publishes an audit of every decision. It operates only in a
dedicated Alpaca paper account.

**Live audit → [augur-506910.web.app](https://augur-506910.web.app)**

## What it does

Shortly after the open, Augur builds a daily Auction Market Theory narrative
from the previous SPY session. It calculates the prior session's TPO profile,
point of control, value area, range, and initial balance from Alpaca market data,
then combines those references with the current opening gap and first completed
five-minute bar.

From 09:40 to 15:55 ET, the agent evaluates the market every five minutes. Each
turn begins with a fresh, timestamped snapshot of the account, positions,
orders, P&L, recent SPY bars, daily narrative, and remaining risk. The model must
classify the auction as balance, upward discovery, downward discovery, or
unclear. A trade requires completed-bar evidence of acceptance or rejection at
a named reference, an entry stated in SPY terms, a structural invalidation, and
a reachable target. If the evidence is absent or contradictory, holding is the
intended outcome.

## How Augur uses Alpaca MCP

Alpaca MCP is the agent's market-data and trading interface. The official
`alpaca-mcp-server` runs over stdio as a local subprocess inside the same
Cloud Run container as the reasoning service. There is no separately hosted
MCP endpoint. The runtime starts the server with the paper-account credentials,
retrieves its tool definitions, and exposes only a narrow allowlist to the model.

The model can use eight Alpaca MCP read tools to inspect option chains, exact
contracts, quotes, snapshots, positions, and orders. It can use three write
tools to cancel or replace an order and close a position. Option-data calls are
forced onto Alpaca's indicative feed, and every MCP call is captured with its
arguments, result, timestamp, and whether it was blocked.

Common account and SPY state takes a separate deterministic path through
Alpaca's REST APIs. It is preloaded so the model cannot omit a buying-power,
position, order, or loss check by simply choosing not to request it. MCP remains
available for the targeted evidence that is relevant only after the model has a
directional thesis, especially the option chain and exact contract snapshot.

An entry follows a deliberately constrained MCP workflow:

1. The model forms a SPY thesis before requesting any option data.
2. It narrows the option chain through Alpaca MCP and selects a contract,
   quantity, and limit price.
3. It calls the local `validate_option_order` tool, which refreshes that exact
   contract through Alpaca MCP and checks expiry, delta, quote freshness,
   spread, buying power, and daily-loss headroom.
4. The model returns a structured decision matching the successful validation.
5. The runtime persists the trade plan, then calls Alpaca MCP's
   `place_option_order` with that exact symbol, quantity, and price.
6. Alpaca-reported orders, fills, and positions are reconciled as execution
   truth; submission is never treated as a fill.

The model cannot call the entry tool with an unvalidated payload. Separating
selection from dispatch preserves meaningful agent authority—the model owns the
thesis, contract, size, and price—while preventing a malformed response or retry
from changing the authorized order. For thesis-driven exits, cancellations, and
replacements, the model calls the allowlisted Alpaca MCP write tools directly.

## Risk and operation

Every proposal is checked again after the model produces it. The initial policy
is intentionally narrow:

| Control | Value |
| --- | --- |
| Underlying and structure | One long SPY call or put position |
| Entry size | 1–20 contracts, agent-sized by conviction |
| Expiry and delta | 1–5 DTE; 0.55–0.65 absolute delta |
| Liquidity | Quote ≤5 seconds old; spread ≤$0.15 and ≤5% |
| Daily loss limit | 10% of session-starting equity |
| Position protection | −35% stop; break-even at +20%; exit at +50% |

A separate non-model watcher runs throughout the session. It reconciles Alpaca
state, maintains the broker-native stop, moves it to break-even, enforces the
profit and structural targets, and closes risk before the session ends. The
five-minute service independently retains daily-loss and close backstops.

Excluded by design: 0DTE contracts, short premium, multiple underlyings,
autonomous rolls, and live-money trading.

## Why it matters

Augur's product is not merely an order; it is a replayable chain of evidence and
action. Every completed evaluation is stored in Firestore with the context the
model saw, its structured decision, validation results, MCP calls, orders, and
fills. The public audit turns those records into a daily narrative and timeline,
making three questions easy to answer:

1. What did the agent believe was happening?
2. What evidence would have proved it wrong?
3. Why did the system allow, reject, or close the risk?

DeepSeek V4 Pro generates the daily narrative and DeepSeek V4 Flash performs the
intraday evaluations through Featherless. Alpaca is authoritative for all
market, account, order, fill, and position state. Augur does not claim that a
short paper-trading window proves profitability; it demonstrates a complete,
inspectable autonomous options lifecycle built around Alpaca MCP.

Technical detail: [architecture](docs/architecture.md),
[strategy](docs/strategy.md), [policy](docs/policy.md),
[discovery](docs/discovery.md), and [deployment](deploy/README.md).
