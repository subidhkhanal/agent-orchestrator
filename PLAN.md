# Build Plan: Multi-Agent Orchestration Platform (Supervisor-Worker)

> Hand this whole file to Claude Code. Save it in the repo root as `PLAN.md`.

## 0. Context and ground rules (read first)

**What this is.** This is a portfolio project that replaces my "Multi-Agent Grocery Price Comparison" bot on my resume. It is a scaled-down but real implementation of a supervisor-worker orchestration platform, modeled on how LangGraph-style systems are designed in production. I am an AI engineer (fresher level), and I will be interviewed on every design choice in this repo. Code clarity and documented reasoning matter more than feature count.

**Ground rules for you (Claude Code):**
1. Create a **new repo** called `agent-orchestrator`. Do not modify or delete the grocery bot repo.
2. Work on a feature branch. Make **small, logical commits** (one concern per commit, with clear messages). **Do not push** anything, and do not touch any production resource, live database, or hosting setting without asking me first.
3. Use a **separate local database**. Never reuse the live databases of my other projects.
4. **Ask before adding any paid service** or anything that needs a credit card.
5. Write an **ADR** (Architecture Decision Record) in `docs/adr/` for every major decision (list in section 9). Each ADR covers the context, the options considered, the decision, and its consequences.
6. Put **safety invariants in code, not in prompts** (details in section 4.3).
7. **Don't hard-code model names.** Put them in config and verify they are currently available from the provider before using them. A model in one of my projects was retired recently and broke the live site.
8. **Don't overclaim** in the README. Every claim needs a test or an eval number behind it.
9. Write all docs in your own words. Do not copy text from any reference spec I show you.
10. **Stop at the end of each milestone** (section 11) and report: what works, what doesn't, the decisions you made, and any open questions.

---

## 1. Scope

### In scope
- A LangGraph `StateGraph` with a **supervisor** that picks the next node dynamically from shared state, plus three specialized workers: **researcher, coder, reviewer**.
- **Human-in-the-loop gate** as a first-class node that blocks until approve/reject.
- **Authoritative shared state** with validated patches and a monotonic `state_version`, plus an **append-only event log**.
- **Durable execution**: runs survive worker crashes and restarts, and resume from the last checkpoint.
- **Per-node and per-run budgets** (tokens, USD, latency) enforced by a small **LLM gateway** module, using a waterfall run budget.
- **Exactly-once-style external effects** via an effect ledger with idempotency keys.
- **Role-scoped tool allowlists** through a tool router.
- **SSE streaming** of node transitions, state patches, checkpoints, HITL prompts, and costs, with reconnect replay.
- **Tenant isolation** (tenant_id on everything, API keys per tenant).
- **Immutable, versioned graphs** that are pinned per run.
- Integration with my **Document Q&A (RAG) platform** as a researcher tool.
- A minimal **web UI**, an **eval harness with a single-agent baseline**, **chaos tests**, CI, and hosting.

### Out of scope (explain each in the README's "Scaling to production" section)
- Temporal. Use LangGraph's Postgres checkpointer plus a Postgres job lease instead, and document the Temporal migration path in an ADR.
- Real 5K-tenant / 10K-concurrent scale. Do the capacity math in the docs, not in infrastructure.
- Redis, Kafka, and a separate blob store. Postgres covers all of these at this scale; document where each would come in.
- A graph-authoring UI. Graphs are registered in code.
- A real code-execution sandbox. The coder gets a `spawn_sandbox` **interface with a stub implementation**, which my next project (autonomous coding agent) will fill in.

---

## 2. Demo workflows (graphs)

### Graph A: `research-memo` v1 (main demo)
The user gives a task, e.g. "Research X and draft a briefing memo with cited sources."

```
START → supervisor ⇄ {researcher, coder, reviewer} → human_gate → publish → END
```

- **researcher**: web search plus a `rag_query` against my Document Q&A platform. It returns sources with citations into shared state.
- **coder**: drafts and edits the memo artifact. The role name "coder" matches the standard pattern: it produces artifacts and later gets sandbox access.
- **reviewer**: checks that every claim in the draft is backed by a cited source in state, flags policy issues, and writes redlines into state. It can send work back to the coder, and the coder ⇄ reviewer cycle is allowed.
- **human_gate**: pauses the run and shows an artifact preview in the UI.
- **publish**: the only node with an external side effect. It posts the final memo to a publish sink (section 4.6).

