"""Full research-memo runs on the in-memory runner with the scripted fake LLM."""

from __future__ import annotations

import json

import pytest

from orchestrator.agents.guards import InvariantViolation
from orchestrator.agents.hitl import publish_node
from orchestrator.events import EventType
from orchestrator.gateway.providers.demo_policy import ResearchMemoPolicy
from orchestrator.gateway.providers.fake import FakeLLM, FakeReply
from orchestrator.offline import build_offline
from tests.factories import make_budget

TASK = "Research grid battery storage trends and draft a briefing memo"


def approve(gate_id: str) -> dict[str, str]:
    return {"gate_id": gate_id, "decision": "approved", "reviewer": "alice"}


async def test_approve_path_publishes_exactly_once() -> None:
    runner, deps = build_offline()
    paused = await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=make_budget()
    )
    assert paused.status == "waiting_hitl"
    assert paused.state.hitl.pending and paused.interrupt == {
        "gate_id": "gate_memo_v1",
        "artifact_id": "memo",
        "artifact_version": 1,
    }
    assert deps.publisher.posts == []  # type: ignore[attr-defined]

    done = await runner.resume("r1", approve("gate_memo_v1"))
    assert done.status == "completed"
    assert done.state.termination.reason == "published"
    assert done.state.publication and done.state.publication.artifact_version == 1
    assert len(deps.publisher.posts) == 1  # type: ignore[attr-defined]


async def test_reject_revise_approve_loop() -> None:
    runner, deps = build_offline()
    first = await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=make_budget()
    )
    rejected = await runner.resume(
        "r1",
        {
            "gate_id": "gate_memo_v1",
            "decision": "rejected",
            "reviewer": "alice",
            "comment": "Add a note on what changed.",
        },
    )
    assert first.interrupt and rejected.status == "waiting_hitl"
    # The rejection reason reached the coder, which wrote v2, which was reviewed again.
    assert rejected.interrupt == {
        "gate_id": "gate_memo_v2",
        "artifact_id": "memo",
        "artifact_version": 2,
    }
    feedback = [n for n in rejected.state.review_notes if n.kind == "human_rejection"]
    assert [(n.author, n.artifact_version) for n in feedback] == [("human:alice", 1)]
    coder_prompts = [
        c.request
        for c in runner._deps.gateway._providers["fake"].calls  # type: ignore[attr-defined]
        if c.node == "coder"
    ]
    assert "Add a note on what changed." in coder_prompts[-1].messages[1].content

    done = await runner.resume("r1", approve("gate_memo_v2"))
    assert done.status == "completed" and done.state.publication.artifact_version == 2  # type: ignore[union-attr]
    assert len(deps.publisher.posts) == 1  # type: ignore[attr-defined]


async def test_decision_for_a_stale_gate_fails_instead_of_publishing() -> None:
    runner, deps = build_offline()
    await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=make_budget()
    )
    await runner.resume(
        "r1",
        {"gate_id": "gate_memo_v1", "decision": "rejected", "reviewer": "a", "comment": "redo"},
    )
    # An approval for v1 arrives after v2 is pending (e.g. a slow second browser tab).
    outcome = await runner.resume("r1", approve("gate_memo_v1"))
    assert outcome.status == "failed" and "InvariantViolation" in (outcome.error or "")
    assert deps.publisher.posts == []  # type: ignore[attr-defined]


async def test_reviewer_loop_fixes_an_uncited_first_draft() -> None:
    runner, _ = build_offline(llm=FakeLLM(policy=ResearchMemoPolicy(flawed_first_draft=True)))
    paused = await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=make_budget()
    )
    verdicts = [
        (n.artifact_version, n.verdict) for n in paused.state.review_notes if n.kind == "verdict"
    ]
    assert verdicts == [(1, "changes_requested"), (2, "pass")]
    assert paused.interrupt["artifact_version"] == 2  # type: ignore[index]


async def test_reviewer_pass_is_overridden_when_the_citation_check_fails() -> None:
    llm = FakeLLM(policy=ResearchMemoPolicy(flawed_first_draft=True))
    # The reviewer model skips its tools and rubber-stamps the flawed draft.
    llm.push("reviewer", FakeReply(content=json.dumps({"verdict": "pass", "summary": "lgtm"})))
    runner, _ = build_offline(llm=llm)
    paused = await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=make_budget()
    )
    first_verdict = next(n for n in paused.state.review_notes if n.kind == "verdict")
    assert first_verdict.artifact_version == 1 and first_verdict.verdict == "changes_requested"
    assert "model verdict was 'pass'" in first_verdict.text


