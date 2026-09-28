"""Scoped event emission for code running inside a node."""

from __future__ import annotations

from typing import Any, Protocol

from orchestrator.events.log import EventLog, EventType


class Emitter(Protocol):
    async def __call__(self, event_type: EventType, payload: dict[str, Any]) -> None: ...


class NodeEmitter:
    """Emits events for one node execution with deterministic idempotency keys.

    Key format: ``a{attempt}:s{step}:{node}:{seq}``. ``attempt`` is the lease generation of the
    worker executing the run, so a node that re-executes after a crash writes a fresh set of
    events (the audit log shows both attempts), while a retried write of the *same* event from
    the same attempt is deduplicated by the log.
    """

    def __init__(self, log: EventLog, run_id: str, attempt: int, step: int, node: str) -> None:
        self._log = log
        self.run_id = run_id
        self.attempt = attempt
        self.step = step
        self.node = node
        self._seq = 0

    async def __call__(self, event_type: EventType, payload: dict[str, Any]) -> None:
        self._seq += 1
        key = f"a{self.attempt}:s{self.step}:{self.node}:{self._seq}"
        await self._log.append(
            self.run_id,
            event_type,
            {"node": self.node, "step": self.step, "attempt": self.attempt, **payload},
            key,
            attempt=self.attempt,
        )
