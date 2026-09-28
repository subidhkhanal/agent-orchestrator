from __future__ import annotations

import json

import pytest

from orchestrator.agents.supervisor import RouteValidationError, parse_route
from orchestrator.events import EventType
from orchestrator.gateway.providers.demo_policy import ResearchMemoPolicy
from orchestrator.gateway.providers.fake import FakeLLM, FakeReply
from orchestrator.offline import build_offline
from tests.factories import make_budget

ALLOWED = ("researcher", "coder", "reviewer", "human_gate", "publish", "END")


def route(next_node: str, **extra: object) -> str:
    return json.dumps({"next_node": next_node, "reason": "because", **extra})


def test_valid_route_parses() -> None:
    decision = parse_route(route("researcher", state_patch={"open_questions": ["q"]}), ALLOWED)
    assert decision.next_node == "researcher"
    assert decision.state_patch.open_questions == ["q"]


def test_code_fenced_json_is_accepted() -> None:
    assert parse_route("```json\n" + route("coder") + "\n```", ALLOWED).next_node == "coder"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "not valid JSON"),
        (route("deploy_to_prod"), "not one of"),
        (json.dumps({"next_node": "coder"}), "reason"),
        (route("coder", tools=["web_search"]), "tools"),
        (route("coder", state_patch={"budget": 100}), "budget"),
    ],
)
def test_invalid_routes_are_rejected_with_a_useful_error(raw: str, message: str) -> None:
    with pytest.raises(RouteValidationError, match=message):
        parse_route(raw, ALLOWED)


async def test_invalid_output_is_retried_with_the_error_in_the_prompt() -> None:
    llm = FakeLLM(policy=ResearchMemoPolicy())
    llm.push("supervisor", FakeReply(content=route("deploy_to_prod")))
    runner, deps = build_offline(llm=llm)
    outcome = await runner.start(
        run_id="r1",
        tenant_id="t",
        graph_id="research-memo",
        task="Research storage",
        budget=make_budget(),
    )
    assert outcome.status == "waiting_hitl"

    first, retry = [c.request for c in llm.calls if c.node == "supervisor"][:2]
    assert "deploy_to_prod" in retry.messages[-1].content
    assert "not one of" in retry.messages[-1].content
    assert len(retry.messages) == len(first.messages) + 2
    invalid = deps.events.of_type("r1", EventType.ROUTE_INVALID)  # type: ignore[attr-defined]
    assert len(invalid) == 1


async def test_three_consecutive_invalid_routes_fail_the_run() -> None:
    llm = FakeLLM(policy=ResearchMemoPolicy())
    llm.push("supervisor", *[FakeReply(content="garbage")] * 3)
    runner, deps = build_offline(llm=llm)
    outcome = await runner.start(
        run_id="r1",
        tenant_id="t",
        graph_id="research-memo",
        task="Research storage",
        budget=make_budget(),
    )
    assert outcome.status == "failed"
    assert "SupervisorRoutingError" in (outcome.error or "")
    assert len(deps.events.of_type("r1", EventType.ROUTE_INVALID)) == 3  # type: ignore[attr-defined]
    assert len(deps.events.of_type("r1", EventType.FAILED)) == 1  # type: ignore[attr-defined]


async def test_two_invalid_then_valid_does_not_fail() -> None:
    llm = FakeLLM(policy=ResearchMemoPolicy())
    llm.push("supervisor", FakeReply(content="garbage"), FakeReply(content="garbage"))
    runner, _ = build_offline(llm=llm)
    outcome = await runner.start(
        run_id="r1",
        tenant_id="t",
        graph_id="research-memo",
        task="Research storage",
        budget=make_budget(),
    )
    assert outcome.status == "waiting_hitl"


async def test_supervisor_trying_to_publish_first_is_overridden_by_code() -> None:
    llm = FakeLLM(policy=ResearchMemoPolicy())
    llm.push("supervisor", FakeReply(content=route("publish")))
    runner, deps = build_offline(llm=llm)
    outcome = await runner.start(
        run_id="r1",
        tenant_id="t",
        graph_id="research-memo",
        task="Research storage",
        budget=make_budget(),
    )
    assert outcome.status == "waiting_hitl"
    first_route = deps.events.of_type("r1", EventType.ROUTE_DECIDED)[0].payload  # type: ignore[attr-defined]
    assert (first_route["proposed"], first_route["next_node"]) == ("publish", "researcher")
    assert deps.publisher.posts == []  # type: ignore[attr-defined]


async def test_supervisor_has_no_tools() -> None:
    llm = FakeLLM(policy=ResearchMemoPolicy())
    runner, _ = build_offline(llm=llm)
    await runner.start(
        run_id="r1",
        tenant_id="t",
        graph_id="research-memo",
        task="Research storage",
        budget=make_budget(),
    )
    assert all(c.request.tools == () for c in llm.calls if c.node == "supervisor")
