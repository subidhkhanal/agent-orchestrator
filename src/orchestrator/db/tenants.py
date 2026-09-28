"""Tenants and API keys. Keys are random secrets; only their SHA-256 is stored."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from orchestrator.db.pool import Pool

KEY_PREFIX = "ak_"


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    name: str
    max_usd_per_run: float
    max_tokens_per_run: int


class TenantStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def create_tenant(
        self, tenant_id: str, name: str, max_usd_per_run: float, max_tokens_per_run: int
    ) -> Tenant:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO tenants (tenant_id, name, max_usd_per_run, max_tokens_per_run) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id) DO UPDATE SET "
                "name = EXCLUDED.name, max_usd_per_run = EXCLUDED.max_usd_per_run, "
                "max_tokens_per_run = EXCLUDED.max_tokens_per_run",
                (tenant_id, name, max_usd_per_run, max_tokens_per_run),
            )
        return Tenant(tenant_id, name, max_usd_per_run, max_tokens_per_run)

    async def create_api_key(
        self, tenant_id: str, label: str = "", secret: str | None = None
    ) -> str:
        """Returns the secret. It is shown once and cannot be recovered from the database."""
        secret = secret or KEY_PREFIX + secrets.token_urlsafe(32)
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO api_keys (key_id, tenant_id, key_hash, label) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (key_hash) DO NOTHING",
                ("key_" + secrets.token_hex(8), tenant_id, hash_key(secret), label),
            )
        return secret

    async def authenticate(self, secret: str) -> Tenant | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT t.* FROM api_keys k JOIN tenants t USING (tenant_id) "
                    "WHERE k.key_hash = %s AND k.revoked_at IS NULL",
                    (hash_key(secret),),
                )
            ).fetchone()
        return None if row is None else self._tenant(row)

    async def get(self, tenant_id: str) -> Tenant | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT * FROM tenants WHERE tenant_id = %s", (tenant_id,))
            ).fetchone()
        return None if row is None else self._tenant(row)

    @staticmethod
    def _tenant(row: dict[str, object]) -> Tenant:
        return Tenant(
            tenant_id=str(row["tenant_id"]),
            name=str(row["name"]),
            max_usd_per_run=float(row["max_usd_per_run"]),  # type: ignore[arg-type]
            max_tokens_per_run=int(row["max_tokens_per_run"]),  # type: ignore[call-overload]
        )
