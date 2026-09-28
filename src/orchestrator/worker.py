"""Worker process entry point: `python -m orchestrator.worker`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import timedelta

from orchestrator.agents import NODE_LIBRARY
from orchestrator.db.graphs import sync_graph_versions
from orchestrator.db.pool import open_pool
from orchestrator.engine.worker import Worker
from orchestrator.graphs.definitions import default_registry
from orchestrator.runtime import build_runtime, gateway_config, load_settings, verify_models


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set")

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    if settings.worker_metrics_port:
        from prometheus_client import start_http_server

        start_http_server(settings.worker_metrics_port)
    pool = await open_pool(settings.database_url, max_size=5)
    runtime = build_runtime(settings, pool)
    await verify_models(gateway_config(settings), runtime.providers)
    graphs = default_registry()
    await sync_graph_versions(pool, graphs)

    worker = Worker(
        deps=runtime.deps,
        runs=runtime.runs,
        graphs=graphs,
        library=NODE_LIBRARY,
        checkpointer=AsyncPostgresSaver(pool),
        worker_id=settings.worker_id,
        lease_s=settings.worker_lease_s,
        heartbeat_s=settings.worker_heartbeat_s,
        poll_s=settings.worker_poll_s,
        hitl_timeout=timedelta(hours=settings.hitl_timeout_hours),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loop
            signal.signal(sig, lambda *_: stop.set())
    try:
        await worker.run_forever(stop)
    finally:
        await pool.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg's async mode needs a selector event loop on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
