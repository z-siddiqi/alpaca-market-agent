# Trading Policy

## Purpose

This is the operating mandate applied before the runtime submits a validated
entry or the agent uses an Alpaca MCP trading tool. It keeps risk decisions
consistent and visible.

The model must use current evidence, show its policy check in the decision record,
and choose the exact entry proposal. Common account and market evidence arrives in the
timestamped turn context; the model retrieves targeted state when that context is
stale or insufficient. Missing, stale, or contradictory evidence means hold or
cancel the entry.

## Competition limits

| Control | Value |
| --- | --- |
| Account mode | Competition paper account only |
| Underlying | SPY only |
| Structures | Long call or long put |
| Quantity | 1–20 contracts, sized by model conviction |
| Concurrent positions | One |
| Concurrent working entries | One |
| Days to expiration | 1–5 calendar DTE |
| Target absolute delta | 0.55–0.65 |
| Minimum model confidence | 0.50 |
| Minimum underlying reward:risk | 1R |
| Daily equity-loss limit | 10% of session-starting equity |
| Daily realized profit stop | None |
| Daily trade-count limit | None |
| Premium-loss circuit breaker | 35% below average filled premium |
| Premium break-even trigger | 20% above average filled premium |
| Premium profit target | 50% above average filled premium |
| Entry delay | 10 minutes after the open |
| Post-close cooldown | 10 minutes |
| Forced flatten | 15 minutes before the close |

The daily equity-loss limit is 10%, and profitable sessions remain uncapped. The
10-minute cooldown gives the five-minute loop two newly completed bars before
another entry can be considered.

On a normal SPY session, entries begin at 09:40 ET and end before 15:45 ET.
Shortened sessions use the open and close reported by Alpaca.

## Before every entry

The agent checks the current clock, account, positions, working orders, underlying
state, selected contract, and quote. The first five arrive in the preloaded turn
context; contract and quote evidence comes from the targeted Alpaca MCP chain
request. It records that:

- the account is the dedicated competition paper account
- the market is open and inside the entry window
- no position or working entry already exists
- ten minutes have elapsed since the last position closed
- the latest SPY trade or quote is no more than ten seconds old
- the most recent expected five-minute bar is complete
- the option quote is no more than five seconds old, two-sided, and not crossed
- quote width is no more than $0.15 and no more than 5% of midpoint
- the contract is active, tradable, 1–5 DTE, and not expiring today
- absolute delta is 0.55–0.65
- the SPY thesis has an invalidation, target, and at least 1R reward:risk
- confidence is at least 0.50
- account equity is above the daily loss floor
- the intended quantity is between 1 and 20 contracts and fits options buying power

## Contract selection

The model inspects a narrow chain only after a directional thesis exists. It
filters contracts using the rules above, then prefers:

1. absolute delta closest to 0.60
2. narrower relative spread
3. earlier expiration

Missing Greeks make a contract unsuitable for the initial strategy. The model
does not replace delta with a moneyness guess.

The model owns contract ranking and selection. Before submission, it passes the
proposed action, symbol, quantity, and limit price to the model-visible
`validate_option_order` tool. The tool refreshes metadata and a snapshot for that
exact symbol and returns:

- active and tradable status, right, expiration, DTE, and delta
- bid, ask, midpoint, spread, relative spread, and quote timestamp
- limit-price range and SPY penny-increment validity
- total debit and loss at the 35% premium circuit breaker
- options buying power and remaining daily-loss headroom
- a pass or failure for every applicable policy rule
- precise reasons for any rejection

The validator does not score the chain, recommend a contract, or change the
proposal. If it rejects a candidate, the model may inspect another contract and
try again within the same turn.

## Position sizing

The model chooses between 1 and 20 contracts based on conviction; stronger
evidence is required for a larger position. The selected contract's premium
determines the debit and therefore the dollars at risk. Before submission, the
agent records the total debit, the loss implied by the 35% premium circuit
breaker, and the full-debit worst case. The chosen size must fit current options
buying power.

The daily loss floor includes realized and unrealized P&L. Reaching it means no
new entries and an immediate exit attempt. For a session starting at $100,000,
the floor is $90,000. Closed-position capital can be reused for later entries;
the 10% floor is not a ten-trade allocation. There is no daily profit stop, daily
trade-count cap, or competition-wide drawdown stop.

## Entry orders

- Use limit orders only.
- Start at the current midpoint, rounded to a valid price increment.
- Refresh the option quote and confirm that the selected quantity fits buying power
  immediately before submission;
  refresh account or position state when its context timestamp is stale or a
  lifecycle event makes it ambiguous.
- Give every order a deterministic client order ID derived from the decision ID.
- Let an entry rest for at most 30 seconds.
- After ten seconds, the agent may re-read the quote and replace once, no higher
  than the current ask and without increasing quantity.
- Cancel when the quote becomes stale or crossed, the thesis expires, an entry
  rule becomes false, or the rest window ends.
- Never assume submission means fill.

For multiple contracts, a partial fill immediately becomes the open position.
The agent may manage only the original remainder and does not create a new order
to top up after canceling it.

## Position management and exits

The stored SPY thesis remains the reason for the trade. The option position has
its own money-management exits: a 35% maximum premium loss and a 50% premium
profit target. The agent may also exit earlier when SPY invalidates the thesis,
reaches its structural objective, or develops newer contradictory structure.

After the entry lifecycle settles, the reasoning service submits a broker-native
sell stop for the filled quantity at 65% of average filled premium. A separate
position watcher polls the executable option bid and SPY throughout the session.
It closes the option when SPY reaches the stored structural target.
Once the option bid reaches a 20% gain, it replaces the stop at average filled
premium and never loosens it. Once the bid reaches 50%, it latches the exit,
cancels the stop, and works a sell limit at the current bid until flat. Alpaca owns
the stop trigger, so gaps and fill quality can still produce a worse fill. The
five-minute tick retains the premium-loss threshold as a slower fallback.

Normal exits begin with a limit at midpoint. After five seconds, the agent may
replace it with a marketable limit at the refreshed bid and continues checking
until Alpaca reports every contract flat.

Exits are not blocked by entry-only rules such as cooldown or daily stops. The
runtime begins forced liquidation fifteen minutes before the
Alpaca-reported close and may use a market order when remaining open is the
greater risk.

## Direct tool responsibility

The model uses Alpaca MCP to inspect the chain, select a contract, validate the
exact order, and manage thesis-driven closes and cancellations. A successful
entry decision is written to `trade_plans` before the runtime submits its exact
validated order through Alpaca MCP. Broker protection, premium profit-taking,
the daily-loss exit, and the session-close exit are runtime responsibilities and
execute before a model call.
Before each account-changing call it checks the latest reconciled order,
position, and quote state, using a targeted MCP read when any value is stale or
ambiguous. A new entry must exactly match a successful
`validate_option_order` result from the same turn. Its SPY thesis, entry,
invalidation, target, contract, and option exit parameters are durable before
submission. Validation does not carry into later turns, and changing the action,
symbol, quantity, or limit price requires a new validation call. It never:

- uses a market order to enter
- increases quantity during replacement
- submits another entry while positioned or while an entry is working
- closes more contracts than the account currently holds
- retries an ambiguous submission under a new client order ID
- intentionally holds a position after the regular session or into expiration

Every attempted write call, Alpaca response, order event, fill, and resulting
position is retained in the audit trace.
