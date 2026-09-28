# ADR 0005: Effect ledger with idempotency keys, and the limits of "exactly once"

- Status: accepted (M2)
- Date: 2026-09-28

## Context

`publish` is the one node with an external side effect: it posts the approved memo to a
downstream system. Two facts make duplicates likely unless we design against them:
- a node that was executing when its worker died **re-executes** after resume (ADR 0001);
- humans double-click, and HTTP clients retry.

## The honest version of exactly-once

A process can crash after the downstream system accepted a request but before the process
recorded that it did. No amount of local bookkeeping can tell, after restart, whether that
request landed. So "exactly once" delivery is impossible in general. What is achievable is
**exactly-once effect**: the request may be *delivered* more than once, but the downstream
*applies* it once. That requires the downstream to deduplicate on a key we send.

## Decision

1. **Deterministic effect key**: `sha256(run_id | gate_id | artifact_version)`. The same
   approved version of the same run always produces the same key, however many times the node
   re-executes. A different approved version (after a reject and revise) is a different
   effect.
2. **Ledger** (`effects` table), per publish:
   - record the intent: insert `(key, PENDING)` if absent;
   - if the row is already `SUCCEEDED`, return the stored receipt without calling out;
   - call the sink with `Idempotency-Key: <key>`;
   - record `SUCCEEDED` with the receipt (or `FAILED` with the error; a later attempt retries).
3. **The sink honors the key** (`published_memos.idempotency_key UNIQUE`; a repeat returns the
   original row and increments a `deliveries` counter so we can see replays happened).
4. **Approvals are idempotent** too: approve/reject lock the approval row; the first decision
   re-queues the run, a repeat of the same decision returns "duplicate" (200) without
   re-queueing, and a conflicting decision gets 409. One gate resumes its run at most once.
5. `publish` re-checks the approval invariant itself right before the effect (ADR 0004).

## Evidence

- `test_crash_between_downstream_write_and_ledger_update_does_not_duplicate`: the sink
  accepts, the worker "dies" before the ledger update, the retry reaches the sink with the same
  key; result: one stored memo, two deliveries.
- `test_double_and_concurrent_approvals_resume_once_and_publish_once`: 10 concurrent approves
  give 1 applied + 9 duplicates, one `hitl_decided` event, one publish.
- Chaos runs (3 seeds x 50 runs, workers killed mid-run, 2-3 concurrent approvals on about
  half the gates): 0 duplicate external effects, 0 gates resumed twice. In those runs no kill
  happened to land inside the publish call itself, so that exact window is covered by the
  deterministic test above rather than by the chaos numbers.

## If the downstream does not honor keys

Then exactly-once effect is not achievable, only at-least-once or at-most-once, and we need
**reconciliation**:
- before retrying a `PENDING`/`FAILED` effect, query the downstream for evidence it happened
  (search by a natural key such as run id and version embedded in the payload);
- if found, mark `SUCCEEDED` and move on; if not, retry;
- if the downstream cannot be queried either, choose per effect type: at-most-once (never
  retry; alert a human on `PENDING` older than a threshold) for irreversible actions like
  payments or emails, at-least-once (retry; tolerate duplicates) for harmless ones.
The `effects` table already holds what a reconciliation job needs: key, request, status,
attempts and timestamps.
