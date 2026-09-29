"""One test group per code-enforced invariant (PLAN.md section 4.3)."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.agents.guards import InvariantViolation, enforce_invariants, publish_allowed
from orchestrator.graphs.definitions import RESEARCH_MEMO_V1
from orchestrator.graphs.spec import END
from orchestrator.state.models import Budget
from tests.factories import make_budget, make_state

GRAPH = RESEARCH_MEMO_V1
SOURCE = {
    "id": "src_00000001",
    "title": "A",
    "url_or_doc_ref": "https://e.org/a",
    "snippet": "s",
    "retrieved_by": "researcher",
}


def memo(version: int) -> dict[str, Any]:
    return {
        "id": "memo",
        "type": "memo",
        "content_ref": f"ref/v{version}",
        "producer_agent": "coder",
        "version": version,
    }


def approved(version: int, **kw: Any) -> dict[str, Any]:
    return {
        "pending": False,
        "gate_id": f"gate_memo_v{version}",
        "artifact_id": "memo",
        "artifact_version": version,
        "decision": "approved",
        **kw,
    }


# --- Invariant 1: publish only after approval of the current artifact version --------------


def test_publish_without_any_approval_is_redirected_to_human_gate() -> None:
    state = make_state(sources=[SOURCE], artifacts=[memo(1)])
    result = enforce_invariants(state, "publish", GRAPH)
    assert result.next_node == "human_gate"
    assert [o.guard for o in result.overrides] == ["publish_requires_approval"]


def test_approval_of_an_older_version_does_not_allow_publish() -> None:
    state = make_state(sources=[SOURCE], artifacts=[memo(2)], hitl=approved(1))
    assert not publish_allowed(state)
    assert enforce_invariants(state, "publish", GRAPH).next_node == "human_gate"


def test_rejected_or_pending_gate_does_not_allow_publish() -> None:
    rejected = make_state(
        sources=[SOURCE], artifacts=[memo(1)], hitl={**approved(1), "decision": "rejected"}
    )
    pending = make_state(
        sources=[SOURCE], artifacts=[memo(1)], hitl={**approved(1), "pending": True}
    )
    for state in (rejected, pending):
        assert enforce_invariants(state, "publish", GRAPH).next_node == "human_gate"


def test_publish_with_approval_of_current_version_is_allowed() -> None:
    state = make_state(sources=[SOURCE], artifacts=[memo(2)], hitl=approved(2))
    result = enforce_invariants(state, "publish", GRAPH)
    assert result.next_node == "publish" and result.overrides == []


def test_publish_with_nothing_at_all_chains_back_to_researcher() -> None:
    result = enforce_invariants(make_state(), "publish", GRAPH)
    assert result.next_node == "researcher"
    assert [o.guard for o in result.overrides] == [
        "publish_requires_approval",
        "human_gate_requires_artifact",
        "coder_requires_sources",
    ]


# --- Invariant 2: no coder before at least one source (unless code-only) --------------------


def test_coder_without_sources_is_redirected_to_researcher() -> None:
    result = enforce_invariants(make_state(), "coder", GRAPH)
    assert result.next_node == "researcher"
    assert [o.guard for o in result.overrides] == ["coder_requires_sources"]


def test_coder_with_sources_is_allowed() -> None:
    assert enforce_invariants(make_state(sources=[SOURCE]), "coder", GRAPH).next_node == "coder"


def test_code_only_task_may_go_to_coder_without_sources() -> None:
    state = make_state(task_kind="code_only")
    assert enforce_invariants(state, "coder", GRAPH).next_node == "coder"


# --- Invariant 3: under 10% budget -> final reviewer summary, then END ------------------------


def low(tokens_left: int = 100_000, usd_left: float = 1.0) -> Budget:
    return make_budget().model_copy(
        update={"tokens_remaining": tokens_left, "usd_remaining": usd_left}
    )


@pytest.mark.parametrize("budget", [low(tokens_left=9_999), low(usd_left=0.099)])
def test_low_budget_forces_final_reviewer_summary(budget: Budget) -> None:
    state = make_state(sources=[SOURCE], budget=budget)
    result = enforce_invariants(state, "coder", GRAPH)
    assert result.next_node == "reviewer"
    assert result.overrides[0].guard == "budget_low"
    [op] = result.system_ops
    assert op.path == "termination"
    assert op.value["budget_exhausted"] is True and op.value["reason"] == "budget_exhausted"


def test_low_budget_after_final_summary_forces_end() -> None:
    state = make_state(
        budget=low(tokens_left=5_000),
        termination={
            "budget_exhausted": True,
            "final_summary_done": True,
            "reason": "budget_exhausted",
        },
    )
    assert enforce_invariants(state, "coder", GRAPH).next_node == END


def test_budget_at_exactly_ten_percent_is_not_low() -> None:
    state = make_state(sources=[SOURCE], budget=low(tokens_left=10_000, usd_left=0.1))
    assert enforce_invariants(state, "coder", GRAPH).next_node == "coder"


def test_low_budget_overrides_even_an_approved_publish() -> None:
    state = make_state(
        sources=[SOURCE], artifacts=[memo(1)], hitl=approved(1), budget=low(tokens_left=1_000)
    )
    assert enforce_invariants(state, "publish", GRAPH).next_node == "reviewer"


# --- Invariant 4: zero budget -> END with the partial artifact --------------------------------


@pytest.mark.parametrize("budget", [low(tokens_left=0), low(usd_left=0.0)])
def test_zero_budget_forces_end(budget: Budget) -> None:
    state = make_state(sources=[SOURCE], artifacts=[memo(1)], budget=budget)
    result = enforce_invariants(state, "reviewer", GRAPH)
    assert result.next_node == END
    [op] = result.system_ops
    assert op.value["reason"] == "budget_zero" and op.value["budget_exhausted"] is True


# --- step cap ---------------------------------------------------------------------------------


def test_max_steps_forces_end() -> None:
    state = make_state(step=29, max_steps=30, sources=[SOURCE])
    result = enforce_invariants(state, "coder", GRAPH)
    assert result.next_node == END and result.system_ops[0].value["reason"] == "max_steps"


def test_supervisor_choosing_end_records_the_reason() -> None:
    result = enforce_invariants(make_state(), END, GRAPH)
    assert result.next_node == END
    assert result.system_ops[0].value["reason"] == "supervisor_finished"


def test_guards_never_produce_a_node_outside_the_graph() -> None:
    from orchestrator.graphs.spec import GraphSpec

    tiny = GraphSpec(
        "tiny",
        1,
        "",
        "supervisor",
        ("supervisor", "coder"),
        edges=(("coder", "supervisor"),),
        routes=(("supervisor", ("coder", END)),),
    )
    with pytest.raises(InvariantViolation):
        enforce_invariants(make_state(), "coder", tiny)  # would need "researcher"


# --- review before rewrite (found on a live run: supervisor looped on "coder") ---------------


def test_unreviewed_draft_goes_to_reviewer_not_back_to_coder() -> None:
    state = make_state(sources=[SOURCE], artifacts=[memo(2)])
    result = enforce_invariants(state, "coder", GRAPH)
    assert result.next_node == "reviewer"
    assert [o.guard for o in result.overrides] == ["review_before_rewrite"]


def test_coder_allowed_after_review_of_current_version() -> None:
    verdict = {
        "id": "n1",
        "author": "reviewer",
        "kind": "verdict",
        "verdict": "changes_requested",
        "text": "fix",
        "artifact_id": "memo",
        "artifact_version": 2,
    }
    state = make_state(sources=[SOURCE], artifacts=[memo(2)], review_notes=[verdict])
    assert enforce_invariants(state, "coder", GRAPH).next_node == "coder"


def test_reviewer_before_any_draft_goes_to_the_coder() -> None:
    state = make_state(sources=[SOURCE])
    result = enforce_invariants(state, "reviewer", GRAPH)
    assert result.next_node == "coder"
    assert [o.guard for o in result.overrides] == ["reviewer_requires_draft"]


def test_reviewer_before_any_source_chains_to_researcher() -> None:
    assert enforce_invariants(make_state(), "reviewer", GRAPH).next_node == "researcher"
