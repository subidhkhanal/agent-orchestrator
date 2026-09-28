from __future__ import annotations

from typing import Any

import pytest

from orchestrator.events import EventType
from orchestrator.gateway.budget import BudgetLedger
from orchestrator.gateway.providers.demo_policy import ResearchMemoPolicy
from orchestrator.gateway.providers.fake import FakeLLM, FakeReply
from orchestrator.gateway.types import ToolCall
from orchestrator.offline import build_offline
from orchestrator.tools import ROLE_TOOLS, ToolContext, default_registry
from orchestrator.tools.services import (
    InMemoryArtifactStore,
    SearchHit,
    StaticRag,
    StaticSearch,
    StubSandbox,
    ToolServices,
)
from tests.factories import make_budget, make_state


def test_role_allowlists_match_the_design() -> None:
    assert ROLE_TOOLS["researcher"] == ("web_search", "rag_query", "summarize")
    assert ROLE_TOOLS["coder"] == ("write_artifact", "edit_section", "spawn_sandbox")
    assert ROLE_TOOLS["reviewer"] == ("policy_check", "redline", "add_review_note")
    assert ROLE_TOOLS["supervisor"] == ()


def test_each_role_is_only_offered_its_own_tools() -> None:
    registry = default_registry()
    for role, tools in ROLE_TOOLS.items():
        assert {s.name for s in registry.router_for(role).specs()} == set(tools)


def test_reviewer_has_no_network_tools() -> None:
    offered = {s.name for s in default_registry().router_for("reviewer").specs()}
    assert offered.isdisjoint({"web_search", "rag_query"})


class Emitted:
    def __init__(self) -> None:
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    async def __call__(self, event_type: EventType, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


def tool_ctx(node: str, search: StaticSearch, emit: Emitted) -> ToolContext:
    async def no_llm(messages: object) -> Any:
        raise AssertionError("no LLM in this test")

    return ToolContext(
        run_id="run-1",
        tenant_id="tenant-a",
        node=node,
        state=make_state(),
        services=ToolServices(search, StaticRag(), InMemoryArtifactStore(), StubSandbox()),
        emit=emit,
        ledger=BudgetLedger.for_node(make_budget(), None),
        new_id=lambda prefix: f"{prefix}_1",
        llm=no_llm,
    )


@pytest.mark.parametrize(
    ("role", "tool"),
    [
        ("coder", "web_search"),
        ("reviewer", "rag_query"),
        ("researcher", "write_artifact"),
        ("supervisor", "web_search"),
        ("coder", "not_a_tool"),
    ],
)
async def test_out_of_allowlist_call_is_denied_and_never_executed(role: str, tool: str) -> None:
    search = StaticSearch()
    emit = Emitted()
    router = default_registry().router_for(role)
    outcome = await router.invoke(
        ToolCall(id="c1", name=tool, arguments={"query": "x y"}), tool_ctx(role, search, emit)
    )
    assert outcome.status == "denied" and outcome.ops == []
    assert search.queries == []  # the handler never ran
    assert [t for t, _ in emit.events] == [EventType.TOOL_DENIED]


async def test_invalid_arguments_are_reported_not_executed() -> None:
    search = StaticSearch()
    outcome = (
        await default_registry()
        .router_for("researcher")
        .invoke(
            ToolCall(id="c1", name="web_search", arguments={"query": ""}),
            tool_ctx("researcher", search, Emitted()),
        )
    )
    assert outcome.status == "invalid_args" and search.queries == []


async def test_allowed_call_runs_and_returns_ops() -> None:
    search = StaticSearch(default=[SearchHit(title="t", url="https://e.org/1", snippet="s")])
    outcome = (
        await default_registry()
        .router_for("researcher")
        .invoke(
            ToolCall(id="c1", name="web_search", arguments={"query": "grid storage"}),
            tool_ctx("researcher", search, Emitted()),
        )
    )
    assert outcome.status == "ok"
    assert [op.path for op in outcome.ops] == ["sources"]


async def test_worker_cannot_use_a_foreign_tool_even_if_the_model_emits_it() -> None:
    """End to end: the coder's model emits web_search; the router refuses and the run goes on."""
    llm = FakeLLM(policy=ResearchMemoPolicy())
    # First coder turn: the model tries to search the web instead of writing.
    llm.push("coder", FakeReply(tool_calls=(("web_search", {"query": "sneaky"}),)))
    search = StaticSearch(
        default=[SearchHit(title="t", url="https://e.org/1", snippet="some text")]
    )
    runner, deps = build_offline(llm=llm, search=search)
    outcome = await runner.start(
        run_id="r1",
        tenant_id="t",
        graph_id="research-memo",
        task="Research storage",
        budget=make_budget(),
    )
    assert outcome.status == "waiting_hitl"
    denied = deps.events.of_type("r1", EventType.TOOL_DENIED)  # type: ignore[attr-defined]
    assert [(e.payload["role"], e.payload["tool"]) for e in denied] == [("coder", "web_search")]
    assert "sneaky" not in search.queries
