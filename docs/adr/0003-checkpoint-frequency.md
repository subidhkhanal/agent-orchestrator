# ADR 0003: Checkpoint every step; a checkpoint marker event every N steps (default 5)

- Status: accepted (M2)
- Date: 2026-09-28

## Context

Two different things are easy to confuse:
1. **Execution checkpoints**: what LangGraph saves so a run can resume after a crash.
2. **Checkpoint marker events** (`checkpoint` in `graph_events`): entries in the audit log /
   SSE stream saying "a durable point was reached", which the UI shows and which a coarse
   replay tool could use as starting points.

The frequency of each is a trade-off between write amplification (more writes per run) and
replay cost (how much work is redone after a crash).

## Options considered

- Execution checkpoints every step vs every N steps. With every N steps, a crash redoes up
  to N-1 completed nodes, each with LLM calls that cost money and may produce different
  output. With every step, at most the one in-flight node is redone.
- Marker events every step vs every N steps vs never.

## Decision

- **Execution checkpoints: every super-step**, written synchronously (`durability="sync"`).
  At our step granularity a step is an LLM-heavy node (seconds to a minute of work, cents of
  spend), so the cost of redoing work dwarfs the cost of one more row write. Measured locally:
  about 12 checkpoints per research-memo run, roughly 64 KB of checkpoint data per run with the
  fake LLM (real runs are larger because source snippets are in state).
- **Marker events: every N steps, N = 5 by default**, configurable per run through
  `checkpoint_policy.event_every_n` on the create-run request (0 disables them). They are
  cheap, but a marker on every step would double the length of the timeline without adding
  information; the UI already shows every node completion.

## Consequences

- Good: a crash costs at most one node's work. The chaos test shows it: interrupted runs
  resume from the node that was executing.
- Cost: write amplification. Every step writes checkpoint rows plus blob rows for changed
  channels. At our scale this is irrelevant; at large scale (see README, "Scaling to
  production") checkpoint writes/s is one of the numbers that drive the design, and the fixes
  are: a shallow checkpointer that keeps only the latest checkpoint per thread (LangGraph ships
  `ShallowPostgresSaver`), pruning history after completion, and moving large values out of
  state (we already keep artifact content out of state).
- If steps became tiny (many cheap tool nodes), the balance flips and every-N checkpoints
  would be the better default. That is why N is configuration, not a constant.
