"""FastAPI application: `uvicorn orchestrator.api.app:app`.

The API never executes graphs. It validates requests, writes runs/decisions to Postgres, and
streams events; background workers (orchestrator.worker) do the execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from orchestrator import metrics
from orchestrator.api.schemas import (
    ApproveRequest,
    CreateRunRequest,
    CreateRunResponse,
    DecisionResponse,
    PublishSinkRequest,
    RejectRequest,
)
from orchestrator.api.streaming import Broadcaster, event_stream
from orchestrator.config import Settings
from orchestrator.db.artifacts import PostgresArtifactStore
from orchestrator.db.effects import PostgresPublishSink
from orchestrator.db.events import PostgresEventLog
from orchestrator.db.graphs import sync_graph_versions
from orchestrator.db.hitl import DecisionConflict, GateNotFound, HitlStore, NotWaiting
from orchestrator.db.pool import Pool, open_pool
from orchestrator.db.runs import (
    IdempotencyConflict,
    RunStateConflict,
    RunStore,
    new_run_id,
    request_hash,
)
from orchestrator.db.tenants import Tenant, TenantStore
from orchestrator.effects.publisher import PublishRequest
from orchestrator.events.log import EventType
from orchestrator.graphs.definitions import default_registry
from orchestrator.graphs.spec import GraphRegistry
from orchestrator.runtime import load_settings
from orchestrator.state.models import Budget, RunState


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers


@dataclass
class Services:
    settings: Settings
    pool: Pool
    runs: RunStore
    events: PostgresEventLog
    hitl: HitlStore
    tenants: TenantStore
    artifacts: PostgresArtifactStore
    sink: PostgresPublishSink
    graphs: GraphRegistry
    broadcaster: Broadcaster
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class RateLimiter:
    """Sliding-window limiter, in memory (one API instance; a shared store would be needed
    behind a load balancer)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_s: float) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - window_s:
            hits.popleft()
        if len(hits) >= limit:
            retry = int(hits[0] + window_s - now) + 1
            raise ApiError(
                429, "rate_limited", "too many runs; try again later", {"Retry-After": str(retry)}
            )
        hits.append(now)


def services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


Svc = Annotated[Services, Depends(services)]


async def tenant(
    svc: Svc,
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Query()] = None,
) -> Tenant:
    # access_token in the query string exists only for EventSource, which cannot send
    # headers. It is accepted for the stream endpoint's convenience; prefer the header.
    secret = None
    if authorization and authorization.lower().startswith("bearer "):
        secret = authorization[7:].strip()
    secret = secret or access_token
    if secret:
        found = await svc.tenants.authenticate(secret)
        if found is None:
            raise ApiError(401, "unauthorized", "invalid API key")
        return found
    if svc.settings.demo_mode:
        demo = await svc.tenants.get(svc.settings.demo_tenant_id)
        if demo is not None:
            return demo
    raise ApiError(401, "unauthorized", "missing API key")


Auth = Annotated[Tenant, Depends(tenant)]


class Background:
    """Optional in-process tasks for single-container hosts with little memory."""

    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.wake = asyncio.Event()  # set by the API when a run becomes runnable
        self.tasks: list[asyncio.Task[None]] = []

    async def stop(self) -> None:
        self.stop_event.set()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def start_background(settings: Settings, pool: Pool, graphs: GraphRegistry) -> Background:
    background = Background()
    if settings.embedded_worker:
        # Same Worker as `python -m orchestrator.worker`, run as a task in the API process.
        # Leases, fencing and resume work identically; it just shares the process.
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        from orchestrator.agents import NODE_LIBRARY
        from orchestrator.engine.worker import Worker
        from orchestrator.runtime import build_runtime, gateway_config, verify_models

        runtime = build_runtime(settings, pool)
        await verify_models(gateway_config(settings), runtime.providers)
        worker = Worker(
            deps=runtime.deps,
            runs=runtime.runs,
            graphs=graphs,
            library=NODE_LIBRARY,
            checkpointer=AsyncPostgresSaver(pool),
            worker_id=settings.worker_id,
            lease_s=settings.worker_lease_s,
            heartbeat_s=settings.worker_heartbeat_s,
            poll_s=settings.worker_poll_s,
            hitl_timeout=timedelta(hours=settings.hitl_timeout_hours),
            wake=background.wake,
            sweep_s=settings.worker_sweep_s,
        )
        background.tasks.append(asyncio.create_task(worker.run_forever(background.stop_event)))
    if settings.keepalive_url:
        background.tasks.append(asyncio.create_task(_keepalive(settings, background.stop_event)))
    return background


