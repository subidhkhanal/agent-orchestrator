"""The publish side effect.

`publish` is the only node with an external effect. Every publish carries an effect key derived
from (run_id, gate_id, artifact_version), so re-executing the node after a crash, or resuming
twice, sends the same key and the sink returns the original receipt instead of posting again.
The durable effects ledger that wraps this lives in the Postgres layer (milestone 2).
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


def effect_key(run_id: str, gate_id: str, artifact_version: int) -> str:
    raw = f"{run_id}|{gate_id}|{artifact_version}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class PublishRequest:
    effect_key: str
    tenant_id: str
    run_id: str
    artifact_id: str
    artifact_version: int
    content: str


@dataclass(frozen=True)
class PublishReceipt:
    effect_key: str
    sink_ref: str
    published_at: datetime
    duplicate: bool = False


class Publisher(Protocol):
    async def publish(self, request: PublishRequest) -> PublishReceipt: ...


class InMemoryPublishSink:
    """A downstream that honors idempotency keys: same key, same receipt, no second post."""

    def __init__(self, clock: object) -> None:
        self._clock = clock
        self._by_key: dict[str, PublishReceipt] = {}
        self.posts: list[PublishRequest] = []
        self._lock = asyncio.Lock()

    async def publish(self, request: PublishRequest) -> PublishReceipt:
        async with self._lock:
            existing = self._by_key.get(request.effect_key)
            if existing is not None:
                return PublishReceipt(
                    existing.effect_key, existing.sink_ref, existing.published_at, duplicate=True
                )
            self.posts.append(request)
            receipt = PublishReceipt(
                effect_key=request.effect_key,
                sink_ref=f"published/{len(self.posts)}",
                published_at=self._clock.now(),  # type: ignore[attr-defined]
            )
            self._by_key[request.effect_key] = receipt
            return receipt
