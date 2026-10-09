# API reference

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/graph-runs` | `Idempotency-Key` required. Replays return the original run (200); the same key with a different body is 409. |
| GET | `/api/v1/graph-runs` | Recent runs for the caller's tenant |
| GET | `/api/v1/graph-runs/{id}` | Status, current state, cost so far, approvals |
| GET | `/api/v1/graph-runs/{id}/stream` | SSE; resumes after `Last-Event-ID` |
| GET | `/api/v1/graph-runs/{id}/events?after=&limit=` | Paginated audit log |
| GET | `/api/v1/graph-runs/{id}/artifacts/{artifact_id}` | Current memo and its sources |
| POST | `/api/v1/graph-runs/{id}/hitl/{gate}/approve` | Idempotent (`applied` / `duplicate`) |
| POST | `/api/v1/graph-runs/{id}/hitl/{gate}/reject` | Idempotent; `reason` goes to the writer |
| POST | `/api/v1/graph-runs/{id}/cancel` | Queued/paused: immediate. Running: at the next heartbeat. |
| GET | `/api/v1/graphs` | Registered graphs, versions, topology hashes, Mermaid |
| GET | `/api/v1/published` | The publish sink's feed |
| POST | `/api/v1/publish-sink` | The simulated downstream (honors `Idempotency-Key`) |
| GET | `/metrics`, `/health` | Prometheus metrics (derived from committed events); health check |

Errors are `{"error": {"code", "message"}}`:

| Code | Meaning |
|---|---|
| 400 | Missing `Idempotency-Key` |
| 401 | Bad or missing API key (outside demo mode) |
| 402 | Budget above the tenant's per-run allowance |
| 404 | Not found, including another tenant's resources (the same answer on purpose) |
| 409 | Idempotency body mismatch, a conflicting or expired decision, or cancelling a finished run |
| 412 | Approve/reject when the run is not paused at that gate (not reached yet, or cancelled) |
| 422 | Invalid body, or a demo task that is too long |
| 429 | Demo rate limit, with `Retry-After` |
| 503 | Demo daily USD/token cap reached, if one is set (with `Retry-After`), or the database is unavailable |
