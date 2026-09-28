"""Postgres event log.

Each append runs in one transaction that:
1. locks the run's graph_runs row (serializes appends per run, and gives us the tenant),
2. checks the fencing token when the writer is a worker (attempt must match),
3. returns early if the idempotency key was already used,
4. takes the next event_id from graph_runs.last_event_id (so ids have no gaps),
5. inserts the event and sends NOTIFY so live SSE streams wake up.

NOTIFY is transactional: listeners only hear about events that were committed.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb

from orchestrator.db.pool import Pool
from orchestrator.events.log import Event, EventType, LeaseLost

NOTIFY_CHANNEL = "run_events"


async def append_in_tx(
    conn: Any,
    run_id: str,
    tenant_id: str,
    event_type: EventType,
    payload: dict[str, Any],
    idempotency_key: str,
) -> int | None:
    """Append inside the caller's transaction. The caller must hold the graph_runs row lock."""
    exists = await (
        await conn.execute(
            "SELECT 1 FROM graph_events WHERE run_id = %s AND idempotency_key = %s",
            (run_id, idempotency_key),
        )
    ).fetchone()
    if exists:
        return None
    row = await (
        await conn.execute(
            "UPDATE graph_runs SET last_event_id = last_event_id + 1 WHERE run_id = %s "
            "RETURNING last_event_id",
            (run_id,),
        )
    ).fetchone()
    event_id = int(row["last_event_id"])
    await conn.execute(
        "INSERT INTO graph_events (run_id, event_id, tenant_id, event_type, payload, "
        "idempotency_key) VALUES (%s, %s, %s, %s, %s, %s)",
        (run_id, event_id, tenant_id, str(event_type), Jsonb(payload), idempotency_key),
    )
    await conn.execute(
        "SELECT pg_notify(%s, %s)",
        (NOTIFY_CHANNEL, json.dumps({"run_id": run_id, "event_id": event_id})),
    )
    return event_id


class PostgresEventLog:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def append(
        self,
        run_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        idempotency_key: str,
        attempt: int | None = None,
    ) -> Event | None:
        async with self._pool.connection() as conn, conn.transaction():
            run = await (
                await conn.execute(
                    "SELECT tenant_id, attempt FROM graph_runs WHERE run_id = %s FOR UPDATE",
                    (run_id,),
                )
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown run {run_id}")
            if attempt is not None and run["attempt"] != attempt:
                raise LeaseLost(f"run {run_id} is now at attempt {run['attempt']}, not {attempt}")
            exists = await (
                await conn.execute(
                    "SELECT 1 FROM graph_events WHERE run_id = %s AND idempotency_key = %s",
                    (run_id, idempotency_key),
                )
            ).fetchone()
            if exists:
                return None
            row = await (
                await conn.execute(
                    "UPDATE graph_runs SET last_event_id = last_event_id + 1 WHERE run_id = %s "
                    "RETURNING last_event_id",
                    (run_id,),
                )
            ).fetchone()
            assert row is not None
            inserted = await (
                await conn.execute(
                    "INSERT INTO graph_events "
                    "(run_id, event_id, tenant_id, event_type, payload, idempotency_key) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING created_at",
                    (
                        run_id,
                        row["last_event_id"],
                        run["tenant_id"],
                        str(event_type),
                        Jsonb(payload),
                        idempotency_key,
                    ),
                )
            ).fetchone()
            assert inserted is not None
            await conn.execute(
                "SELECT pg_notify(%s, %s)",
                (NOTIFY_CHANNEL, json.dumps({"run_id": run_id, "event_id": row["last_event_id"]})),
            )
            return Event(
                run_id=run_id,
                event_id=row["last_event_id"],
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
                created_at=inserted["created_at"],
            )

    async def read(self, run_id: str, after_event_id: int = 0, limit: int = 100) -> list[Event]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT run_id, event_id, event_type, payload, idempotency_key, created_at "
                    "FROM graph_events WHERE run_id = %s AND event_id > %s "
                    "ORDER BY event_id LIMIT %s",
                    (run_id, after_event_id, limit),
                )
            ).fetchall()
        return [Event.model_validate(r) for r in rows]
