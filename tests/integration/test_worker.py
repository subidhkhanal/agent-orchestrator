"""Durable execution: worker runs, HITL park/resume, crash/resume, effects, idempotent approvals."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator.db.effects import LedgerPublisher
from orchestrator.db.hitl import DecisionConflict, NotWaiting
from orchestrator.effects.publisher import PublishReceipt, PublishRequest, effect_key
from orchestrator.gateway.providers.demo_policy import ResearchMemoPolicy
from orchestrator.gateway.providers.fake import FakeLLM
from tests.integration.conftest import Stack, make_stack

pytestmark = pytest.mark.postgres


async def status(stack: Stack, run_id: str) -> str:
    [row] = await stack.fetch("SELECT status FROM graph_runs WHERE run_id = %s", run_id)
    return str(row["status"])


async def event_types(stack: Stack, run_id: str) -> list[str]:
    rows = await stack.fetch(
        "SELECT event_type FROM graph_events WHERE run_id = %s ORDER BY event_id", run_id
    )
    return [r["event_type"] for r in rows]


async def approve(stack: Stack, run_id: str, gate: str = "gate_memo_v1", **kw: Any) -> Any:
    return await stack.hitl.decide(
        tenant_id="tenant-a",
        run_id=run_id,
        gate_id=gate,
        decision="approved",
        reviewer="alice",
        comment=None,
        **kw,
    )


async def test_worker_runs_to_the_gate_then_publishes_after_approval(stack: Stack) -> None:
    run_id = await stack.create_run()
    worker = stack.worker()
    assert await worker.run_once()
    assert await status(stack, run_id) == "WAITING_HITL"
    [gate] = await stack.hitl.get("tenant-a", run_id)
    assert (gate["gate_id"], gate["status"]) == ("gate_memo_v1", "PENDING")
    assert "hitl_required" in await event_types(stack, run_id)
    assert not await worker.run_once()  # nothing to do while waiting for a human

    assert (await approve(stack, run_id)).outcome == "applied"
    assert await status(stack, run_id) == "QUEUED"
    assert await worker.run_once()
    assert await status(stack, run_id) == "COMPLETED"
    memos = await stack.fetch("SELECT * FROM published_memos WHERE run_id = %s", run_id)
    assert len(memos) == 1 and memos[0]["deliveries"] == 1
    [effect] = await stack.fetch("SELECT * FROM effects WHERE run_id = %s", run_id)
    assert effect["status"] == "SUCCEEDED"
    [row] = await stack.fetch("SELECT state FROM graph_runs WHERE run_id = %s", run_id)
    assert row["state"]["termination"]["reason"] == "published"


async def test_reject_revise_approve_through_the_worker(stack: Stack) -> None:
    run_id = await stack.create_run()
    worker = stack.worker()
    await worker.run_once()
    await stack.hitl.decide(
        tenant_id="tenant-a",
        run_id=run_id,
        gate_id="gate_memo_v1",
        decision="rejected",
        reviewer="alice",
        comment="Add revision notes.",
    )
    await worker.run_once()
    assert [g["gate_id"] for g in await stack.hitl.get("tenant-a", run_id)] == [
        "gate_memo_v1",
        "gate_memo_v2",
    ]
    await approve(stack, run_id, gate="gate_memo_v2")
    await worker.run_once()
    [memo] = await stack.fetch(
        "SELECT artifact_version FROM published_memos WHERE run_id = %s", run_id
    )
    assert memo["artifact_version"] == 2


async def test_double_and_concurrent_approvals_resume_once_and_publish_once(stack: Stack) -> None:
    run_id = await stack.create_run()
    worker = stack.worker()
    with pytest.raises(NotWaiting):  # before the run reaches the gate
        await approve(stack, run_id)
    await worker.run_once()

    results = await asyncio.gather(*(approve(stack, run_id) for _ in range(10)))
    assert sorted(r.outcome for r in results) == ["applied"] + ["duplicate"] * 9
    with pytest.raises(DecisionConflict):
        await stack.hitl.decide(
            tenant_id="tenant-a",
            run_id=run_id,
            gate_id="gate_memo_v1",
            decision="rejected",
            reviewer="bob",
            comment=None,
        )
    decided = [t for t in await event_types(stack, run_id) if t == "hitl_decided"]
    assert len(decided) == 1

    await worker.run_once()
    assert (await approve(stack, run_id)).outcome == "duplicate"  # late double click
    assert not await worker.run_once()  # and it did not re-queue the run
    assert len(await stack.fetch("SELECT 1 FROM published_memos WHERE run_id = %s", run_id)) == 1


async def test_other_tenant_cannot_approve(stack: Stack) -> None:
    from orchestrator.db.hitl import GateNotFound

    run_id = await stack.create_run(tenant_id="tenant-a")
    await stack.worker().run_once()
    with pytest.raises(GateNotFound):
        await stack.hitl.decide(
            tenant_id="tenant-b",
            run_id=run_id,
            gate_id="gate_memo_v1",
            decision="approved",
            reviewer="mallory",
            comment=None,
        )


async def _wait_for_event(stack: Stack, run_id: str, sql_where: str, wait_s: float = 20) -> None:
    deadline = asyncio.get_running_loop().time() + wait_s
    while asyncio.get_running_loop().time() < deadline:
        rows = await stack.fetch(
            f"SELECT 1 FROM graph_events WHERE run_id = %s AND {sql_where}", run_id
        )
        if rows:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(sql_where)


async def test_crash_mid_run_resumes_from_the_last_checkpoint(pool: Any) -> None:
    slow = FakeLLM(policy=ResearchMemoPolicy(), delay_s=0.05)
    stack = make_stack(pool, llm=slow)
    run_id = await stack.create_run()

    # Worker 1 starts, and "dies" (task killed, nothing cleaned up) while the reviewer runs.
    crashing = asyncio.create_task(stack.worker("w1").run_once())
    await _wait_for_event(
        stack, run_id, "event_type = 'node_started' AND payload->>'node' = 'reviewer'"
    )
    crashing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await crashing
    assert await status(stack, run_id) == "RUNNING"  # the dead worker still "holds" it

    # Its lease expires; worker 2 reclaims the run and resumes from the checkpoint.
    await stack.fetch(
        "UPDATE graph_runs SET lease_expires_at = now() - interval '1 second' "
        "WHERE run_id = %s RETURNING 1",
        run_id,
    )
    assert await stack.worker("w2").run_once()
    assert await status(stack, run_id) == "WAITING_HITL"

    claims = await stack.fetch(
        "SELECT payload FROM graph_events WHERE run_id = %s AND event_type = 'run_claimed' "
        "ORDER BY event_id",
        run_id,
    )
    assert [(c["payload"]["attempt"], c["payload"]["mode"]) for c in claims] == [
        (1, "start"),
        (2, "recover"),
    ]
    assert claims[1]["payload"]["resume_from"] == ["reviewer"]  # the interrupted node re-runs
    # Work finished before the crash was not redone: one researcher run in total.
    started = await stack.fetch(
        "SELECT payload->>'node' AS node FROM graph_events WHERE run_id = %s "
        "AND event_type = 'node_started'",
        run_id,
    )
    nodes = [r["node"] for r in started]
    assert nodes.count("researcher") == 1 and nodes.count("reviewer") == 2

    await approve(stack, run_id)
    await stack.worker("w2").run_once()
    assert await status(stack, run_id) == "COMPLETED"
    assert len(await stack.fetch("SELECT 1 FROM published_memos WHERE run_id = %s", run_id)) == 1


class CrashAfterDownstreamWrite:
    """Publishes for real, then 'crashes' before the ledger can record success (first call)."""

    def __init__(self, sink: Any) -> None:
        self.sink = sink
        self.calls = 0

    async def publish(self, request: PublishRequest) -> PublishReceipt:
        self.calls += 1
        receipt: PublishReceipt = await self.sink.publish(request)
        if self.calls == 1:
            raise ConnectionError("worker died after the downstream accepted the memo")
        return receipt


async def test_crash_between_downstream_write_and_ledger_update_does_not_duplicate(
    stack: Stack,
) -> None:
    run_id = await stack.create_run()
    ledger = LedgerPublisher(stack.pool, CrashAfterDownstreamWrite(stack.sink))
    key = effect_key(run_id, "gate_memo_v1", 1)
    request = PublishRequest(key, "tenant-a", run_id, "memo", 1, "# memo")

    with pytest.raises(ConnectionError):
        await ledger.publish(request)
    [effect] = await stack.fetch("SELECT status FROM effects WHERE effect_key = %s", key)
    assert effect["status"] == "FAILED"  # the ledger does not know the downstream succeeded

    receipt = await ledger.publish(request)  # the re-executed node retries with the same key
    assert receipt.duplicate
    [memo] = await stack.fetch("SELECT deliveries FROM published_memos WHERE run_id = %s", run_id)
    assert memo["deliveries"] == 2  # delivered twice, stored once

    third = await ledger.publish(request)  # after success the ledger short-circuits
    assert third.duplicate and third.sink_ref == receipt.sink_ref
    [memo] = await stack.fetch("SELECT deliveries FROM published_memos WHERE run_id = %s", run_id)
    assert memo["deliveries"] == 2


async def test_cancel_running_and_waiting_runs(stack: Stack) -> None:
    waiting = await stack.create_run()
    await stack.worker().run_once()
    assert await stack.runs.request_cancel("tenant-a", waiting) == "CANCELLED"
    with pytest.raises(DecisionConflict):  # cancelling closes the open gate
        await approve(stack, waiting)

    slow_stack = make_stack(stack.pool, llm=FakeLLM(policy=ResearchMemoPolicy(), delay_s=0.05))
    running = await slow_stack.create_run()
    task = asyncio.create_task(slow_stack.worker().run_once())
    await _wait_for_event(slow_stack, running, "event_type = 'node_started'")
    assert await slow_stack.runs.request_cancel("tenant-a", running) == "RUNNING"
    await task
    assert await status(slow_stack, running) == "CANCELLED"
    assert "cancelled" in await event_types(slow_stack, running)


async def test_hitl_timeout_fails_the_run(stack: Stack) -> None:
    run_id = await stack.create_run()
    worker = stack.worker()
    await worker.run_once()
    await stack.fetch(
        "UPDATE hitl_approvals SET expires_at = now() - interval '1 second' "
        "WHERE run_id = %s RETURNING 1",
        run_id,
    )
    await worker.sweep_expired_approvals()
    assert await status(stack, run_id) == "FAILED"
    with pytest.raises(DecisionConflict):
        await approve(stack, run_id)
