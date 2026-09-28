"""Effects ledger and the (simulated) downstream publish sink.

Ledger protocol for one effect key:
  1. record intent:   INSERT effects(key, PENDING) ON CONFLICT DO NOTHING
  2. short-circuit:   if the row is already SUCCEEDED, return the stored receipt
  3. call downstream: sink.publish(key)   <- the sink dedupes on the same key
  4. record result:   UPDATE effects SET SUCCEEDED, response

A crash between 3 and 4 leaves the row PENDING. The node re-executes on resume, reaches step 3
again with the *same* key, and the sink returns the original receipt instead of posting again.
That is why this is exactly-once only because the downstream honors the key (ADR 0005).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from orchestrator.db.pool import Pool
from orchestrator.effects.publisher import Publisher, PublishReceipt, PublishRequest


class LedgerPublisher:
    def __init__(self, pool: Pool, sink: Publisher) -> None:
        self._pool = pool
        self._sink = sink

    async def publish(self, request: PublishRequest) -> PublishReceipt:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO effects (effect_key, run_id, tenant_id, kind, request) "
                "VALUES (%s, %s, %s, 'publish_memo', %s) ON CONFLICT (effect_key) DO NOTHING",
                (
                    request.effect_key,
                    request.run_id,
                    request.tenant_id,
                    Jsonb(
                        {
                            "artifact_id": request.artifact_id,
                            "artifact_version": request.artifact_version,
                            "content_chars": len(request.content),
                        }
                    ),
                ),
            )
            row = await (
                await conn.execute(
                    "UPDATE effects SET attempts = attempts + 1, updated_at = now() "
                    "WHERE effect_key = %s RETURNING status, response",
                    (request.effect_key,),
                )
            ).fetchone()
        assert row is not None
        if row["status"] == "SUCCEEDED":
            r = row["response"]
            return PublishReceipt(
                r["effect_key"],
                r["sink_ref"],
                datetime.fromisoformat(r["published_at"]),
                duplicate=True,
            )

        try:
            receipt = await self._sink.publish(request)
        except Exception as exc:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE effects SET status = 'FAILED', response = %s, updated_at = now() "
                    "WHERE effect_key = %s AND status <> 'SUCCEEDED'",
                    (Jsonb({"error": repr(exc)}), request.effect_key),
                )
            raise

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE effects SET status = 'SUCCEEDED', response = %s, updated_at = now() "
                "WHERE effect_key = %s",
                (
                    Jsonb({**asdict(receipt), "published_at": receipt.published_at.isoformat()}),
                    request.effect_key,
                ),
            )
        return receipt


class PostgresPublishSink:
    """The downstream system itself: a 'published memos' feed that honors idempotency keys."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def publish(self, request: PublishRequest) -> PublishReceipt:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO published_memos (idempotency_key, tenant_id, run_id, "
                    "artifact_id, artifact_version, content) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (idempotency_key) DO UPDATE "
                    "SET deliveries = published_memos.deliveries + 1 "
                    "RETURNING id, published_at, deliveries",
                    (
                        request.effect_key,
                        request.tenant_id,
                        request.run_id,
                        request.artifact_id,
                        request.artifact_version,
                        request.content,
                    ),
                )
            ).fetchone()
        assert row is not None
        return PublishReceipt(
            effect_key=request.effect_key,
            sink_ref=f"published/{row['id']}",
            published_at=row["published_at"],
            duplicate=row["deliveries"] > 1,
        )

    async def feed(self, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT id, run_id, artifact_id, artifact_version, content, published_at, "
                    "deliveries FROM published_memos WHERE tenant_id = %s "
                    "ORDER BY published_at DESC LIMIT %s",
                    (tenant_id, limit),
                )
            ).fetchall()


class HttpPublishSink:
    """Client for the sink's HTTP endpoint (POST /api/v1/publish-sink with Idempotency-Key)."""

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = base_url.rstrip("/") + "/api/v1/publish-sink"
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=15)

    async def publish(self, request: PublishRequest) -> PublishReceipt:
        response = await self._client.post(
            self._url,
            headers={
                "Idempotency-Key": request.effect_key,
                "Authorization": f"Bearer {self._token}",
            },
            json={
                "tenant_id": request.tenant_id,
                "run_id": request.run_id,
                "artifact_id": request.artifact_id,
                "artifact_version": request.artifact_version,
                "content": request.content,
            },
        )
        response.raise_for_status()
        data = response.json()
        return PublishReceipt(
            effect_key=request.effect_key,
            sink_ref=data["sink_ref"],
            published_at=datetime.fromisoformat(data["published_at"]),
            duplicate=data["duplicate"],
        )
