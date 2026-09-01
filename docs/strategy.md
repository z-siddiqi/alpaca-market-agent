# Strategy

Status: Draft

## Purpose

This document is the normative specification for the market interpretation the
agent performs. It defines the evidence available to the model, the Auction
Market Theory (AMT) concepts used by the MVP, and the decision the model must
produce. Option and account rules are defined in
[Policy](policy.md).

The strategy does not predict an unconstrained price direction. It looks for a
falsifiable auction thesis with a clear location, an underlying invalidation, and
a reachable underlying target. Incomplete or contradictory evidence resolves to
a hold.

## Strategy mandate

Every agent turn begins with this fixed brief:

> Treat trade as a two-sided auction. Inside established value indicates balance
> and normally requires patience. Acceptance outside value may indicate price
> discovery when structure supports it. Rejection back into value invalidates
> continuation. Prefer clear structural location, a nearby invalidation, and a
> reachable reference over prediction. Hold when evidence is incomplete or
> contradictory.

The brief constrains interpretation. Each turn also receives the compact daily
narrative, calculated references, live snapshot, and account state assembled by
the runtime. These preload evidence without precomputing a market decision or
encoding a mandatory sequence of tool calls.

## Market and session

- The underlying is SPY.
- The regular session is 09:30–16:00 America/New_York on Alpaca trading days.
- Premarket is separate context and is not included in the regular-session
  profile.
- The prior session means the most recent completed regular trading session, not
  the previous calendar day.
- Historical SIP minute bars supply the completed prior-session profile.
- Live IEX data supplies current price, range, initial balance, opening behavior,
  and completed five-minute bars.
- IEX volume must not be described as consolidated participation.
- The MVP does not calculate a developing live TPO or volume profile.
- The prior-session profile and compact daily narrative are prepared once after
  the first five-minute bar and included in each later turn context.

All references carry their source session, source feed, calculation version, and
timestamp. Missing or incomplete prior-session data makes profile-derived
references unavailable rather than silently substituting another feed.

## Prior-session profile

The completed-session profile uses 30-minute TPO periods and a fixed $0.10 row
size.

For each regular-session minute bar:

1. Assign the bar to its containing 30-minute period, starting at 09:30 ET.
2. Map its low and high to inclusive $0.10 price rows.
3. Mark each touched row once for that period, regardless of how many minute bars
   touched it.
4. Count the number of distinct periods assigned to every row.

Prices are represented as integer cents during calculation. A row is identified
by its lower price boundary. The session must contain the expected regular-session
minute range for that trading calendar; shortened sessions use the exchange close
reported by Alpaca.

The profile exposes:

- prior regular-session high, low, open, and close
- point of control (POC)
- value-area high (VAH) and value-area low (VAL)
- session range and midpoint
- profile completeness and source metadata

The POC is the row with the greatest TPO count. A tie is resolved by choosing the
row closest to the session midpoint, then the lower row if a tie remains.

The value area begins at the POC and expands until it contains at least 70% of all
TPOs. At each step, compare the next unselected row above and below the selected
area and add the side with the greater TPO count. Add both when the counts tie.
Stop after the first expansion that reaches the threshold. VAL and VAH are the
outer selected row boundaries.

## Daily narrative

The narrative preserves Augur's existing product shape rather than introducing
a separate planning vocabulary. It contains:

- `Contextual Analysis & Plan`: two or three short paragraphs describing the
  prior auction and the relevance of the current opening gap
- `Levels of Interest`: two concise conditional level bullets
- a structured `balanced`, `above_structure`, or `below_structure` level map

The source perception contains the prior session's period OHLC, initial balance,
extensions, POC, VAH, VAL, high, low, open, close, and closing location. Current-
session input is limited to the IEX opening print, its gap from the historical SIP
prior close, gap classification, location relative to prior range and value, and
the first completed five-minute IEX bar. The cross-feed provenance remains
explicit. The generator does not receive older sessions, retrieved examples, or
a current developing profile.

Narrative levels must be selected verbatim from the calculated prior-session
references. A true gap beyond the prior range may legitimately have no clean
continuation target. The narrative supplies context; it is not a trade signal and
does not replace the live acceptance or rejection requirements.

## Live references

The live-state tool exposes only deterministic observations:

- regular-session open, high, low, and latest price
- premarket high and low, when available
- initial-balance high and low
- upward and downward initial-balance extensions
- recent completed five-minute bars
- five-minute ATR14, when enough completed bars exist
- feed timestamps, connection state, and freshness

