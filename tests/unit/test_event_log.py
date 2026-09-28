from __future__ import annotations

from orchestrator.clock import FakeClock
from orchestrator.events import EventType, InMemoryEventLog, NodeEmitter


async def test_event_ids_are_monotonic_per_run() -> None:
    log = InMemoryEventLog(FakeClock())
    for i in range(3):
        await log.append("r1", EventType.NODE_STARTED, {}, f"k{i}")
    await log.append("r2", EventType.NODE_STARTED, {}, "k0")
    assert [e.event_id for e in log.all("r1")] == [1, 2, 3]
    assert [e.event_id for e in log.all("r2")] == [1]


async def test_duplicate_idempotency_key_is_a_noop() -> None:
    log = InMemoryEventLog(FakeClock())
    first = await log.append("r1", EventType.HITL_DECIDED, {"d": "approved"}, "hitl:g1")
    second = await log.append("r1", EventType.HITL_DECIDED, {"d": "approved"}, "hitl:g1")
    assert first is not None and second is None
    assert len(log.all("r1")) == 1


async def test_list_after_cursor_supports_replay() -> None:
    log = InMemoryEventLog(FakeClock())
    for i in range(5):
        await log.append("r1", EventType.STATE_PATCH, {"i": i}, f"k{i}")
    replay = await log.read("r1", after_event_id=3)
    assert [e.event_id for e in replay] == [4, 5]


async def test_node_emitter_keys_are_stable_within_an_attempt_and_differ_across_attempts() -> None:
    log = InMemoryEventLog(FakeClock())
    # Same attempt re-emitting the same sequence (e.g. a retried write): deduplicated.
    for _ in range(2):
        emit = NodeEmitter(log, "r1", attempt=1, step=4, node="coder")
        await emit(EventType.NODE_STARTED, {})
    assert len(log.all("r1")) == 1
    # A new attempt after a crash re-executes the node: its events are kept separately.
    emit = NodeEmitter(log, "r1", attempt=2, step=4, node="coder")
    await emit(EventType.NODE_STARTED, {})
    assert [e.idempotency_key for e in log.all("r1")] == ["a1:s4:coder:1", "a2:s4:coder:1"]
