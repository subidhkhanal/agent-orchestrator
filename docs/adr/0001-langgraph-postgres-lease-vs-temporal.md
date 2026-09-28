# ADR 0001: LangGraph + Postgres checkpointer + a Postgres job lease, instead of Temporal

- Status: accepted (M2)
- Date: 2026-09-28

## Context

Runs take minutes of LLM work and can wait a day for a human. Worker processes crash, get
redeployed, or get OOM-killed. A run must survive that and continue, and nothing may run twice
in a way the outside world can see (a memo published twice).

We need three things: durable state between steps, a way to hand a run to exactly one worker at
a time, and a way to take the run back when that worker dies.

## Options considered

1. **Temporal** (durable workflow engine).
   - Real strengths: event-sourced workflow history, deterministic replay, timers, retries
     and signals built in, battle-tested at large scale.
   - Costs here: another cluster to operate (or a paid cloud), a second programming model
     (workflow code must be deterministic; LLM calls become activities), and the LangGraph
     graph would have to be re-expressed as workflow + activities. For a single-developer
     project at this scale, the operational weight outweighs the benefit.
2. **A message queue** (Redis/RabbitMQ/SQS) plus our own state store.
   - A queue gives delivery, but not "who owns this run right now", and we would still need
     a durable state store and our own resume logic. Two systems to keep consistent.
3. **LangGraph's Postgres checkpointer + a lease on the `graph_runs` row** (chosen).

## Decision

- LangGraph saves a checkpoint after every super-step (`durability="sync"`, so the checkpoint
  is written before the next node starts). The run's `run_id` is the checkpoint `thread_id`.
- `graph_runs` is also the work queue. A worker claims a run with one statement:
  `SELECT ... FOR UPDATE SKIP LOCKED` on the oldest QUEUED run (or a RUNNING run whose lease
  expired), setting `lease_owner`, `lease_expires_at` and `attempt = attempt + 1`.
  `SKIP LOCKED` means concurrent workers never wait on or double-claim the same row.
- The worker heartbeats every few seconds to extend the lease. If it dies, the lease expires
  and another worker claims the run, loads the latest checkpoint and continues (`mode=recover`
  in the `run_claimed` event).
- `attempt` is a **fencing token**. Every write a worker makes for a run (state commits,
  events, final status) is conditional on its `attempt` still being current. A worker that was
  only *paused* (GC pause, network partition) and wakes up after losing its lease gets
  `LeaseLost` on its next write and stops.
- Human approval does not hold a lease: the run parks as `WAITING_HITL` and the lease is
  released. Approve/reject re-queues it with a `resume_payload`; any worker resumes it with
  `Command(resume=...)`.

## What "resume" means precisely

Checkpoints are taken *between* nodes. The node that was executing when the worker died runs
again from its start on the new worker. So:
- work finished before the crash is not redone (the crash test asserts the researcher ran
  exactly once when the crash hit during review);
- the interrupted node repeats, including its LLM calls (they cost tokens twice), which is
  why every side effect inside a node is idempotent (see ADR 0005).

## Consequences

- Good: one database for state, queue, events and approvals. Transactions span them, e.g.
  parking a run and announcing its approval gate is one commit.
- Good: measured, not claimed. `scripts/chaos.py` hard-kills worker processes mid-run (three
  seeds, 50 runs each): every run completed, with 0 duplicate external effects.
- Cost: the fencing token protects *our* tables, but the LangGraph checkpointer's writes are
  not fenced. A paused zombie worker could write one more checkpoint for a thread that another
  worker now owns before its next fenced write fails. The window is one node. Closing it would
  need a checkpointer wrapper that checks the lease inside the checkpoint transaction; that is
  listed under limitations.
- Cost: polling for work (every ~1 s) instead of push. Fine at this scale; `LISTEN/NOTIFY` on
  run creation would remove the poll latency.

## Migration path to Temporal

The seams are already where Temporal would need them:
- each node function becomes an activity (they already take a context and return a patch,
  with side effects behind idempotent interfaces);
- the supervisor's routing loop becomes the workflow function (the graph topology is data, so
  the workflow can interpret the same `GraphSpec`);
- approve/reject become signals; the HITL timeout becomes a workflow timer;
- `graph_events` stays as our audit log (Temporal's history is not a product-facing log).
Trigger to migrate: many thousands of concurrent long-waiting runs, cross-region failover, or
workflows long enough that Postgres checkpoint history becomes the bottleneck.
