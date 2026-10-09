# Scaling to production

The design target in the plan is about 5,000 tenants and 10,000 concurrent runs. That scale
was not built here; this page is the math and the changes it would force.

**Assumptions** (from local measurements with the fake LLM, and from the real smoke runs):
- A run is ~12 super-steps, ~65 events, ~15 LLM calls and ~30k tokens.
- Active execution takes ~5 minutes; approval waits last minutes to hours.
- Storage per run: ~40 KB of events, ~60-200 KB of checkpoints (more with real source
  snippets in state), and ~5-10 KB of artifacts.

**10,000 concurrently executing runs** (not waiting for humans):

| Quantity | Estimate |
|---|---|
| Run throughput | 10,000 / 300 s ≈ **33 runs/s** started and finished |
| LLM tokens | 10,000 × 30k / 300 s ≈ **1M tokens/s** (≈ 10k LLM calls/s at ~100 tokens/s each, streaming) |
| Checkpoint writes | 33 runs/s × 12 steps ≈ **400 checkpoints/s** (≈ 1,500-2,000 row writes/s with blobs and pending writes) |
| Event inserts | 33 × 65 ≈ **2,200 events/s**, each with a `NOTIFY` |
| Storage/day | 33 × 86,400 ≈ 2.9M runs × ~250 KB ≈ **~700 GB/day** before pruning |

What changes, and why:
- **LLM capacity is the real bottleneck,** not our infrastructure. 1M tokens/s needs
  provider enterprise quotas across several providers. The gateway would need per-tenant
  quotas and a global token-bucket service (Redis) so that one tenant cannot starve others.
- **Checkpoints.** Use the shallow checkpointer (latest checkpoint only), prune history when
  runs finish, and keep large values out of state. This cuts storage by roughly 10x.
- **Blob storage.** Move artifacts and large tool outputs to object storage (S3/GCS), keyed by
  the same content hash; state keeps references only (it already does).
- **Message bus.** At ~2k events/s, `LISTEN/NOTIFY` on the primary becomes a problem: the
  notification queue is shared, and every API node holds a listener. Write events to Kafka or
  Redis Streams (or CDC from the events table). Stateless SSE gateways then consume per-run
  partitions. Postgres stays the system of record for audit.
- **Redis** also takes over the rate limiter, the daily spend counters, and short-lived
  caches.
- **Sharding.** Shard by `tenant_id` (every table already carries it, and every query filters
  on it). Big tenants get dedicated shards and a worker pool per shard, so noisy neighbors stay
  contained.
- **Work queue.** `SKIP LOCKED` polling is fine to a few hundred claims/s per database. Beyond
  that, per-shard queues or a real queue feeding workers, with the lease row kept for
  fencing.
- **Temporal.** It becomes worth it when runs are long and numerous enough that durable
  timers, cross-region failover and workflow versioning matter more than operating one
  more system (ADR 0001 has the migration path).
- **Long human waits.** A run waiting 24 hours holds no worker and no lease; it is one row
  plus its checkpoint. What grows is history: Temporal-style engines cap history per workflow
  (tens of thousands of events), so a workflow that loops through many review cycles needs
  "continue-as-new". The equivalent here is capping steps (`max_steps`) and pruning
  checkpoint history on completion. Expiry is a periodic sweep (`hitl_approvals.expires_at`),
  which scales with an index, not with open timers.
