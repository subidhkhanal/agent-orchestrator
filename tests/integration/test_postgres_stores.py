"""Event log, state commits, leases and tenant scoping against real Postgres."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.db.runs import IdempotencyConflict, request_hash
from orchestrator.events.log import EventType, LeaseLost
from orchestrator.state.models import RunState
from orchestrator.state.reducer import StalePatchError
from tests.integration.conftest import Stack

pytestmark = pytest.mark.postgres


async def test_event_ids_are_monotonic_and_keys_idempotent(stack: Stack) -> None:
    run_id = await stack.create_run()
    first = await stack.events.append(run_id, EventType.NODE_STARTED, {}, "k1")
    again = await stack.events.append(run_id, EventType.NODE_STARTED, {}, "k1")
    second = await stack.events.append(run_id, EventType.NODE_STARTED, {}, "k2")
    assert again is None
    assert first and second and (first.event_id, second.event_id) == (2, 3)  # 1 = run_created
    assert [e.event_id for e in await stack.events.read(run_id, after_event_id=1)] == [2, 3]


async def test_concurrent_appends_get_unique_contiguous_ids(stack: Stack) -> None:
    run_id = await stack.create_run()
    await asyncio.gather(
        *(stack.events.append(run_id, EventType.STATE_PATCH, {"i": i}, f"k{i}") for i in range(20))
    )
    ids = [e.event_id for e in await stack.events.read(run_id, limit=100)]
    assert ids == list(range(1, 22))


async def test_create_run_is_idempotent_per_key(stack: Stack) -> None:
    run_a = await stack.create_run(key="same-key")
    run_b = await stack.create_run(key="same-key")
    assert run_a == run_b
    with pytest.raises(IdempotencyConflict):
        await stack.create_run(key="same-key", task="a different task")
    # The key is scoped per tenant.
    assert await stack.create_run(tenant_id="tenant-b", key="same-key") != run_a


async def test_claim_is_exclusive_under_concurrency(stack: Stack) -> None:
    run_ids = {await stack.create_run() for _ in range(5)}
    claims = await asyncio.gather(*(stack.runs.claim(f"w{i}", 30) for i in range(10)))
    claimed = [c.run_id for c in claims if c is not None]
    assert sorted(claimed) == sorted(run_ids)  # each run exactly once, extra workers get None


async def test_expired_lease_is_reclaimed_with_a_new_attempt_and_old_writer_is_fenced(
    stack: Stack,
) -> None:
    run_id = await stack.create_run()
    first = await stack.runs.claim("w1", 30)
    assert first and first.attempt == 1
    assert await stack.runs.claim("w2", 30) is None  # lease still valid
    async with stack.pool.connection() as conn:
        await conn.execute(
            "UPDATE graph_runs SET lease_expires_at = now() - interval '1 second' "
            "WHERE run_id = %s",
            (run_id,),
        )
    second = await stack.runs.claim("w2", 30)
    assert second and second.run_id == run_id and second.attempt == 2

    # The first worker wakes up and tries to write: every path is fenced.
    with pytest.raises(LeaseLost):
        await stack.events.append(run_id, EventType.NODE_STARTED, {}, "zombie", attempt=1)
    state = RunState.model_validate(first.state)
    with pytest.raises(LeaseLost):
        await stack.runs.commit(run_id, 1, 0, state.model_copy(update={"state_version": 1}))
    assert not (await stack.runs.heartbeat(run_id, 1, 30)).alive
    assert (await stack.runs.heartbeat(run_id, 2, 30)).alive


async def test_state_commit_rejects_stale_versions(stack: Stack) -> None:
    run_id = await stack.create_run()
    claimed = await stack.runs.claim("w1", 30)
    assert claimed
    state = RunState.model_validate(claimed.state)
    v1 = state.model_copy(update={"state_version": 1})
    await stack.runs.commit(run_id, claimed.attempt, 0, v1)
    with pytest.raises(StalePatchError):
        await stack.runs.commit(run_id, claimed.attempt, 0, v1)  # based on v0, stored is v1


async def test_tenant_scoping(stack: Stack) -> None:
    run_id = await stack.create_run(tenant_id="tenant-a")
    assert await stack.runs.get("tenant-a", run_id) is not None
    assert await stack.runs.get("tenant-b", run_id) is None
    ref = await stack.deps.services.artifacts.put(
        run_id=run_id,
        tenant_id="tenant-a",
        artifact_id="memo",
        version=1,
        type_="memo",
        content="x",
    )
    assert await stack.deps.services.artifacts.get(ref, tenant_id="tenant-a") == "x"
    with pytest.raises(KeyError):
        await stack.deps.services.artifacts.get(ref, tenant_id="tenant-b")


def test_request_hash_ignores_key_order() -> None:
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})
