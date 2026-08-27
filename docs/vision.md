# Product Vision

Status: Draft

## Vision

Build an autonomous SPY options agent that reasons and acts like a disciplined
auction trader.

The agent should turn live market evidence into a clear, falsifiable trade
thesis, express qualified theses through SPY options, and make every decision
understandable after the fact. It gathers evidence and executes through Alpaca
MCP directly inside a dedicated paper account.

## Hackathon Objective

The immediate objective is a credible entry for the Alpaca AI Trading Agents Hackathon that:

- uses Alpaca's Trading API and MCP server or CLI
- trades options in the dedicated competition paper account
- operates autonomously during its scheduled window
- demonstrates explicit AI reasoning and a visible risk policy
- produces a concise, inspectable record of every hold, entry, and exit
- is complete and reliable enough to accumulate meaningful paper-trading evidence during the event

The build window is 28 August–4 September 2026. Published judging emphasizes P&L and creativity or engagement, but the project will not pursue P&L by removing safety boundaries.

Source: [Alpaca's hackathon announcement](https://ir.linkedin.com/company/alpacamarkets)

## The Problem

Many trading agents fail in one of two ways:

1. They are opaque predictors that produce a direction without a falsifiable market thesis.
2. They hide the model behind so much orchestration that the resulting system is
   barely agentic.

Options make both problems more dangerous. A directionally correct idea can still lose because of poor contract selection, time decay, volatility, liquidity, or an unsuitable holding period.

This project gives the model genuine responsibility. It interprets market
structure, checks a concise trading policy, selects and sizes a contract, and
calls Alpaca MCP trading tools itself. The paper account makes that autonomy
appropriate for the hackathon.

## Product Thesis

Auction Market Theory provides a useful language for an AI trading agent because it frames decisions around observable market behavior rather than unconstrained prediction:

- Is the market balanced or seeking a new value area?
- Is price being accepted or rejected outside established value?
- Is value migrating with the move?
- Is the proposed entry well located relative to invalidation and target?
- Is the evidence strong enough to justify taking risk now?

SPY is the initial underlying because Alpaca supplies its equity and options surfaces, its options are highly liquid, and the analyzed market can be the same market that defines entry, invalidation, and target.

The initial options expression is intentionally narrow: buy a policy-sized
position in qualifying SPY calls for a bullish thesis, buy a policy-sized
position in qualifying SPY puts for a bearish thesis, close the existing
position, or hold.

## Product Experience

At each scheduled evaluation, the model receives identity, the compact AMT
mandate, policy boundaries, tool descriptions, and a timestamped turn context.
The context preloads common evidence without precomputing the decision. The agent
should behave as an evidence-seeking operator:

1. Review the preloaded clock, account, P&L, positions, daily plan, and live SPY
   auction.
2. Classify the auction and identify active references.
3. Decide whether a directional thesis exists at a defensible location.
4. Hold without a tool call when it does not.
5. Request targeted additional evidence when the state is ambiguous.
6. If a thesis exists, inspect the relevant option chain through Alpaca MCP and
   choose an exact contract and limit price.
7. Check the proposal against the market, liquidity, account, and risk mandate.
8. Call the relevant Alpaca MCP order tool directly.
9. Reconcile order and position state on each scheduled turn and manage the
   position against its underlying SPY thesis.
10. Record the evidence, risk check, tool calls, and outcome.

A user reviewing the system should be able to answer three questions quickly:

- What did the agent believe was happening in the auction?
- What evidence would prove that belief wrong?
- Why did policy allow or reject the proposed risk?

## Seeded Session Context

After the first five-minute bar closes, the runtime combines the most recent
completed SPY profile with the current opening gap and prepares a compact daily
narrative. That plan, the fixed AMT mandate, and the current live snapshot are
preloaded on each later turn; the product does not need a retrieval corpus or a
new narrative generation call on every evaluation.

The fixed mandate is:

> Treat trade as a two-sided auction. Inside established value indicates balance and normally requires patience. Acceptance outside value may indicate price discovery when value, participation, and structure support it. Rejection back into value invalidates continuation. Prefer clear structural location, a nearby invalidation, and a reachable reference over prediction. Hold when evidence is incomplete or contradictory.

Daily references are calculated from real Alpaca-sourced SPY data. The brief constrains the agent's reasoning; it does not precompute the decision.

## Decision Contract

Every agent turn must end with a structured record containing:

- auction state: balance, discovery up, discovery down, or unclear
- active references and their provenance
- acceptance or rejection evidence
- current range and initial-balance context
- proposed entry and structural invalidation
- reachable underlying target
- selected option and liquidity evidence, when applicable
- rejection reason, when holding or blocked
- final decision and confidence

The model may trade, but it may not assume a fill, invent a quote, or infer
account state from stale context.

## Agent Authority

### The agent owns

- auction interpretation
- evidence gathering through approved tools
- thesis formation
- entry-quality judgment
- contract selection and position sizing
- Alpaca MCP order submission, replacement, cancellation, and closing
- an explanation of holds, entries, and discretionary exits

The model directly uses selected Alpaca MCP read and write tools plus local
analysis helpers. Every write call is retained in the audit trace.

### The runtime owns

- paper-account verification and the order-submission enable switch
- market, order, and position state transport
- assembly of the timestamped turn context and daily narrative
- durable decisions, tool calls, orders, and fills
- idempotency and reconciliation after ambiguous responses
- the kill switch and forced session close

The trading policy is part of the agent's operating mandate, not a second order-
approval service. The MVP reconciles Alpaca state at the start of every scheduled
turn and after account-changing calls. Always-on event streams can be added later
if five-minute reconciliation proves insufficient.

## Product Principles

### Evidence before action

The agent should retrieve missing evidence or hold. Confidence is not a substitute for market, quote, position, or account data.

### Autonomy should be visible

The model should visibly retrieve evidence, make the decision, and call the
trading tool. Infrastructure supports the loop rather than impersonating the
agent.

### The underlying defines the thesis

Entries, invalidations, and targets are expressed in SPY market structure. Option premium is the vehicle, not the source of the market thesis.

### Holding is a successful decision

The system should prefer an explicit, well-supported hold over manufacturing activity for a demo or leaderboard.

### Every action must be replayable

Inputs, tool results, reasoning fields, policy decisions, orders, fills, and exits should form one auditable trace.

### Narrow scope creates reliability

One underlying, long premium, one open position, and a small action space are features of the first version, not shortcomings.

## MVP Scope

The first complete product includes:

- SPY regular-session market data sourced through Alpaca
- a small deterministic completed-session TPO/profile calculation surface
- scheduled agent evaluations on a five-minute cadence
- a preloaded, timestamped market and portfolio snapshot
- targeted tool-driven option-chain inspection
- structured AMT decisions
- SPY call and put discovery
- explicit option, liquidity, time, and account-risk checks
- limit-order submission to the competition paper account
- fill and position monitoring
- thesis-driven exits and a forced session close
- persistent decision and execution traces
- a compact operator view suitable for a live demonstration

## Explicit Non-Goals

The MVP will not include:

- ES or MES market data or execution
- a corpus, embeddings, or retrieval pipeline
- multiple underlyings
- naked short options
- 0DTE options
- autonomous rolls, exercise, or assignment workflows
- multi-agent debate without a measured benefit
- live-money trading
- claims of profitability based on a short paper-trading window

Bull-call and bear-put debit spreads are the first extension after the single-leg order and position lifecycle is reliable.

## Differentiation

The project is not a generic finance chatbot and not an LLM wrapped around a buy/sell tool. Its differentiators are:

- native AMT reasoning over live SPY structure
- an agent that receives common evidence and selectively retrieves what it needs
- direct model use of Alpaca MCP market and trading tools
- option selection tied to the expected underlying move and holding period
- a decision trace that explains both trades and rejected trades
- a paper-contained system that remains legible under uncertainty

## Success Criteria

### Submission success

- Meets all published competition requirements.
- Completes at least one end-to-end qualifying option lifecycle in paper trading.
- Demonstrates the Trading API and MCP server or CLI in the submitted product.
- Produces the required one-page explanation from implemented behavior.

### Product success

- Every scheduled turn produces a valid structured decision.
- Every order is preceded by a recorded risk check over fresh account and quote
  state.
- Duplicate turns cannot create duplicate positions.
- Restarting the runtime does not lose authoritative order or position state.
- Every entry has a recorded SPY invalidation, target, maximum risk, and session deadline.
- The forced-close path is tested before unattended operation.
- Holds and rejected proposals are as inspectable as executed trades.

### Evaluation success

- Paper fills and option marks use actual Alpaca responses rather than fabricated prices.
- Results distinguish realized P&L from open mark-to-market P&L.
- Performance reporting includes drawdown and risk taken, not only gross return.
- Replays preserve the information that was available at decision time.

## Primary Risks

- The competition data entitlement provides live IEX equity data and indicative option quotes rather than live consolidated SIP and OPRA coverage.
- Option Greeks may be absent, especially near expiration or for illiquid contracts.
- Paper fills may not represent live execution quality.
- SPY profile parameters copied from ES may create noisy or misleading structure.
- A one-week event provides too little evidence to validate profitability.
- Tool or network failure during an autonomous turn could leave stale assumptions unless the runtime fails closed.

These risks should be made visible in the product rather than hidden in the presentation.

## Beyond the Hackathon

If the core lifecycle proves reliable, the product can evolve toward:

- debit spreads selected from the same underlying thesis
- volatility-aware structure selection
- calibrated SPY day-type and similar-session models
- richer macro-event handling
- comparative evaluation of model interpretations
- additional liquid option underlyings
- supervised live deployment with stricter operational controls

The long-term product remains the same: an explainable market-interpretation agent whose freedom to reason is greater than its freedom to risk capital.
