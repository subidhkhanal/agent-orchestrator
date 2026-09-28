# ADR 0006: Server-Sent Events (with replay) instead of WebSockets

- Status: accepted (M3)
- Date: 2026-09-28

## Context

The UI shows a live timeline of a run: node starts and finishes, state patches, costs, the
approval prompt. Runs last minutes to a day, clients disconnect (laptop sleeps, mobile network),
and the timeline must be complete after a reconnect.

Traffic is one-directional: the server pushes events; the client's actions (approve, reject,
cancel) are rare, and they are ordinary idempotent POSTs.

## Options considered

1. **WebSocket**: bidirectional, but we do not need client-to-server streaming. It needs its
   own reconnect and resume protocol, is awkward through some proxies, and does not reuse HTTP
   auth and caching semantics as naturally.
2. **Polling** the events endpoint: simple and robust, but either slow or wasteful.
3. **SSE** (chosen): plain HTTP, one-directional, built-in reconnect in browsers, and a
   standard resume cursor (`Last-Event-ID`).

## Decision

- `GET /api/v1/graph-runs/{id}/stream` returns `text/event-stream`. Each event carries
  `id: <event_id>`; `event_id` is monotonic per run with no gaps (ADR 0002).
- On connect the server **subscribes first, then replays** from `graph_events` after the
  client's `Last-Event-ID` (or `?after=`), then streams live events. Subscribing before the
  replay query means nothing committed in between can be missed; ids make duplicates easy to
  drop.
- Live push: one `LISTEN run_events` connection per API process fans out wake-ups to that
  process's streams. The notification carries only `(run_id, event_id)`; handlers re-read the
  rows from the table, so Postgres stays the single source of truth, and a lost notification
  only delays delivery until the 15-second keepalive re-check.
- `NOTIFY` is issued inside the same transaction as the event insert, so listeners are only
  woken for committed events.
- The stream closes after a terminal event (`completed`, `failed`, `cancelled`).
- The frontend reads the stream with `fetch` (so it can send the API key header) and
  reconnects with `Last-Event-ID`; `test_sse_replays_and_resumes_after_last_event_id` checks
  replay order, completeness and resume.

## Consequences

- Good: reconnects are cheap and exact; no client state is needed beyond the last id.
- Good: works through ordinary HTTP infrastructure (we set `X-Accel-Buffering: no` for
  nginx-style proxies).
- Cost: one open HTTP connection per viewer. Fine here; at large scale the fan-out moves off
  Postgres (see README: a message bus or Redis pub/sub feeding stateless SSE gateways), because
  `LISTEN/NOTIFY` traffic goes through the primary database.
- Cost: browsers' `EventSource` cannot send headers, so the API also accepts the key as an
  `access_token` query parameter. Query strings can end up in logs; the frontend therefore uses
  `fetch` with a header instead, and the query option is documented as a fallback.
