"""graph_runs: creation, the work queue (leases), fencing, HITL parking, cancel and timeouts.

Lease protocol
- claim(): one statement picks the oldest runnable run with FOR UPDATE SKIP LOCKED (so
  concurrent workers never block on or double-claim the same row), sets status RUNNING, a
  lease expiry, and increments `attempt`.
- `attempt` is the fencing token. Every write a worker makes on behalf of a run (state
  commits, events, final status) is conditional on `attempt` still matching. If the worker
  stalls past its lease and another worker reclaims the run, the stale worker's next write
  fails with LeaseLost and it stops.
- heartbeat() extends the lease while the worker is alive, and reports cancel requests.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

from orchestrator.db.events import append_in_tx
from orchestrator.db.pool import Pool
from orchestrator.events.log import EventType, LeaseLost
from orchestrator.state.models import RunState
from orchestrator.state.reducer import StalePatchError


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_HITL = "WAITING_HITL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class IdempotencyConflict(Exception):
    """Same Idempotency-Key reused with a different request body."""


class RunStateConflict(Exception):
    """The requested transition is not allowed from the run's current status."""


def request_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class ClaimedRun:
    run_id: str
    tenant_id: str
    graph_id: str
    graph_version: int
    attempt: int
    state: dict[str, Any]
    resume_payload: dict[str, Any] | None
    options: dict[str, Any]


@dataclass(frozen=True)
class Heartbeat:
    alive: bool
    cancel_requested: bool


class RunStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    # --- creation and reads ------------------------------------------------------------------

    async def create(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        body_hash: str,
        initial_state: RunState,
        topology_hash: str,
        options: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Insert a QUEUED run plus its run_created event. Returns (row, created).

        A repeated request with the same key and body returns the original run (created=False).
        The same key with a different body raises IdempotencyConflict.
        """
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "INSERT INTO graph_runs (run_id, tenant_id, graph_id, graph_version, input, "
                    "budget, options, state, create_idempotency_key, create_request_hash, "
                    "last_event_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1) "
                    "ON CONFLICT (tenant_id, create_idempotency_key) DO NOTHING RETURNING *",
                    (
                        initial_state.run_id,
                        tenant_id,
                        initial_state.graph_id,
                        initial_state.graph_version,
                        Jsonb({"task": initial_state.task, "task_kind": initial_state.task_kind}),
                        Jsonb(initial_state.budget.model_dump(mode="json")),
                        Jsonb(options),
                        Jsonb(initial_state.model_dump(mode="json")),
                        idempotency_key,
                        body_hash,
                    ),
                )
            ).fetchone()
            if row is None:
                existing = await (
                    await conn.execute(
                        "SELECT * FROM graph_runs WHERE tenant_id = %s "
                        "AND create_idempotency_key = %s",
                        (tenant_id, idempotency_key),
                    )
                ).fetchone()
                assert existing is not None
                if existing["create_request_hash"] != body_hash:
                    raise IdempotencyConflict(idempotency_key)
                return existing, False
            await conn.execute(
                "INSERT INTO graph_events (run_id, event_id, tenant_id, event_type, payload, "
                "idempotency_key) VALUES (%s, 1, %s, %s, %s, 'run_created')",
                (
                    initial_state.run_id,
                    tenant_id,
                    str(EventType.RUN_CREATED),
                    Jsonb(
                        {
                            "graph_id": initial_state.graph_id,
                            "graph_version": initial_state.graph_version,
                            "topology_hash": topology_hash,
                        }
                    ),
                ),
            )
            await conn.execute(
                "SELECT pg_notify('run_events', %s)",
                (json.dumps({"run_id": initial_state.run_id, "event_id": 1}),),
            )
            return row, True

    async def find_by_key(self, tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT * FROM graph_runs WHERE tenant_id = %s AND create_idempotency_key = %s",
                    (tenant_id, idempotency_key),
                )
            ).fetchone()

    async def get(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT * FROM graph_runs WHERE run_id = %s AND tenant_id = %s",
                    (run_id, tenant_id),
                )
            ).fetchone()

    async def list_runs(self, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT run_id, graph_id, graph_version, status, created_at, updated_at, "
                    "input FROM graph_runs WHERE tenant_id = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, limit),
                )
            ).fetchall()

    # --- the work queue ------------------------------------------------------------------------

    async def claim(self, worker_id: str, lease_s: float) -> ClaimedRun | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    """
                    WITH candidate AS (
                      SELECT run_id FROM graph_runs
                      WHERE status = 'QUEUED'
                         OR (status = 'RUNNING' AND lease_expires_at < now())
                      ORDER BY created_at
                      LIMIT 1
                      FOR UPDATE SKIP LOCKED
                    )
                    UPDATE graph_runs r
                    SET status = 'RUNNING', lease_owner = %s,
                        lease_expires_at = now() + make_interval(secs => %s),
                        attempt = r.attempt + 1, updated_at = now()
                    FROM candidate WHERE r.run_id = candidate.run_id
                    RETURNING r.*
                    """,
                    (worker_id, lease_s),
                )
            ).fetchone()
        if row is None:
            return None
        return ClaimedRun(
            run_id=row["run_id"],
            tenant_id=row["tenant_id"],
            graph_id=row["graph_id"],
            graph_version=row["graph_version"],
            attempt=row["attempt"],
            state=row["state"],
            resume_payload=row["resume_payload"],
            options=row["options"],
        )

    async def heartbeat(self, run_id: str, attempt: int, lease_s: float) -> Heartbeat:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE graph_runs SET lease_expires_at = now() + make_interval(secs => %s) "
                    "WHERE run_id = %s AND attempt = %s AND status = 'RUNNING' "
                    "RETURNING cancel_requested",
                    (lease_s, run_id, attempt),
                )
            ).fetchone()
        if row is None:
            return Heartbeat(alive=False, cancel_requested=False)
        return Heartbeat(alive=True, cancel_requested=row["cancel_requested"])

    # --- state (StateStore protocol) ---------------------------------------------------------

    async def commit(
        self, run_id: str, attempt: int, base_version: int, new_state: RunState
    ) -> None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE graph_runs SET state = %s, state_version = %s, updated_at = now() "
                    "WHERE run_id = %s AND attempt = %s AND state_version = %s "
                    "RETURNING state_version",
                    (
                        Jsonb(new_state.model_dump(mode="json")),
                        new_state.state_version,
                        run_id,
                        attempt,
                        base_version,
                    ),
                )
            ).fetchone()
            if row is not None:
                return
            current = await (
                await conn.execute(
                    "SELECT attempt, state_version FROM graph_runs WHERE run_id = %s", (run_id,)
                )
            ).fetchone()
        if current is None or current["attempt"] != attempt:
            raise LeaseLost(
                f"run {run_id}: lease lost before committing v{new_state.state_version}"
            )
        raise StalePatchError(base_version, current["state_version"])

    async def rewind(self, run_id: str, attempt: int, state: RunState) -> None:
        """Make the stored state match the checkpoint this worker is about to resume from.

        After a crash the previous worker may have committed a patch whose checkpoint was never
        written; the node that produced it will re-execute. The checkpoint is authoritative, so
        the cached state goes back to it (fenced, so only the lease holder can do this).
        """
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE graph_runs SET state = %s, state_version = %s, updated_at = now() "
                    "WHERE run_id = %s AND attempt = %s RETURNING run_id",
                    (Jsonb(state.model_dump(mode="json")), state.state_version, run_id, attempt),
                )
            ).fetchone()
        if row is None:
            raise LeaseLost(run_id)

    # --- transitions -------------------------------------------------------------------------

    async def park_for_hitl(
        self,
        run_id: str,
        attempt: int,
        gate: dict[str, Any],
        approval_window: timedelta,
        announcement: dict[str, Any],
    ) -> None:
        """RUNNING -> WAITING_HITL, release the lease, open the approval row, and emit
        hitl_required, all in one transaction: a client that sees the event can act on it."""
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "UPDATE graph_runs SET status = 'WAITING_HITL', lease_owner = NULL, "
                    "lease_expires_at = NULL, resume_payload = NULL, updated_at = now() "
                    "WHERE run_id = %s AND attempt = %s RETURNING tenant_id",
                    (run_id, attempt),
                )
            ).fetchone()
            if row is None:
                raise LeaseLost(run_id)
            await conn.execute(
                "INSERT INTO hitl_approvals (run_id, gate_id, tenant_id, artifact_id, "
                "artifact_version, expires_at) VALUES (%s, %s, %s, %s, %s, now() + %s) "
                "ON CONFLICT (run_id, gate_id) DO NOTHING",
                (
                    run_id,
                    gate["gate_id"],
                    row["tenant_id"],
                    gate["artifact_id"],
                    gate["artifact_version"],
                    approval_window,
                ),
            )
            await append_in_tx(
                conn,
                run_id,
                row["tenant_id"],
                EventType.HITL_REQUIRED,
                announcement,
                f"hitl_required:{gate['gate_id']}",
            )

    async def finish(
        self, run_id: str, attempt: int, status: RunStatus, error: str | None = None
    ) -> None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE graph_runs SET status = %s, error = %s, lease_owner = NULL, "
                    "lease_expires_at = NULL, finished_at = now(), updated_at = now() "
                    "WHERE run_id = %s AND attempt = %s RETURNING run_id",
                    (str(status), error, run_id, attempt),
                )
            ).fetchone()
        if row is None:
            raise LeaseLost(run_id)

    async def request_cancel(self, tenant_id: str, run_id: str) -> RunStatus:
        """Queued or paused runs are cancelled at once; running ones at the next heartbeat."""
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT status FROM graph_runs WHERE run_id = %s AND tenant_id = %s FOR UPDATE",
                    (run_id, tenant_id),
                )
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            status = RunStatus(row["status"])
            if status in TERMINAL:
                raise RunStateConflict(f"run is already {status}")
            if status == RunStatus.RUNNING:
                await conn.execute(
                    "UPDATE graph_runs SET cancel_requested = true, updated_at = now() "
                    "WHERE run_id = %s",
                    (run_id,),
                )
                return RunStatus.RUNNING
            await conn.execute(
                "UPDATE graph_runs SET status = 'CANCELLED', cancel_requested = true, "
                "finished_at = now(), updated_at = now() WHERE run_id = %s",
                (run_id,),
            )
            await conn.execute(
                "UPDATE hitl_approvals SET status = 'EXPIRED' "
                "WHERE run_id = %s AND status = 'PENDING'",
                (run_id,),
            )
            return RunStatus.CANCELLED

    async def expire_hitl(self) -> list[str]:
        """Fail runs whose approval window passed (policy: timeout -> FAILED)."""
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (
                await conn.execute(
                    "UPDATE hitl_approvals SET status = 'EXPIRED' "
                    "WHERE status = 'PENDING' AND expires_at < now() RETURNING run_id"
                )
            ).fetchall()
            run_ids = [r["run_id"] for r in rows]
            if run_ids:
                await conn.execute(
                    "UPDATE graph_runs SET status = 'FAILED', error = 'hitl_timeout', "
                    "finished_at = now(), updated_at = now() "
                    "WHERE run_id = ANY(%s) AND status = 'WAITING_HITL'",
                    (run_ids,),
                )
        return run_ids

    async def usd_spent_since(self, since: datetime, tenant_id: str | None = None) -> float:
        """Spend from the cached state of runs created since `since` (for daily caps)."""
        query = (
            "SELECT COALESCE(SUM((state->'budget'->>'usd_limit')::numeric "
            "- (state->'budget'->>'usd_remaining')::numeric), 0) AS spent "
            "FROM graph_runs WHERE created_at >= %s"
        )
        params: tuple[Any, ...] = (since,)
        if tenant_id is not None:
            query += " AND tenant_id = %s"
            params = (since, tenant_id)
        async with self._pool.connection() as conn:
            row = await (await conn.execute(query, params)).fetchone()
        return float(row["spent"]) if row else 0.0

    async def reserved_tokens_since(self, since: datetime) -> int:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT COALESCE(SUM((budget->>'token_limit')::bigint), 0) AS reserved "
                    "FROM graph_runs WHERE created_at >= %s",
                    (since,),
                )
            ).fetchone()
        return int(row["reserved"]) if row else 0

    async def reserved_usd_since(self, since: datetime) -> float:
        """Sum of the USD *limits* of runs created since `since`: the most they could spend."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT COALESCE(SUM((budget->>'usd_limit')::numeric), 0) AS reserved "
                    "FROM graph_runs WHERE created_at >= %s",
                    (since,),
                )
            ).fetchone()
        return float(row["reserved"]) if row else 0.0