### Graph B: `quick-answer` v1
`START → researcher → END`, with no supervisor. It exists to prove the engine is generic and that runs pin a graph version.

### Baseline: `single-agent` v1
One ReAct-style agent with the **same tools and the same budget** as Graph A. It is used only for the eval comparison in section 7.

---

## 3. Tech stack
- Python 3.12, FastAPI, Pydantic v2, LangGraph (`StateGraph`, `interrupt`, `Command`, Postgres checkpointer)
- PostgreSQL (psycopg 3), with Alembic migrations
- LLM providers behind the gateway: use config-driven providers, with a fast/cheap tier for the supervisor and a stronger tier for the workers, plus a fallback provider. Verify the current model list first.
- Web search: pick a provider with a **free tier** and tell me which one before wiring it in.
- Frontend: Next.js (deployed on Vercel, the same as my RAG project)
- Metrics: a Prometheus `/metrics` endpoint
- Tests: pytest, plus a **deterministic fake LLM** for CI

---

## 4. Architecture and components

### 4.1 Shared state
Define one Pydantic model, `RunState`:
- `task`, `constraints[]`
- `sources[]`: `{id, title, url_or_doc_ref, snippet, retrieved_by}`
- `artifacts[]`: `{id, type, content_ref, producer_agent, version}`
- `review_notes[]`, `open_questions[]`
- `budget`: `{tokens_remaining, usd_remaining, deadline_at}`
- `hitl`: `{pending, gate_id, decision}`
- `state_version` (monotonic int), `step`, `graph_id`, `graph_version`

Rules:
- A node **never mutates state directly**. It returns a **patch**, which is validated against the schema. The reducer applies it and increments `state_version`.
- A patch carries the `state_version` it was based on. Stale patches are rejected with optimistic concurrency, and a test must prove this.
- Large artifact content lives in an `artifacts` table and is referenced by id in state, never inlined.
- Every applied patch is appended to `graph_events`.

### 4.2 Supervisor
- Its only job is routing. It has **no tools**.
- It uses structured output: `{"next_node": ..., "reason": ..., "state_patch": {...}}`, validated against the allowed nodes of the pinned graph version.
- If the output is invalid, retry with the validation error injected into the prompt. After **3 consecutive invalid routes, fail the run**.
- Enforce a max-steps cap per run.

### 4.3 Code-enforced invariants (important)
These must hold **even if the LLM ignores its prompt**. Implement them as guard functions that run after the supervisor's decision, and give each one its own test:
- `publish` is unreachable unless `hitl.decision == "approved"` for the current artifact version.
- The coder cannot be routed to until at least one source exists (except in code-only tasks).
- When `tokens_remaining < 10%` or `usd_remaining < 10%`, force a route to a final reviewer summary and then END, with a `budget_exhausted` flag.
- When the budget reaches zero, force END with the partial artifact.

### 4.4 Tool router (least privilege)
The tool registry is keyed by role, and each worker only ever receives its own tool set:
- researcher: `web_search`, `rag_query`, `summarize`
- coder: `write_artifact`, `edit_section`, `spawn_sandbox` (stub)
- reviewer: `policy_check`, `redline`, `add_review_note` (no network)
- supervisor: none

Tests must prove that a worker cannot call a tool outside its allowlist, even if the model emits that tool call.

### 4.5 LLM gateway (`gateway/`)
- A tier config per node type: model, max tokens, latency timeout, and price table.
- The **waterfall budget**: a node can never be granted more than the run's remaining budget. Tokens and USD are deducted after each call.
- On a 429, back off exponentially and emit a `waiting` event. Backoff never extends past the run deadline.
- Fall back to the secondary provider on failure.
- Every call records tokens, cost, latency, model, and node in `graph_events`.

### 4.6 External effects and exactly-once
- A `publish_sink` endpoint that I control, simulating a downstream system (a "published memos" feed shown in the UI). It **honors idempotency keys**: the same key produces the same result with no duplicate.
- An `effects` ledger table. The key is `hash(run_id, gate_id, artifact_version)`. Record the intent, call the sink with the key, then record the result.
- Document in an ADR why true exactly-once is only possible when the downstream honors the key, and what reconciliation would look like otherwise.
- The HITL approve/reject endpoints are **idempotent**: double-clicking or a retried request never double-resumes and never double-publishes.

