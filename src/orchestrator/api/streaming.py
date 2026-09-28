"""Server-Sent Events with replay.

One background task per API process holds a single Postgres connection that LISTENs on
`run_events`. Notifications carry only (run_id, event_id); the fan-out wakes the SSE handlers
subscribed to that run, which then read the new rows from graph_events. The database stays
the source of truth, so a missed notification only delays delivery until the next keepalive
poll, and a reconnecting client resumes exactly after its Last-Event-ID.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator

import psycopg

from orchestrator.db.events import NOTIFY_CHANNEL
from orchestrator.events.log import Event, EventLog

log = logging.getLogger("orchestrator.api.sse")

TERMINAL_EVENTS = {"completed", "failed", "cancelled"}
KEEPALIVE_S = 15.0


class Broadcaster:
    """Holds the LISTEN connection only while at least one stream is open.

    An idle API therefore keeps no database connection open, which lets scale-to-zero
    databases (e.g. Neon) suspend when nobody is watching a run.
    """

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._subscribers: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        return None  # connects lazily on the first subscriber

    async def stop(self) -> None:
        await self._stop_listener()

    async def _stop_listener(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _ensure_listener(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._url, autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    async for note in conn.notifies():
                        try:
                            run_id = json.loads(note.payload)["run_id"]
                        except (ValueError, KeyError):
                            continue
                        for waiter in self._subscribers.get(run_id, ()):
                            waiter.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("LISTEN connection lost; reconnecting")
                await asyncio.sleep(1)

    @contextlib.contextmanager
    def subscribe(self, run_id: str):  # type: ignore[no-untyped-def]
        waiter = asyncio.Event()
        self._subscribers[run_id].add(waiter)
        self._ensure_listener()
        try:
            yield waiter
        finally:
            self._subscribers[run_id].discard(waiter)
            if not self._subscribers[run_id]:
                self._subscribers.pop(run_id, None)
            if not self._subscribers and self._task is not None:
                self._task.cancel()
                self._task = None


def format_sse(event: Event) -> str:
    data = {
        "event_id": event.event_id,
        "type": str(event.event_type),
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }
    body = json.dumps(data, default=str)
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {body}\n\n"


async def event_stream(
    run_id: str, last_event_id: int, events: EventLog, broadcaster: Broadcaster
) -> AsyncIterator[str]:
    cursor = last_event_id
    with broadcaster.subscribe(run_id) as waiter:
        # Subscribe first, then replay: nothing committed in between can be missed.
        yield "retry: 2000\n\n"
        while True:
            waiter.clear()
            batch = await events.read(run_id, after_event_id=cursor, limit=500)
            for event in batch:
                yield format_sse(event)
                cursor = event.event_id
                if str(event.event_type) in TERMINAL_EVENTS:
                    return
            if len(batch) == 500:
                continue
            try:
                await asyncio.wait_for(waiter.wait(), timeout=KEEPALIVE_S)
            except TimeoutError:
                yield ": keepalive\n\n"
