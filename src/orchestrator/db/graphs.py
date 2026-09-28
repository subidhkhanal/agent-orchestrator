"""Persist code-registered graph versions, and refuse to start if a version changed."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from orchestrator.db.pool import Pool
from orchestrator.graphs.spec import GraphRegistry


class GraphVersionMismatch(Exception):
    pass


async def sync_graph_versions(pool: Pool, registry: GraphRegistry) -> None:
    async with pool.connection() as conn, conn.transaction():
        for spec in registry.all():
            await conn.execute(
                "INSERT INTO graph_versions (graph_id, version, topology_hash, topology, "
                "description) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    spec.graph_id,
                    spec.version,
                    spec.topology_hash(),
                    Jsonb(spec.topology()),
                    spec.description,
                ),
            )
            row = await (
                await conn.execute(
                    "SELECT topology_hash FROM graph_versions WHERE graph_id = %s AND version = %s",
                    (spec.graph_id, spec.version),
                )
            ).fetchone()
            assert row is not None
            if row["topology_hash"] != spec.topology_hash():
                raise GraphVersionMismatch(
                    f"{spec.graph_id} v{spec.version} in code differs from the stored version; "
                    "register a new version instead of editing an existing one"
                )
