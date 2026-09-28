# ADR 0002: Shared state with validated patches, plus an append-only event log

- Status: accepted (M1)
- Date: 2026-09-28

## Context

Four agents (supervisor, researcher, coder, reviewer) and a human work on one task. Each needs
to know what the others produced: sources, the current draft, review findings, the approval
decision, and how much budget is left. We need to be able to answer "why did the run do
that?" after the fact. A run can also be paused for a day and resumed on a different machine.

## Options considered

1. **Message passing.** Agents send each other messages (as in chat-style multi-agent
   frameworks). Each agent rebuilds its picture of the world from the conversation.
   - Simple to start with, and it looks natural for LLMs.
   - The "current truth" is implicit. To know which draft is current, or whether the human
     approved *this* version, you have to replay and interpret the whole conversation.
   - Hard to validate: a message can claim anything.
2. **Shared mutable state.** Agents read and write a shared object directly.
   - Easy to read. But any agent can write any field, and concurrent writers overwrite each
     other without noticing.
3. **Shared state changed only through validated patches, plus an event log** (chosen).

## Decision

- One schema, `RunState` (Pydantic, frozen), is the only source of truth for a run.
- Nodes never modify it. A node returns a `StatePatch`: typed ops (`set`, `append`,
  `upsert`) plus the `state_version` it was computed from.
- A single pure function, `apply_patch`, turns (state, patch) into the next state. It
  rejects the patch if:
  - `base_version` is not the current `state_version` (optimistic concurrency),
  - the author touches a field outside its write list (for example, the researcher cannot write
    `artifacts`, and no LLM-driven node can write `budget` or `termination`),
  - an artifact version does not advance by exactly one,
  - the result fails schema validation.
- Every applied patch, and every node start/finish, LLM call, tool call and routing decision,
  is appended to `graph_events` with a per-run monotonic `event_id` and an idempotency key.
  In Postgres, a trigger blocks UPDATE/DELETE on that table.
- Large content (memo text) lives in the `artifacts` table and is referenced by id.

## Consequences

- Good: the current truth is one object, so guards and prompts read the same facts.
- Good: the event log is a full audit trail and the replay source for SSE reconnects.
- Good: write permissions per role are enforced in one place (`WRITE_ACL` in the reducer).
- Cost: every node change goes through a patch, which is more code than mutating a dict.
- Cost: inside one LangGraph run nodes execute one at a time, so a stale patch cannot happen
  there. The version check matters once a second writer exists: a worker whose lease expired
  (milestone 2 uses `state_version` and the lease generation as a fencing token), or an API
  request racing a worker. The unit test proves the reducer refuses a stale patch; the
  Postgres-level test for zombie workers comes with the lease work in M2.
- Cost: the state is stored twice, once in the LangGraph checkpoint (so the graph can resume)
  and once as patch events (so humans can audit). The checkpoint is authoritative for resume;
  the log is authoritative for history.

## Amendment (M2, 2026-09-28): the durable version check

With workers, the version check is enforced in Postgres too: every patch commit is
`UPDATE graph_runs SET state = ..., state_version = new WHERE run_id = ... AND attempt = <my
lease generation> AND state_version = <base>`. A zero-row update means either another worker
owns the run now (`LeaseLost`) or the stored version moved (`StalePatchError`).

After a crash, the new worker first *rewinds* the stored state to the checkpoint it resumes
from: the dead worker may have committed a patch for the node that is about to re-execute.
The checkpoint is authoritative for execution; `graph_runs.state` is a cache for the API.
Events from the abandoned attempt stay in the log, tagged with their `attempt`, so the audit
trail shows both executions of the re-run node.