async def test_budget_running_low_mid_run_ends_with_final_summary_and_partial_artifact() -> None:
    # A normal run to the approval gate uses ~5.4k tokens; 3k runs out right after the draft.
    runner, deps = build_offline()
    outcome = await runner.start(
        run_id="r1",
        tenant_id="t",
        graph_id="research-memo",
        task=TASK,
        budget=make_budget(tokens=3_000, usd=1.0),
    )
    assert outcome.status == "completed"
    state = outcome.state
    assert state.termination.budget_exhausted and state.termination.final_summary_done
    assert state.termination.reason == "budget_exhausted"
    assert state.current_artifact() is not None  # the partial draft is kept
    assert any(n.kind == "final_summary" for n in state.review_notes)
    assert state.publication is None and deps.publisher.posts == []  # type: ignore[attr-defined]
    routes = [e.payload["next_node"] for e in deps.events.of_type("r1", EventType.ROUTE_DECIDED)]  # type: ignore[attr-defined]
    assert routes[-2:] == ["reviewer", "END"]


async def test_final_summary_is_written_by_the_reviewer_model_when_affordable() -> None:
    runner, _ = build_offline()
    budget = make_budget(tokens=100_000).model_copy(update={"tokens_remaining": 9_000})
    outcome = await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=budget
    )
    [summary] = [n for n in outcome.state.review_notes if n.kind == "final_summary"]
    assert summary.text.startswith("Out of budget")
    assert outcome.state.termination.reason == "budget_exhausted"


async def test_tiny_budget_never_overspends() -> None:
    runner, deps = build_offline()
    budget = make_budget(tokens=3_000, usd=0.002)
    outcome = await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=budget
    )
    assert outcome.status == "completed"
    spent = sum(e.payload["usd"] for e in deps.events.of_type("r1", EventType.LLM_CALL))  # type: ignore[attr-defined]
    tokens = sum(
        e.payload["input_tokens"] + e.payload["output_tokens"]
        for e in deps.events.of_type("r1", EventType.LLM_CALL)
    )  # type: ignore[attr-defined]
    assert spent <= budget.usd_limit and tokens <= budget.token_limit
    assert outcome.state.termination.budget_exhausted


async def test_every_state_version_has_exactly_one_patch_event() -> None:
    runner, deps = build_offline()
    outcome = await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=make_budget()
    )
    patches = deps.events.of_type("r1", EventType.STATE_PATCH)  # type: ignore[attr-defined]
    versions = [e.payload["state_version"] for e in patches]
    assert versions == list(range(1, outcome.state.state_version + 1))
    assert all(e.payload["base_version"] == e.payload["state_version"] - 1 for e in patches)


async def test_node_completed_events_carry_cost_and_latency() -> None:
    runner, deps = build_offline()
    await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=make_budget()
    )
    completed = deps.events.of_type("r1", EventType.NODE_COMPLETED)  # type: ignore[attr-defined]
    assert completed and all(
        {"tokens", "usd", "latency_ms", "next"} <= e.payload.keys() for e in completed
    )
    researcher = next(e for e in completed if e.payload["node"] == "researcher")
    assert researcher.payload["tokens"] > 0


async def test_max_steps_cap_ends_the_run() -> None:
    llm = FakeLLM(policy=ResearchMemoPolicy())
    runner, _ = build_offline(llm=llm)
    outcome = await runner.start(
        run_id="r1",
        tenant_id="t",
        graph_id="research-memo",
        task=TASK,
        budget=make_budget(),
        max_steps=3,
    )
    assert outcome.status == "completed" and outcome.state.termination.reason == "max_steps"
    assert outcome.state.step == 3  # supervisor, researcher, supervisor (which ended it)


async def test_publish_node_refuses_without_approval() -> None:
    runner, _ = build_offline()
    paused = await runner.start(
        run_id="r1", tenant_id="t", graph_id="research-memo", task=TASK, budget=make_budget()
    )
    with pytest.raises(InvariantViolation):
        await publish_node(paused.state, None)  # type: ignore[arg-type]


async def test_single_agent_baseline_uses_the_union_of_tools_and_the_same_budget() -> None:
    llm = FakeLLM(policy=ResearchMemoPolicy())
    runner, deps = build_offline(llm=llm)
    outcome = await runner.start(
        run_id="s1", tenant_id="t", graph_id="single-agent", task=TASK, budget=make_budget()
    )
    assert outcome.status == "completed"
    assert outcome.state.current_artifact() is not None
    offered = {t.name for c in llm.calls for t in c.request.tools}
    assert offered == {
        "web_search",
        "rag_query",
        "summarize",
        "write_artifact",
        "edit_section",
        "spawn_sandbox",
        "policy_check",
        "redline",
        "add_review_note",
    }
    nodes = [e.payload["node"] for e in deps.events.of_type("s1", EventType.NODE_STARTED)]  # type: ignore[attr-defined]
    assert nodes == ["single_agent"]