The initial balance is the regular-session range from 09:30 through 10:29:59 ET.
Before 10:30 it is explicitly marked as developing. The agent may use opening
behavior before the initial balance completes, but it may not describe a
developing range as a completed initial balance.

## Gap and extension classifications

The absolute gap from the prior regular-session close is classified as:

| Gap | Classification |
| --- | --- |
| Below 0.15% | Normal |
| 0.15% to below 0.35% | Elevated |
| 0.35% to below 0.70% | Large |
| At least 0.70% | Extreme |

A true gap is structural rather than percentage-based: the regular-session open
is above the prior regular-session high or below the prior regular-session low.

Initial-balance extension is measured as a percentage of the completed initial-
balance range:

- below 10% is noise
- at least 30% is meaningful directional extension
- at least 100%, with opposing extension below 20%, is a trend candidate
- extension of at least 25% on both sides is neutral behavior

These labels are evidence for the model. None creates a trade by itself.

## Acceptance and rejection

The minimum meaningful probe through a reference is:

```text
max($0.10, 0.25 × five-minute ATR14)
```

When ATR14 is unavailable, the minimum probe is $0.10 and the decision records
that the volatility-adjusted threshold was unavailable.

Acceptance above a reference requires a completed five-minute close at least one
meaningful probe beyond it and one of the following:

- the next completed bar also closes above the reference without reclaiming it
- a later completed bar extends the move by another meaningful probe before any
  completed close returns through the reference

Acceptance below a reference is the symmetric condition.

Rejection above a reference requires a bar to probe at least the meaningful
distance above it and close back below it. Confirmation then requires either:

- the next completed bar to remain below the reference without extending the
  rejected high
- at least two of the next three completed bars to close below the reference
  while their combined range remains no greater than one ATR14

Rejection below a reference is the symmetric condition. A probe without a
responsive close is unresolved, not rejection. A single close through a reference
that immediately fails to hold or expand is not acceptance.

## Auction state

Every turn classifies the current auction as exactly one of:

- `balance`: price is trading within established value or showing confirmed
  responsive rejection at its boundary without directional acceptance
- `discovery_up`: price has confirmed acceptance above the active balance or
  reference and supporting structure is extending upward
- `discovery_down`: the symmetric downward condition
- `unclear`: evidence is missing, contradictory, transitional, or does not meet
  the definitions above

The model must identify the active reference behind the classification. Location
relative to a reference is not enough; the decision must cite completed-bar
evidence of acceptance or rejection.

## Entry thesis

A bullish candidate may arise from confirmed upward acceptance or confirmed
rejection of a lower reference. A bearish candidate may arise from confirmed
downward acceptance or confirmed rejection of an upper reference.

Every candidate requires:

- an active structural reference with provenance
- completed-bar acceptance or rejection evidence
- an entry location stated in SPY price terms
- a structural invalidation stated in SPY price terms
- a target at another reachable SPY reference
- underlying target distance at least equal to the invalidation distance
- no contradictory acceptance or rejection signal of equal or greater recency

The agent does not inspect an option chain until a directional candidate exists.
Contract selection is an expression of the underlying thesis. The agent narrows
the chain through Alpaca MCP using the trading policy before choosing a contract.

## Position management

The recorded SPY thesis remains authoritative after entry and is preloaded with
the active option position. The position may exit when:

- a completed five-minute bar crosses the structural invalidation
- price reaches the recorded structural target
- new acceptance or rejection evidence materially contradicts the thesis
- the option premium reaches its 50% profit target
- the trading policy or session-close backstop requires liquidation

Option premium movement does not redefine the market thesis, but it does manage
the instrument actually held. The broker stop begins 35% below filled premium,
moves to break-even after a 20% premium gain, and never loosens. The plan is
stored before order submission so these levels survive a failed model response.

## Decision contract

Every model turn returns one structured decision with:

- decision ID and evaluation timestamp
- final action: `hold`, `buy_call`, `buy_put`, or `close_position`
- auction state and confidence
- active references and provenance
- completed-bar acceptance or rejection evidence
- current range and initial-balance context
- entry, invalidation, and target in SPY price terms, when applicable
- selected option and quote evidence, when applicable
- policy result, when a proposal was evaluated
- hold or rejection reasons

Confidence must be at least 0.50 for an entry. It does not change position size or
substitute for another policy requirement. The runtime records the
decision and the evidence available at that time automatically; persistence does
not depend on the model making a separate recording call.

Common hold reasons include `missing_data`, `stale_data`, `unclear_auction`,
`inside_balance`, `unconfirmed_probe`, `contradictory_evidence`,
`poor_location`, `insufficient_reward_to_risk`, `no_eligible_contract`, and
`policy_blocked`.
