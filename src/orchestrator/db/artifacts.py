"""Artifact content in Postgres (append-only; state only holds references)."""

from __future__ import annotations

import hashlib
from typing import Any

from orchestrator.db.pool import Pool
from orchestrator.tools.services import artifact_ref, parse_artifact_ref


class PostgresArtifactStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def put(
        self,
        *,
        run_id: str,
        tenant_id: str,
        artifact_id: str,
        version: int,
        type_: str,
        content: str,
        producer_agent: str = "coder",
    ) -> str:
        digest = hashlib.sha256(content.encode()).hexdigest()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO artifacts (run_id, artifact_id, version, tenant_id, type, content, "
                "content_sha256, producer_agent) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (run_id, artifact_id, version, tenant_id, type_, content, digest, producer_agent),
            )
        return artifact_ref(run_id, artifact_id, version, content)

    async def get(self, content_ref: str, *, tenant_id: str) -> str:
        run_id, artifact_id, version, digest = parse_artifact_ref(content_ref)
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT content FROM artifacts WHERE run_id = %s AND artifact_id = %s "
                    "AND version = %s AND tenant_id = %s AND content_sha256 LIKE %s",
                    (run_id, artifact_id, version, tenant_id, digest + "%"),
                )
            ).fetchone()
        if row is None:
            raise KeyError(content_ref)
        return str(row["content"])

    async def versions(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT artifact_id, version, type, content_sha256, producer_agent, "
                    "created_at FROM artifacts WHERE run_id = %s AND tenant_id = %s "
                    "ORDER BY artifact_id, version, created_at",
                    (run_id, tenant_id),
                )
            ).fetchall()