### 4.7 Durable execution
- Runs execute in **background worker processes**, not in the API process.
- Job claiming uses a Postgres lease: `SELECT ... FOR UPDATE SKIP LOCKED` on `graph_runs`, with a heartbeat and a lease expiry.
- If a worker dies, another worker reclaims the expired lease and **resumes from the latest LangGraph checkpoint** (using the run_id as thread_id).
- Document the behavior clearly: the interrupted node **re-executes** on resume. That is exactly why effects must be idempotent.
- The HITL gate uses LangGraph `interrupt()`. Approve/reject resumes with `Command(resume=...)`. Status changes to `WAITING_HITL` while paused, and the lease is released.
- Checkpoint policy: the LangGraph checkpointer saves every super-step, and a `GraphCheckpointCreated` event is also written to `graph_events` every N steps (configurable, default 5). ADR 0003 explains the N trade-off (write amplification vs replay cost) and why every step is fine at this scale.
- A HITL timeout (configurable, default 24h) fails the run according to policy.

### 4.8 Streaming
- `GET /graph-runs/{id}/stream` as SSE. Event types: `node_started`, `state_patch`, `checkpoint`, `hitl_required`, `node_completed` (with tokens, cost, and latency), `waiting`, `completed`, `failed`.
- Live push uses Postgres `LISTEN/NOTIFY`. Reconnects with `Last-Event-ID` replay the missed events from `graph_events`.

### 4.9 Tenancy and auth
- `tenants` and `api_keys` (hashed) tables, with `tenant_id` on every table and every query scoped by it.
- A test proves that tenant A gets a 404 on tenant B's runs, events, artifacts, and approvals.
- Per-tenant allowance: a run whose requested budget exceeds the allowance is rejected with **402**.

---

## 5. API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/graph-runs` | `Idempotency-Key` header required. The body has graph_id, input, budget, checkpoint_policy, and options. Returns run_id, status, stream_url. |
| GET | `/api/v1/graph-runs/{id}` | Status, current state, and cost so far |
| GET | `/api/v1/graph-runs/{id}/stream` | SSE |
| GET | `/api/v1/graph-runs/{id}/events` | Paginated audit log |
| POST | `/api/v1/graph-runs/{id}/hitl/{gate}/approve` | Idempotent, with an optional comment |
| POST | `/api/v1/graph-runs/{id}/hitl/{gate}/reject` | Idempotent, with a reason. The reason is fed back to the coder. |
| POST | `/api/v1/graph-runs/{id}/cancel` | |
| GET | `/api/v1/graphs` | Registered graphs and versions |
| GET | `/metrics` | Prometheus |

Errors: 400, 401, 402 (budget over allowance), 404, 409 (idempotency or version conflict), 412 (mutation attempted while `WAITING_HITL`), 422, 429 (with Retry-After), 503.

---

## 6. Data model (Alembic migrations)
- `tenants`, `api_keys`
- `graph_versions`: graph_id, version, and a topology hash. These are immutable.
- `graph_runs`: run_id, tenant_id, graph_id, graph_version, status (`QUEUED/RUNNING/WAITING_HITL/COMPLETED/FAILED/CANCELLED`), input, budget, state_version, lease_owner, lease_expires_at, and the idempotency key of the create request
- `graph_events`: run_id, event_id (monotonic per run), event_type, payload, idempotency_key, created_at. It is append-only and has `UNIQUE (run_id, idempotency_key)`.
- `hitl_approvals`: run_id, gate_id, artifact_version, status, reviewer, decided_at, comment
- `effects`: effect_key (PK), run_id, kind, status (`PENDING/SUCCEEDED/FAILED`), request, response, timestamps
- `artifacts`: id, run_id, tenant_id, type, version, content
- the LangGraph checkpointer tables

---

## 7. Evaluation (this produces the resume numbers)

Create `evals/` with a task set of **about 30 research tasks**:
- tasks answerable from documents in my RAG platform
- tasks that need web search
- tasks that need both
- a few that are impossible or adversarial, where the correct behavior is to say the information isn't available rather than invent sources

Run **Graph A vs the single-agent baseline** with the same tools and the same budget, and report:
- task success rate (LLM-judge rubric, plus a list of runs for me to spot-check by hand)
- citation validity: the % of claims backed by a real source in state
- average cost per run, average wall-clock time, and p95 latency per node
- supervisor routing validity rate
- budget adherence: the number of runs that exceeded max_usd (target 0)

