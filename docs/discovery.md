# Discovery Findings

Status: Core data paths verified

Last updated: 27 August 2026

## Verified account state

Read-only checks against the dedicated Alpaca paper account confirmed:

- account status: active
- starting cash and equity: $100,000
- options buying power: $100,000
- options approval and trading level: Level 3
- trading, account, and user-suspension blocks: none
- SPY: active, tradable, and optionable

## Verified equity data

Recent SPY snapshots work with the IEX feed. Recent SIP requests return `403` because the current subscription does not permit recent SIP data.

Historical SIP data outside the subscription's recent-data restriction does work. A completed prior SPY regular session returned the full minute-bar sequence and consolidated volume. This lets us build prior-session profile context from SIP without paying for Algo Trader Plus.

Implication:

- use historical SIP bars for completed-session profile and plan references
- use live IEX bars for current price, session range, initial balance, opening behavior, and five-minute structure
- do not use IEX volume as though it represented consolidated market participation

## Verified options data

The paper account has Level 3 multi-leg permission.

The free indicative SPY option-chain request succeeded. In the first 1,000 returned contracts:

- 827 had two-sided quotes
- 411 had Greeks and implied volatility
- another page of contracts was available

The feed was checked again after the options market opened. At 09:34 ET, a chain request covering 760–776 strikes for the next day's expiry returned:

- 34 contracts, all with fresh two-sided quotes
- Greeks and implied volatility on all 34 contracts
- quote timestamps less than two seconds old
- a $2.57 × $2.59 quote for the 768 call and a $2.86 × $2.89 quote for the 768 put
- near-the-money relative spreads of roughly 0.5–1.5% for most sampled contracts

At 09:33 ET, the IEX SPY snapshot was also current to the second: last trade $768.03 and quote $768.00 × $768.08. This is enough for the paper MVP's live evidence and option-selection path. It is one observed session, so the agent must still check freshness and spread thresholds on every decision.

A real-time OPRA request returned `403`. Alpaca's error says the OPRA agreement is unsigned, but Alpaca support documents that this message generally means the account lacks the Algo Trader Plus subscription. The free indicative feed is intended for development and paper testing, not live-money execution.

Sources: [Alpaca data plans](https://docs.alpaca.markets/us/docs/about-market-data-api), [OPRA error explanation](https://forum.alpaca.markets/t/error-opra-agreement-is-not-signed/18445)

## Augur comparison

Current Augur does not put a developing TPO distribution, live POC, VAH, or VAL into every trading tick.

Its live snapshot contains:

- current price
- session high and low
- initial balance and extensions
- opening classification and opening context
- recent regular-session bars

Completed-session TPO/profile structure primarily informs the daily narrative and reference levels. The live agent interprets current bars and simple auction state against that plan.

The options agent can follow the same shape. It does not need a live consolidated volume profile:

```text
completed SPY session from historical SIP
  -> prior-session profile and seeded plan

live SPY IEX bars
  -> lean snapshot and five-minute evidence

account, P&L, positions, orders, plan, and live snapshot
  -> preloaded turn context

agent
  -> interpret the current auction against the plan
  -> inspect option chains through MCP only for a qualified thesis
```

## Current direction

The data subscription is sufficient for a paper-hackathon MVP without the $99 monthly upgrade.

The selected design is:

- SPY-native prior-session AMT plan
- one Augur-style narrative generated from that session plus the current opening gap
- preloaded account, P&L, daily-plan, and live-auction context
- agent-owned interpretation and directional thesis
- targeted option-chain inspection through Alpaca MCP
- explicit option eligibility and risk policy
- model-visible Alpaca MCP order tools
- policy-sized long SPY calls or puts through Alpaca paper trading
- Alpaca-reported orders, fills, and positions as execution truth

Indicative quotes may not match the prices used by Alpaca's paper execution simulator. Option limit-price behavior therefore remains a risk even though the post-open chain is fresh and reasonably tight.

## Verified paper order lifecycle

A one-contract SPY option lifecycle was completed in the competition paper account while the market was open:

- the account began with no positions or working orders
- a deliberately non-marketable $0.01 buy limit was accepted and then canceled with zero fills
- resubmitting the same `client_order_id` was rejected with HTTP 422, confirming Alpaca's duplicate-ID protection
- one next-day SPY 766 call was opened with a marketable limit and filled at $5.03
- Alpaca's close-position endpoint submitted a market sell and filled at $4.48
- Alpaca's fill activity records contain both executions
- the account ended with no positions and no working orders

The round trip reduced paper equity from $100,000 to $99,944.95. The $55 option-price loss came from the intentional gap between the interactive entry and exit approvals, not observed Alpaca execution latency. In the autonomous runtime, order submission, fill polling, position monitoring, and emergency close must be one uninterrupted workflow.

The test confirms that account state must remain authoritative: a submitted order cannot be treated as filled until Alpaca reports it, and an exit cannot be treated as complete until both the order and resulting position state reconcile.

## Verified AI Gateway

Cloudflare AI Gateway Unified Billing works with the configured account and token. No separate model-provider key is required.

Ad hoc calls through Cloudflare's OpenAI-compatible endpoint confirmed:

- successful third-party model inference
- function/tool-call output
- strict JSON Schema output
- routing through the configured AI Gateway

The probes used `openai/gpt-4.1-mini` only to validate the interface. Claude Sonnet is the selected production reasoning model.

## Verified Alpaca MCP

The official `alpaca-mcp-server` package is currently version 2.3.0. With the initial account, trading, assets, stock-data, and options-data toolsets enabled, it exposes 53 tools.

Verified locally:

- the server constructs successfully with the competition credentials
- a read-only `get_clock` tool call succeeds end to end
- tool results include an explicit untrusted-data trust boundary
- `get_stock_bars` exposes an explicit IEX/SIP feed parameter
- `get_option_chain` exposes indicative/OPRA and chain-narrowing parameters
- `place_option_order` supports single-leg and multi-leg orders
- multi-leg orders support a caller-provided `client_order_id` for idempotency
- order lookup, cancellation, replacement, position reads, and position closing are exposed

This supports a simple runtime architecture: a Python service launches Alpaca MCP over local stdio, exposes a deliberately narrowed subset of its tools to the model, and calls the model through Cloudflare AI Gateway.

## Remaining verification

- marketable limit exits and stale-order replacement behavior
- multi-contract single-leg submission, partial fills, cancellation, and closing
- multi-leg debit-spread submission and cancellation
- restart recovery from working-order and open-position states
- competition leaderboard accounting and intervention rules
