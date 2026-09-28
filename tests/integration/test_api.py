"""HTTP API against real Postgres: errors, idempotency, tenancy, HITL, SSE replay, demo caps."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from orchestrator.api.app import create_app
from orchestrator.config import Settings
from orchestrator.db.tenants import TenantStore
from tests.integration.conftest import Stack

pytestmark = pytest.mark.postgres

BODY: dict[str, Any] = {
    "graph_id": "research-memo",
    "input": {"task": "Research grid storage and draft a memo"},
    "budget": {"max_tokens": 50_000, "max_usd": 0.5},
}


@pytest.fixture
async def api(stack: Stack, test_db_url: str) -> AsyncIterator[Any]:
    settings = Settings(
        database_url=test_db_url,
        publish_sink_token="sink-secret",
        demo_mode=True,
        demo_tenant_id="demo",
        demo_max_usd_per_run=0.05,
        daily_usd_cap=0.12,
        rate_limit_runs_per_hour=100,
        max_task_chars=100,
    )
    app = create_app(settings)
    keys = TenantStore(stack.pool)
    key_a = await keys.create_api_key("tenant-a")
    key_b = await keys.create_api_key("tenant-b")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, {"Authorization": f"Bearer {key_a}"}, {"Authorization": f"Bearer {key_b}"}


async def create(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    key: str = "k1",
    body: dict[str, Any] | None = None,
) -> httpx.Response:
    return await client.post(
        "/api/v1/graph-runs", json=body or BODY, headers={**headers, "Idempotency-Key": key}
    )


async def test_create_run_errors_and_idempotency(api: Any) -> None:
    client, a, _ = api
    assert (await client.post("/api/v1/graph-runs", json=BODY, headers=a)).status_code == 400
    bad_key = {"Authorization": "Bearer nope"}
    assert (await create(client, bad_key)).status_code == 401
    over = {**BODY, "budget": {"max_tokens": 50_000, "max_usd": 5.0}}
    assert (await create(client, a, body=over)).status_code == 402
    assert (await create(client, a, body={**BODY, "graph_id": "nope"})).status_code == 404
    assert (await create(client, a, body={**BODY, "input": {}})).status_code == 422

    first = await create(client, a)
    replay = await create(client, a)
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json()["run_id"] == replay.json()["run_id"]
    changed = {**BODY, "input": {"task": "A different task entirely"}}
    assert (await create(client, a, body=changed)).status_code == 409


async def test_tenant_b_cannot_see_or_touch_tenant_a_runs(api: Any, stack: Stack) -> None:
    client, a, b = api
    run_id = (await create(client, a)).json()["run_id"]
    await stack.worker().run_once()  # reaches the approval gate
    base = f"/api/v1/graph-runs/{run_id}"
    assert (await client.get(base, headers=a)).status_code == 200
    for method, path, body in [
        ("GET", base, None),
        ("GET", f"{base}/events", None),
        ("GET", f"{base}/stream", None),
        ("GET", f"{base}/artifacts/memo", None),
        ("POST", f"{base}/hitl/gate_memo_v1/approve", {}),
        ("POST", f"{base}/hitl/gate_memo_v1/reject", {"reason": "x"}),
        ("POST", f"{base}/cancel", None),
    ]:
        response = await client.request(method, path, json=body, headers=b)
        assert response.status_code == 404, (method, path, response.text)
    runs_b = (await client.get("/api/v1/graph-runs", headers=b)).json()["runs"]
    assert run_id not in [r["run_id"] for r in runs_b]
    assert (await client.get(base, headers=a)).json()["status"] == "WAITING_HITL"


async def test_full_flow_over_http(api: Any, stack: Stack) -> None:
    client, a, _ = api
    run_id = (await create(client, a)).json()["run_id"]
    base = f"/api/v1/graph-runs/{run_id}"
    early = await client.post(f"{base}/hitl/gate_memo_v1/approve", json={}, headers=a)
    assert early.status_code == 412  # not paused at the gate yet

    await stack.worker().run_once()
    run = (await client.get(base, headers=a)).json()
    assert run["status"] == "WAITING_HITL" and run["cost"]["usd_spent"] > 0
    assert run["approvals"][0]["status"] == "PENDING"
    memo = (await client.get(f"{base}/artifacts/memo", headers=a)).json()
    assert memo["version"] == 1 and "[src_" in memo["content"]

    ok = await client.post(f"{base}/hitl/gate_memo_v1/approve", json={"comment": "lgtm"}, headers=a)
    dup = await client.post(f"{base}/hitl/gate_memo_v1/approve", json={}, headers=a)
    conflict = await client.post(
        f"{base}/hitl/gate_memo_v1/reject", json={"reason": "no"}, headers=a
    )
    assert (ok.status_code, ok.json()["outcome"]) == (200, "applied")
    assert (dup.status_code, dup.json()["outcome"]) == (200, "duplicate")
    assert conflict.status_code == 409

    await stack.worker().run_once()
    assert (await client.get(base, headers=a)).json()["status"] == "COMPLETED"
    feed = (await client.get("/api/v1/published", headers=a)).json()["published"]
    assert [p["run_id"] for p in feed] == [run_id]
    assert (await client.post(f"{base}/cancel", headers=a)).status_code == 409

    page = (await client.get(f"{base}/events?after=0&limit=5", headers=a)).json()
    assert [e["event_id"] for e in page["events"]] == [1, 2, 3, 4, 5] and page["has_more"]


def parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads("".join(data)))
    return events


async def test_sse_replays_and_resumes_after_last_event_id(api: Any, stack: Stack) -> None:
    client, a, _ = api
    run_id = (await create(client, a)).json()["run_id"]
    worker = stack.worker()
    await worker.run_once()
    await client.post(f"/api/v1/graph-runs/{run_id}/hitl/gate_memo_v1/approve", json={}, headers=a)
    await worker.run_once()

    full = await client.get(f"/api/v1/graph-runs/{run_id}/stream", headers=a)
    events = parse_sse(full.text)
    ids = [e["event_id"] for e in events]
    assert ids == list(range(1, len(ids) + 1))  # everything, in order, no gaps
    types = [e["type"] for e in events]
    for expected in (
        "node_started",
        "state_patch",
        "checkpoint",
        "hitl_required",
        "node_completed",
        "completed",
    ):
        assert expected in types
    assert types[-1] == "completed"  # the stream closes after a terminal event

    resumed = await client.get(
        f"/api/v1/graph-runs/{run_id}/stream", headers={**a, "Last-Event-ID": str(ids[-4])}
    )
    assert [e["event_id"] for e in parse_sse(resumed.text)] == ids[-3:]


async def test_demo_mode_limits(api: Any) -> None:
    client, _, _ = api
    anon: dict[str, str] = {}
    long_task = {**BODY, "input": {"task": "x" * 101}, "budget": {"max_usd": 0.05}}
    assert (await create(client, anon, "d0", long_task)).status_code == 422
    over = {**BODY, "budget": {"max_usd": 0.06}}
    assert (await create(client, anon, "d1", over)).status_code == 402
    ok = {**BODY, "budget": {"max_usd": 0.05, "max_tokens": 50_000}}
    assert (await create(client, anon, "d2", ok)).status_code == 201
    assert (await create(client, anon, "d3", ok)).status_code == 201
    capped = await create(client, anon, "d4", ok)  # 0.15 reserved > 0.12 daily cap
    assert capped.status_code == 503 and int(capped.headers["Retry-After"]) > 0
    assert (await create(client, anon, "d2", ok)).status_code == 200  # replays still work


async def test_publish_sink_endpoint_honors_idempotency_keys(api: Any) -> None:
    client, _, _ = api
    body = {
        "tenant_id": "tenant-a",
        "run_id": "r",
        "artifact_id": "memo",
        "artifact_version": 1,
        "content": "# m",
    }
    headers = {"Authorization": "Bearer sink-secret", "Idempotency-Key": "effect-1"}
    first = (await client.post("/api/v1/publish-sink", json=body, headers=headers)).json()
    second = (await client.post("/api/v1/publish-sink", json=body, headers=headers)).json()
    assert first["sink_ref"] == second["sink_ref"]
    assert (first["duplicate"], second["duplicate"]) == (False, True)
    assert (await client.post("/api/v1/publish-sink", json=body)).status_code == 401


async def test_graphs_health_and_metrics(api: Any) -> None:
    client, _, _ = api
    graphs = (await client.get("/api/v1/graphs")).json()["graphs"]
    assert {g["graph_id"] for g in graphs} == {"research-memo", "quick-answer", "single-agent"}
    assert (await client.get("/health")).json() == {"status": "ok"}
    body = (await client.get("/metrics")).text
    assert "orchestrator_runs" in body