**Reliability (chaos) tests:**
- `scripts/chaos.py`: start 50 runs and kill the worker at random points. Report the % that resume and complete, and the **number of duplicate external effects (target 0)**.
- A double-approve and concurrent-approve test (0 duplicate publishes).

Put a results table in the README. **Report honestly**: if the multi-agent graph costs more or doesn't beat the single agent on simple tasks, say so and explain why. That trade-off is expected and it's a good interview discussion.

---

## 8. Tests and CI
- Unit tests: patch validation, state_version conflicts, reducer, allowlist enforcement, each code-enforced invariant, the budget waterfall math, supervisor output validation and the 3-strike failure, effect-key idempotency, tenant scoping.
- Integration tests: a full Graph A run with the **fake scripted LLM**, covering HITL pause, approve, publish, and a reject → revise → approve loop.
- A crash/resume integration test.
- GitHub Actions: lint, unit tests, and integration tests with the fake LLM (no API costs in CI). Add a status badge to the README.

---

## 9. Documentation
**README** must include:
- The problem and what the platform does, in about 5 lines
- An architecture diagram and a Graph A diagram (Mermaid)
- The key design decisions table, linking to the ADRs
- Eval and chaos results tables
- Run-locally instructions (docker-compose for Postgres)
- A limitations section
- A **"Scaling to production"** section: capacity math for a large deployment (tokens/sec, checkpoint writes/sec, storage per day), where Temporal, Redis, a message bus, and blob storage would come in, sharding by tenant, and workflow-history limits for long HITL waits

**ADRs** (`docs/adr/`):
- 0001: LangGraph + Postgres checkpointer + job lease vs Temporal
- 0002: shared state + event log vs message passing
- 0003: checkpoint frequency N
- 0004: code-enforced invariants vs prompt-only rules
- 0005: effect ledger and the limits of exactly-once
- 0006: SSE vs WebSocket
- 0007: model tiering per node and the budget waterfall

---

## 10. Frontend (minimal, functional)
A single page with:
- A graph picker, a task input, and budget fields, plus 3 **example tasks** a visitor can run with one click
- A **live timeline** of node events showing the node, reason, tokens, cost, and latency
- A **graph view** (a Mermaid diagram highlighting the current node)
- A **state inspector** showing the current state_version and a list of applied patches
- An **approval panel**: artifact preview, approve/reject buttons, and a reject comment box
- The final memo with citations, and a running cost meter

### Hosting and demo mode
- Backend container plus Postgres on a free/low-cost host. Propose options to me before deploying.
- Frontend on Vercel.
- **Demo tenant** with no sign-up needed. A **hard per-run USD cap**, a **global daily spend cap** (the app refuses new runs once it's hit), rate limiting, and input length limits.
- The publish sink only writes to the in-app "published" feed. It never sends real emails or posts anywhere.

---

## 11. Milestones (about 2 weeks total; stop and report after each)

| # | Days | Deliverable |
|---|---|---|
| M1 | 1–3 | Repo skeleton, migrations, RunState and reducer, event log, gateway with the fake LLM, supervisor + 3 workers wired in LangGraph, the tool router, invariant guards, unit tests |
| M2 | 4–6 | Background workers with a lease, crash/resume, HITL interrupt/resume, effects ledger + publish sink, idempotent endpoints, chaos script |
| M3 | 7–9 | Real LLM providers, web search, RAG tool, budgets + waterfall, full API, SSE with replay, tenancy |
| M4 | 10–12 | Frontend, metrics, eval harness + single-agent baseline, eval and chaos results |
| M5 | 13–14 | CI, hosting + demo mode, README, ADRs, a final cleanup pass |

## 12. Definition of done
- [ ] Graph A runs end to end in the hosted demo, including the approve and reject paths
- [ ] Killing a worker mid-run → the run resumes. The chaos results show 0 duplicate effects.
- [ ] Every invariant in 4.3 has a passing test
- [ ] Eval results for Graph A vs the baseline are in the README
- [ ] CI is green with the fake LLM
- [ ] All 7 ADRs are written
- [ ] Demo spend caps are verified
- [ ] Commits are logical and nothing has been pushed without my approval
