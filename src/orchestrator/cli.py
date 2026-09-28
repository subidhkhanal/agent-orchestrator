"""Admin CLI.

python -m orchestrator.cli create-tenant acme --name "Acme" --max-usd 0.10 --max-tokens 200000
python -m orchestrator.cli create-key acme --label "ci"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from orchestrator.db.pool import open_pool
from orchestrator.db.tenants import TenantStore
from orchestrator.runtime import load_settings


async def main(args: argparse.Namespace) -> None:
    settings = load_settings()
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set")
    pool = await open_pool(settings.database_url, max_size=2)
    store = TenantStore(pool)
    try:
        if args.command == "create-tenant":
            tenant = await store.create_tenant(
                args.tenant_id, args.name or args.tenant_id, args.max_usd, args.max_tokens
            )
            print(
                f"tenant {tenant.tenant_id}: max ${tenant.max_usd_per_run}/run, "
                f"{tenant.max_tokens_per_run} tokens/run"
            )
        elif args.command == "create-key":
            if await store.get(args.tenant_id) is None:
                sys.exit(f"unknown tenant {args.tenant_id}")
            secret = await store.create_api_key(args.tenant_id, args.label)
            print(secret)  # shown once; only its hash is stored
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m orchestrator.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("create-tenant")
    t.add_argument("tenant_id")
    t.add_argument("--name", default="")
    t.add_argument("--max-usd", type=float, default=0.10)
    t.add_argument("--max-tokens", type=int, default=200_000)
    k = sub.add_parser("create-key")
    k.add_argument("tenant_id")
    k.add_argument("--label", default="")
    if sys.platform == "win32":
        asyncio.run(main(parser.parse_args()), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main(parser.parse_args()))
