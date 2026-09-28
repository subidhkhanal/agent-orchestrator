from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from dotenv import dotenv_values

import orchestrator.db  # noqa: F401  (libpq path on Windows)
from orchestrator.agents import NODE_LIBRARY
from orchestrator.clock import SystemClock
from orchestrator.db.artifacts import PostgresArtifactStore
from orchestrator.db.effects import LedgerPublisher, PostgresPublishSink
from orchestrator.db.events import PostgresEventLog
from orchestrator.db.graphs import sync_graph_versions
from orchestrator.db.hitl import HitlStore
from orchestrator.db.pool import open_pool
from orchestrator.db.runs import RunStore, new_run_id, request_hash
from orchestrator.db.tenants import TenantStore
from orchestrator.engine.context import EngineDeps
from orchestrator.engine.worker import Worker
from orchestrator.gateway.config import GatewayConfig
from orchestrator.gateway.gateway import LLMGateway
from orchestrator.gateway.providers.demo_policy import ResearchMemoPolicy
from orchestrator.gateway.providers.fake import FakeLLM
from orchestrator.graphs.definitions import default_registry
from orchestrator.offline import DEFAULT_MODELS_CONFIG, SAMPLE_RAG_HITS, SAMPLE_SEARCH_HITS
from orchestrator.runtime import project_path
from orchestrator.state.models import Budget, RunState
from orchestrator.tools import default_registry as default_tools
from orchestrator.tools.services import StaticRag, StaticSearch, StubSandbox, ToolServices

TABLES = (
    "published_memos, effects, artifacts, hitl_approvals, graph_events, graph_runs, "
    "api_keys, tenants, graph_versions, checkpoint_writes, checkpoint_blobs, checkpoints"
)


def _test_db_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or dotenv_values(".env").get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = _test_db_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; skipping Postgres tests")
    return url


@pytest.fixture
async def pool(test_db_url: str) -> AsyncIterator[Any]:
    pool = await open_pool(test_db_url, max_size=20)
    async with pool.connection() as conn:
        await conn.execute(f"TRUNCATE {TABLES} CASCADE")
    await TenantStore(pool).create_tenant("tenant-a", "A", 1.0, 200_000)
    await TenantStore(pool).create_tenant("tenant-b", "B", 1.0, 200_000)
    await sync_graph_versions(pool, default_registry())
    yield pool
    await pool.close()


@dataclass
class Stack:
    pool: Any
    deps: EngineDeps
    runs: RunStore
    events: PostgresEventLog
    hitl: HitlStore
    sink: PostgresPublishSink
    llm: FakeLLM

    def worker(self, name: str = "w1", **kw: Any) -> Worker:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        return Worker(
            deps=self.deps,
            runs=self.runs,
            graphs=default_registry(),
            library=NODE_LIBRARY,
            checkpointer=AsyncPostgresSaver(self.pool),
            worker_id=name,
            **{"lease_s": 30.0, "heartbeat_s": 0.2, "poll_s": 0.05, **kw},
        )

    async def create_run(
        self, tenant_id: str = "tenant-a", key: str | None = None, task: str = "Research storage"
    ) -> str:
        run_id = new_run_id()
        state = RunState(
            run_id=run_id,
            tenant_id=tenant_id,
            graph_id="research-memo",
            graph_version=1,
            task=task,
            budget=Budget(
                token_limit=100_000, usd_limit=1.0, tokens_remaining=100_000, usd_remaining=1.0
            ),
        )
        row, _ = await self.runs.create(
            tenant_id=tenant_id,
            idempotency_key=key or run_id,
            body_hash=request_hash({"task": task}),
            initial_state=state,
            topology_hash=default_registry().get("research-memo", 1).topology_hash(),
            options={},
        )
        return str(row["run_id"])

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchall()


@pytest.fixture
def stack(pool: Any) -> Stack:
    return make_stack(pool)


def make_stack(pool: Any, llm: FakeLLM | None = None) -> Stack:
    llm = llm or FakeLLM(policy=ResearchMemoPolicy())
    clock = SystemClock()
    runs = RunStore(pool)
    events = PostgresEventLog(pool)
    sink = PostgresPublishSink(pool)
    deps = EngineDeps(
        gateway=LLMGateway(
            GatewayConfig.load(project_path(DEFAULT_MODELS_CONFIG)), {"fake": llm}, clock
        ),
        tools=default_tools(),
        services=ToolServices(
            search=StaticSearch(default=list(SAMPLE_SEARCH_HITS)),
            rag=StaticRag(hits=list(SAMPLE_RAG_HITS)),
            artifacts=PostgresArtifactStore(pool),
            sandbox=StubSandbox(),
        ),
        events=events,
        publisher=LedgerPublisher(pool, sink),
        clock=clock,
        state_store=runs,
    )
    return Stack(pool, deps, runs, events, HitlStore(pool), sink, llm)