async def _keepalive(settings: Settings, stop: asyncio.Event) -> None:
    """Request our own public /health periodically.

    Free hosts scale an idle service to zero, which would also stop the embedded worker and
    make the next visitor wait for a cold start. This keeps the instance warm.
    """
    import httpx

    url = str(settings.keepalive_url).rstrip("/") + "/livez"  # no database query
    async with httpx.AsyncClient(timeout=20) as client:
        while not stop.is_set():
            with contextlib.suppress(Exception):
                await client.get(url)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.keepalive_interval_s)


def wake_worker(request: Request) -> None:
    wake: asyncio.Event | None = getattr(request.app.state, "wake", None)
    if wake is not None:
        wake.set()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    limiter = RateLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        pool = await open_pool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_size,
            max_idle_s=settings.db_pool_max_idle_s,
        )
        graphs = default_registry()
        await sync_graph_versions(pool, graphs)
        tenants = TenantStore(pool)
        if settings.demo_mode:
            await tenants.create_tenant(
                settings.demo_tenant_id,
                "Public demo",
                settings.demo_max_usd_per_run,
                settings.demo_max_tokens_per_run,
            )
        broadcaster = Broadcaster(settings.database_url)
        await broadcaster.start()
        app.state.services = Services(
            settings,
            pool,
            RunStore(pool),
            PostgresEventLog(pool),
            HitlStore(pool),
            tenants,
            PostgresArtifactStore(pool),
            PostgresPublishSink(pool),
            graphs,
            broadcaster,
        )
        background = await start_background(settings, pool, graphs)
        app.state.wake = background.wake
        app.state.services.wake = background.wake
        try:
            yield
        finally:
            await background.stop()
            await broadcaster.stop()
            await pool.close()

    app = FastAPI(title="agent-orchestrator", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        route = request.scope.get("route")
        metrics.HTTP_REQUESTS.labels(
            getattr(route, "path", "unmatched"), str(response.status_code)
        ).inc()
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            status_code=exc.status,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": "invalid request",
                    "details": exc.errors(),
                }
            },
            status_code=422,
        )

    async def owned_run(svc: Services, t: Tenant, run_id: str) -> dict[str, Any]:
        row = await svc.runs.get(t.tenant_id, run_id)
        if row is None:
            # Same answer whether the run does not exist or belongs to another tenant.
            raise ApiError(404, "not_found", "run not found")
        return row

    # --- runs --------------------------------------------------------------------------

    @app.post("/api/v1/graph-runs", status_code=201, response_model=CreateRunResponse)
    async def create_run(
        body: CreateRunRequest,
        svc: Svc,
        t: Auth,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> CreateRunResponse:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ApiError(400, "idempotency_key_required", "Idempotency-Key header is required")
        s = svc.settings
        demo = s.demo_mode and t.tenant_id == s.demo_tenant_id
        if demo and len(body.input.task) > s.max_task_chars:
            raise ApiError(422, "task_too_long", f"task is limited to {s.max_task_chars} chars")
        try:
            spec = svc.graphs.get(body.graph_id, body.graph_version)
        except KeyError:
            raise ApiError(404, "graph_not_found", "unknown graph or version") from None
        if body.budget.max_usd > t.max_usd_per_run or body.budget.max_tokens > t.max_tokens_per_run:
            raise ApiError(
                402,
                "budget_over_allowance",
                f"requested budget exceeds this tenant's allowance "
                f"(max ${t.max_usd_per_run} and {t.max_tokens_per_run} tokens per run)",
            )

        body_hash = request_hash(body.model_dump(mode="json"))
        # A retried request must get its original run back, even if limits were hit since.
        existing = await svc.runs.find_by_key(t.tenant_id, idempotency_key)
        if existing is not None:
            if existing["create_request_hash"] != body_hash:
                raise ApiError(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different body",
                )
            response.status_code = 200
            return CreateRunResponse(
                run_id=existing["run_id"],
                status=existing["status"],
                graph_id=existing["graph_id"],
                graph_version=existing["graph_version"],
                stream_url=f"/api/v1/graph-runs/{existing['run_id']}/stream",
                created=False,
            )
        if demo:
            client = request.client.host if request.client else "unknown"
            limiter.check(f"runs:{t.tenant_id}:{client}", s.rate_limit_runs_per_hour, 3600)
            today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            reserved = await svc.runs.reserved_usd_since(today)
            reserved_tokens = await svc.runs.reserved_tokens_since(today)
            if (
                reserved + body.budget.max_usd > s.daily_usd_cap
                or reserved_tokens + body.budget.max_tokens > s.daily_token_cap
            ):
                retry = int((today + timedelta(days=1) - datetime.now(UTC)).total_seconds())
                raise ApiError(
                    503,
                    "daily_budget_exhausted",
                    "the demo's daily spend cap is reached; try again tomorrow",
                    {"Retry-After": str(retry)},
                )

        run_id = new_run_id()
        deadline = (
            datetime.now(UTC) + timedelta(seconds=body.budget.deadline_s)
            if body.budget.deadline_s
            else None
        )
        state = RunState(
            run_id=run_id,
            tenant_id=t.tenant_id,
            graph_id=spec.graph_id,
            graph_version=spec.version,
            task=body.input.task,
            task_kind=body.input.task_kind,
            max_steps=body.options.max_steps,
            budget=Budget(
                token_limit=body.budget.max_tokens,
                usd_limit=body.budget.max_usd,
                tokens_remaining=body.budget.max_tokens,
                usd_remaining=body.budget.max_usd,
                deadline_at=deadline,
            ),
        )
        try:
            row, created = await svc.runs.create(
                tenant_id=t.tenant_id,
                idempotency_key=idempotency_key,
                body_hash=body_hash,
                initial_state=state,
                topology_hash=spec.topology_hash(),
                options={"checkpoint_event_every_n": body.checkpoint_policy.event_every_n},
            )
        except IdempotencyConflict:
            raise ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different body",
            ) from None
        if not created:
            response.status_code = 200
        wake_worker(request)
        return CreateRunResponse(
            run_id=row["run_id"],
            status=row["status"],
            graph_id=row["graph_id"],
            graph_version=row["graph_version"],
            stream_url=f"/api/v1/graph-runs/{row['run_id']}/stream",
            created=created,
        )

    @app.get("/api/v1/graph-runs")
    async def list_runs(svc: Svc, t: Auth, limit: int = Query(20, ge=1, le=100)) -> Any:
        return {"runs": await svc.runs.list_runs(t.tenant_id, limit)}

    @app.get("/api/v1/graph-runs/{run_id}")
    async def get_run(run_id: str, svc: Svc, t: Auth) -> Any:
        row = await owned_run(svc, t, run_id)
        state = RunState.model_validate(row["state"])
        return {
            "run_id": run_id,
            "status": row["status"],
            "graph_id": row["graph_id"],
            "graph_version": row["graph_version"],
            "state_version": row["state_version"],
            "state": row["state"],
            "cost": {
                "usd_spent": round(state.budget.usd_limit - state.budget.usd_remaining, 6),
                "tokens_spent": state.budget.token_limit - state.budget.tokens_remaining,
                "usd_limit": state.budget.usd_limit,
                "token_limit": state.budget.token_limit,
            },
            "approvals": await svc.hitl.get(t.tenant_id, run_id),
            "error": row["error"],
            "attempt": row["attempt"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    @app.get("/api/v1/graph-runs/{run_id}/events")
    async def list_events(
        run_id: str,
        svc: Svc,
        t: Auth,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> Any:
        await owned_run(svc, t, run_id)
        events = await svc.events.read(run_id, after_event_id=after, limit=limit)
        return {
            "events": [e.model_dump(mode="json") for e in events],
            "next_after": events[-1].event_id if events else after,
            "has_more": len(events) == limit,
        }

    @app.get("/api/v1/graph-runs/{run_id}/stream")
    async def stream(
        run_id: str,
        svc: Svc,
        t: Auth,
        last_event_id: Annotated[str | None, Header()] = None,
        after: int = Query(0, ge=0),
    ) -> StreamingResponse:
        await owned_run(svc, t, run_id)
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
        return StreamingResponse(
            event_stream(run_id, cursor, svc.events, svc.broadcaster),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/graph-runs/{run_id}/artifacts/{artifact_id}")
    async def get_artifact(
        run_id: str, artifact_id: str, svc: Svc, t: Auth, version: int | None = None
    ) -> Any:
        row = await owned_run(svc, t, run_id)
        state = RunState.model_validate(row["state"])
        ref = next((a for a in state.artifacts if a.id == artifact_id), None)
        if ref is None or (version is not None and version != ref.version):
            raise ApiError(404, "not_found", "artifact not found")
        content = await svc.artifacts.get(ref.content_ref, tenant_id=t.tenant_id)
        return {
            "artifact_id": artifact_id,
            "version": ref.version,
            "content": content,
            "sources": [s.model_dump() for s in state.sources],
        }

    async def _decide(
        svc: Services,
        t: Tenant,
        run_id: str,
        gate_id: str,
        decision: str,
        reviewer: str | None,
        comment: str | None,
    ) -> DecisionResponse:
        await owned_run(svc, t, run_id)
        try:
            result = await svc.hitl.decide(
                tenant_id=t.tenant_id,
                run_id=run_id,
                gate_id=gate_id,
                decision=decision,  # type: ignore[arg-type]
                reviewer=reviewer or t.name,
                comment=comment,
            )
        except GateNotFound:
            raise ApiError(404, "not_found", "approval gate not found") from None
        except DecisionConflict as exc:
            raise ApiError(409, "decision_conflict", str(exc)) from None
        except NotWaiting as exc:
            raise ApiError(412, "not_waiting_for_approval", str(exc)) from None
        row = await owned_run(svc, t, run_id)
        if result.outcome == "applied":
            svc.wake.set()
        return DecisionResponse(
            outcome=result.outcome, gate_id=gate_id, decision=decision, run_status=row["status"]
        )

    @app.post("/api/v1/graph-runs/{run_id}/hitl/{gate_id}/approve")
    async def approve(
        run_id: str, gate_id: str, svc: Svc, t: Auth, body: ApproveRequest | None = None
    ) -> DecisionResponse:
        body = body or ApproveRequest()
        return await _decide(svc, t, run_id, gate_id, "approved", body.reviewer, body.comment)

    @app.post("/api/v1/graph-runs/{run_id}/hitl/{gate_id}/reject")
    async def reject(
        run_id: str, gate_id: str, body: RejectRequest, svc: Svc, t: Auth
    ) -> DecisionResponse:
        return await _decide(svc, t, run_id, gate_id, "rejected", body.reviewer, body.reason)

    @app.post("/api/v1/graph-runs/{run_id}/cancel", status_code=202)
    async def cancel(run_id: str, svc: Svc, t: Auth) -> Any:
        await owned_run(svc, t, run_id)
        try:
            status = await svc.runs.request_cancel(t.tenant_id, run_id)
        except RunStateConflict as exc:
            raise ApiError(409, "already_finished", str(exc)) from None
        if status == "CANCELLED":
            await svc.events.append(run_id, EventType.CANCELLED, {"by": "user"}, "cancelled")
        return {
            "run_id": run_id,
            "status": str(status),
            "detail": "cancelled" if status == "CANCELLED" else "cancel requested",
        }

    # --- graphs, feed, sink --------------------------------------------------------------

    @app.get("/api/v1/graphs")
    async def list_graphs(svc: Svc) -> Any:
        return {
            "graphs": [
                {
                    "graph_id": g.graph_id,
                    "version": g.version,
                    "description": g.description,
                    "topology_hash": g.topology_hash(),
                    "nodes": list(g.nodes),
                    "mermaid": g.mermaid(),
                }
                for g in svc.graphs.all()
            ]
        }

    @app.get("/api/v1/published")
    async def published(svc: Svc, t: Auth, limit: int = Query(20, ge=1, le=100)) -> Any:
        return {"published": await svc.sink.feed(t.tenant_id, limit)}

    @app.post("/api/v1/publish-sink")
    async def publish_sink(
        body: PublishSinkRequest,
        svc: Svc,
        idempotency_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Any:
        """The simulated downstream system. It honors Idempotency-Key: same key, same result."""
        token = svc.settings.publish_sink_token
        if not token or authorization != f"Bearer {token}":
            raise ApiError(401, "unauthorized", "publish sink token required")
        if not idempotency_key:
            raise ApiError(400, "idempotency_key_required", "Idempotency-Key header is required")
        receipt = await svc.sink.publish(
            PublishRequest(
                idempotency_key,
                body.tenant_id,
                body.run_id,
                body.artifact_id,
                body.artifact_version,
                body.content,
            )
        )
        return {
            "sink_ref": receipt.sink_ref,
            "published_at": receipt.published_at.isoformat(),
            "duplicate": receipt.duplicate,
        }

    # --- ops ---------------------------------------------------------------------------

    @app.get("/livez")
    async def livez() -> Any:
        """Process liveness only (no database), for keep-alive pings and load balancers."""
        return {"status": "ok"}

    @app.get("/health")
    async def health(svc: Svc) -> Any:
        try:
            async with svc.pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception:
            raise ApiError(503, "database_unavailable", "database unavailable") from None
        return {"status": "ok"}

    @app.get("/metrics")
    async def prometheus(svc: Svc) -> Response:
        async with svc.pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT status, count(*) AS n FROM graph_runs GROUP BY status")
            ).fetchall()
        for status in ("QUEUED", "RUNNING", "WAITING_HITL", "COMPLETED", "FAILED", "CANCELLED"):
            metrics.RUNS_BY_STATUS.labels(status).set(
                next((r["n"] for r in rows if r["status"] == status), 0)
            )
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def __getattr__(name: str) -> Any:
    # `uvicorn orchestrator.api.app:app` builds the app lazily, so importing this module (for
    # tests) does not read the environment.
    if name == "app":
        return create_app()
    raise AttributeError(name)
