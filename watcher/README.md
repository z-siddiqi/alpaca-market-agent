# Position watcher

This is the small, separately deployed process that manages filled SPY option
positions. It does not run an HTTP server, call an LLM, launch MCP, generate
narratives, or select contracts.

Cloud Scheduler starts one Cloud Run Job before the strategy entry window. The
process reconciles Alpaca until the closing window and then exits. Its only
durable input is the `trade_plans` document written before an entry is submitted.

Responsibilities:

- keep the broker-native premium-loss stop present
- move that stop to break-even after the configured premium gain
- submit the configured premium-profit exit using an executable option bid
- continue managing a working exit until flat
- honor the account daily-loss limit and session-close backstop
- use a Firestore lease to prevent overlapping job executions

The existing reasoning service remains independently deployable from the root
`Dockerfile`. This directory is the build context for the watcher image.
