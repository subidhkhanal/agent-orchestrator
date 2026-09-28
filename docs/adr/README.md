# Architecture Decision Records

Each ADR records one decision: the situation, the options we looked at, what we chose, and what
that costs us. Later learning is added as a dated amendment rather than rewriting history.

| ADR | Decision |
|---|---|
| [0001](0001-langgraph-postgres-lease-vs-temporal.md) | LangGraph + Postgres checkpointer + job lease instead of Temporal |
| [0002](0002-shared-state-and-event-log.md) | Shared state with validated patches plus an append-only event log |
| [0003](0003-checkpoint-frequency.md) | Checkpoint every step; checkpoint marker events every N steps |
| [0004](0004-code-enforced-invariants.md) | Safety invariants enforced in code, not prompts |
| [0005](0005-effect-ledger-and-exactly-once.md) | Effect ledger with idempotency keys, and the limits of exactly-once |
| [0006](0006-sse-vs-websocket.md) | SSE with Last-Event-ID replay instead of WebSockets |
| [0007](0007-model-tiering-and-budget-waterfall.md) | Model tier per node and a waterfall budget |
