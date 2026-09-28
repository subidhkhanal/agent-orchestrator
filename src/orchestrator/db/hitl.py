"""Approve / reject, made idempotent with row locks.

decide() locks the run row, then the approval row, in one transaction. Whoever gets the locks
first moves the approval out of PENDING and queues the run for resumption. Anyone after that
(a double click, a retried request, a second browser tab) sees the decision already made:
- same decision  -> "duplicate": 200 with the original result, nothing re-queued;
- other decision -> DecisionConflict (409).
So a gate can resume its run at most once, and a run can only publish after the one
approval that resumed it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from psycopg.types.json import Jsonb

from orchestrator.db.pool import Pool
from orchestrator.events.log import EventType


class GateNotFound(Exception):
    pass


class DecisionConflict(Exception):
    """The gate was already decided the other way, or it expired."""


class NotWaiting(Exception):
    """The run is not paused at this gate (412): not paused yet, or cancelled."""


@dataclass(frozen=True)
class DecisionResult:
    outcome: Literal["applied", "duplicate"]
    gate_id: str
    decision: Literal["approved", "rejected"]
    reviewer: str | None
    comment: str | None


_STATUS = {"approved": "APPROVED", "rejected": "REJECTED"}


class HitlStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def decide(
        self,
        *,
        tenant_id: str,
        run_id: str,
        gate_id: str,
        decision: Literal["approved", "rejected"],
        reviewer: str,
        comment: str | None,
    ) -> DecisionResult:
        async with self._pool.connection() as conn, conn.transaction():
            run = await (
                await conn.execute(
                    "SELECT status, last_event_id FROM graph_runs "
                    "WHERE run_id = %s AND tenant_id = %s FOR UPDATE",
                    (run_id, tenant_id),
                )
            ).fetchone()
            if run is None:
                raise GateNotFound(run_id)
            gate = await (
                await conn.execute(
                    "SELECT * FROM hitl_approvals WHERE run_id = %s AND gate_id = %s FOR UPDATE",
                    (run_id, gate_id),
                )
            ).fetchone()
            if gate is None:
                if run["status"] in ("RUNNING", "QUEUED"):
                    raise NotWaiting("the run has not reached this approval gate (yet)")
                raise GateNotFound(gate_id)

            if gate["status"] in ("APPROVED", "REJECTED"):
                if gate["status"] != _STATUS[decision]:
                    raise DecisionConflict(f"gate already {gate['status'].lower()}")
                return DecisionResult(
                    "duplicate", gate_id, decision, gate["reviewer"], gate["comment"]
                )
            if gate["status"] == "EXPIRED":
                raise DecisionConflict("gate expired")
            if run["status"] != "WAITING_HITL":
                raise NotWaiting(f"run is {run['status']}")

            await conn.execute(
                "UPDATE hitl_approvals SET status = %s, reviewer = %s, comment = %s, "
                "decided_at = now() WHERE run_id = %s AND gate_id = %s",
                (_STATUS[decision], reviewer, comment, run_id, gate_id),
            )
            resume: dict[str, Any] = {
                "gate_id": gate_id,
                "decision": decision,
                "reviewer": reviewer,
                "comment": comment,
            }
            event_id = run["last_event_id"] + 1
            await conn.execute(
                "UPDATE graph_runs SET status = 'QUEUED', resume_payload = %s, "
                "last_event_id = %s, updated_at = now() WHERE run_id = %s",
                (Jsonb(resume), event_id, run_id),
            )
            await conn.execute(
                "INSERT INTO graph_events (run_id, event_id, tenant_id, event_type, payload, "
                "idempotency_key) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    event_id,
                    tenant_id,
                    str(EventType.HITL_DECIDED),
                    Jsonb({**resume, "node": "human"}),
                    f"hitl_decided:{gate_id}",
                ),
            )
            await conn.execute(
                "SELECT pg_notify('run_events', %s)",
                (json.dumps({"run_id": run_id, "event_id": event_id}),),
            )
            return DecisionResult("applied", gate_id, decision, reviewer, comment)

    async def get(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT gate_id, artifact_id, artifact_version, status, reviewer, comment, "
                    "requested_at, expires_at, decided_at FROM hitl_approvals "
                    "WHERE run_id = %s AND tenant_id = %s ORDER BY requested_at",
                    (run_id, tenant_id),
                )
            ).fetchall()
