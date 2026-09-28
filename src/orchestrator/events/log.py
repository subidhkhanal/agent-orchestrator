"""Append-only run event log.

Events are the audit trail and the source for SSE replay. The log guarantees:
- event_id is monotonic per run (the SSE Last-Event-ID cursor),
- an append with an idempotency_key that was already used for this run is a no-op,
  so retried writes never create duplicate events.

The Postgres implementation (db/events.py) keeps the same contract; this in-memory one
backs unit tests.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


class EventType(StrEnum):
    RUN_CREATED = "run_created"
    RUN_CLAIMED = "run_claimed"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    STATE_PATCH = "state_patch"
    CHECKPOINT = "checkpoint"
    ROUTE_DECIDED = "route_decided"
    ROUTE_INVALID = "route_invalid"
    GUARD_OVERRIDE = "guard_override"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_DENIED = "tool_denied"
    WAITING = "waiting"
    HITL_REQUIRED = "hitl_required"
    HITL_DECIDED = "hitl_decided"
    EFFECT_RECORDED = "effect_recorded"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LeaseLost(Exception):
    """This worker no longer owns the run (its lease expired and another worker claimed it)."""


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    event_id: int
    event_type: EventType
    payload: dict[str, Any]
    idempotency_key: str
    created_at: datetime


class EventLog(Protocol):
    async def append(
        self,
        run_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        idempotency_key: str,
        attempt: int | None = None,
    ) -> Event | None:
        """Append an event. Returns None if the idempotency key was already used.

        When `attempt` is given, the write is fenced: it fails with LeaseLost if another
        worker has claimed the run since (see db/events.py)."""
        ...

    async def read(self, run_id: str, after_event_id: int = 0, limit: int = 100) -> list[Event]: ...


class InMemoryEventLog:
    def __init__(self, clock: Any) -> None:
        self._clock = clock
        self._events: dict[str, list[Event]] = defaultdict(list)
        self._keys: dict[str, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def append(
        self,
        run_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        idempotency_key: str,
        attempt: int | None = None,
    ) -> Event | None:
        async with self._lock:
            if idempotency_key in self._keys[run_id]:
                return None
            event = Event(
                run_id=run_id,
                event_id=len(self._events[run_id]) + 1,
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
                created_at=self._clock.now(),
            )
            self._events[run_id].append(event)
            self._keys[run_id].add(idempotency_key)
            return event

    async def read(self, run_id: str, after_event_id: int = 0, limit: int = 100) -> list[Event]:
        return [e for e in self._events[run_id] if e.event_id > after_event_id][:limit]

    def all(self, run_id: str) -> list[Event]:
        return list(self._events[run_id])

    def of_type(self, run_id: str, event_type: EventType) -> list[Event]:
        return [e for e in self._events[run_id] if e.event_type == event_type]
