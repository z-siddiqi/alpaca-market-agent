# Trading Policy

Status: Draft

## Purpose

This is the operating mandate the agent checks before using Alpaca MCP trading
tools. It keeps risk decisions consistent and visible without putting a second
approval service between the model and the paper account.

The model must use current evidence, show its policy check in the decision record,
and call Alpaca MCP itself. Common account and market evidence arrives in the
timestamped turn context; the model retrieves targeted state when that context is
stale or insufficient. Missing, stale, or contradictory evidence means hold or
reduce risk.

## MVP limits

| Control | Initial value |
| --- | --- |
| Account mode | Competition paper account only |
| Underlying | SPY only |
| Structures | Long call or long put |
| Quantity | Whole contracts within the calculated maximum |
| Concurrent positions | One |
| Concurrent working entries | One |
| Days to expiration | 1–5 calendar DTE |
| Target absolute delta | 0.55–0.65 |
| Minimum model confidence | 0.50 |
| Minimum underlying reward:risk | 1R |
| Maximum premium allocation | 5% of session-starting equity |
| Maximum planned loss per trade | 2% of session-starting equity |
| Daily equity-loss limit | 5% of session-starting equity |
| Daily realized profit stop | 5% of session-starting equity |
| Premium-loss circuit breaker | 35% below average filled premium |
| Entry delay | 10 minutes after the open |
| Post-close cooldown | 10 minutes |
| Forced flatten | 15 minutes before the close |

The timing, cooldown, confidence, reward, profit-stop, and macro-event controls
follow the current Augur agent. Futures tick limits do not transfer cleanly to
long options, so allocation and loss limits use account-equity percentages.

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
- no protected red-impact macro window or kill switch is active
- account equity is above the daily loss floor
- realized session P&L is below the daily profit stop
- the intended quantity is within allocation, loss, and buying-power budgets

The macro calendar is loaded before the session. A non-cancelled red-impact event
that applies to SPY, ES, or the broad US equity market blocks entries for its
configured pre- and post-event window.

## Contract selection

The model inspects a narrow chain only after a directional thesis exists. It
filters contracts using the rules above, then prefers:

1. absolute delta closest to 0.60
2. narrower relative spread
3. earlier expiration

Missing Greeks make a contract unsuitable for the MVP. The model does not replace
delta with a moneyness guess.

## Position sizing

Five percent is the maximum premium allocation, not the intended loss. A local
calculator returns the maximum whole-contract quantity:

```text
contract_debit = entry_limit × 100
allocation_budget = session_starting_equity × 0.05
planned_loss_per_contract = contract_debit × 0.35
planned_loss_budget = session_starting_equity × 0.02
daily_loss_floor = session_starting_equity × 0.95
remaining_daily_loss_budget = current_equity - daily_loss_floor

maximum_quantity = floor(min(
  allocation_budget / contract_debit,
  planned_loss_budget / planned_loss_per_contract,
  remaining_daily_loss_budget / planned_loss_per_contract,
  options_buying_power / contract_debit
))
```

The agent may choose any positive whole-contract quantity up to that maximum and
records both planned loss and full-debit worst-case loss. At $100,000 of session-
starting equity, maximum allocation is $5,000, planned-loss budget is $2,000, and
the daily loss floor is $95,000. A fully allocated position reaching the 35%
circuit breaker has a planned loss of $1,750.

The daily loss floor includes realized and unrealized P&L. Reaching it means no
new entries and an immediate exit attempt. The realized profit stop likewise ends
new entries for the session. There is no daily trade-count cap or competition-
wide drawdown stop.

## Entry orders

- Use limit orders only.
- Start at the current midpoint, rounded to a valid price increment.
- Refresh the option quote and maximum quantity immediately before submission;
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

The SPY thesis remains primary. The agent exits when the underlying invalidates
the thesis, reaches the target, or develops newer contradictory structure.

The premium circuit breaker is a secondary backstop. A fresh executable bid at
or below 65% of average filled premium calls for an exit, although gaps and fill
quality can produce a larger loss.

Normal exits begin with a limit at midpoint. After five seconds, the agent may
replace it with a marketable limit at the refreshed bid and continues checking
until Alpaca reports every contract flat.

Exits are not blocked by entry-only rules such as the macro window, cooldown, or
daily stops. The runtime begins forced liquidation fifteen minutes before the
Alpaca-reported close and may use a market order when remaining open is the
greater risk.

## Direct tool responsibility

The model can call Alpaca MCP tools to place, replace, cancel, and close orders.
Before each account-changing call it checks the latest reconciled order,
position, and quote state, using a targeted MCP read when any value is stale or
ambiguous. It never:

- uses a market order to enter
- increases quantity during replacement
- submits another entry while positioned or while an entry is working
- closes more contracts than the account currently holds
- retries an ambiguous submission under a new client order ID
- intentionally holds a position after the regular session or into expiration

Every attempted write call, Alpaca response, order event, fill, and resulting
position is retained in the audit trace.
