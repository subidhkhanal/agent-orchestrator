from __future__ import annotations

import dataclasses

import pytest

from orchestrator.events import EventType
from orchestrator.graphs.definitions import QUICK_ANSWER_V1, RESEARCH_MEMO_V1, default_registry
from orchestrator.graphs.spec import END, GraphRegistry, GraphSpec
from orchestrator.offline import build_offline
from tests.factories import make_budget


def test_topology_hash_is_stable_and_order_independent() -> None:
    shuffled = dataclasses.replace(
        RESEARCH_MEMO_V1,
        nodes=tuple(reversed(RESEARCH_MEMO_V1.nodes)),
        edges=tuple(reversed(RESEARCH_MEMO_V1.edges)),
    )
    assert shuffled.topology_hash() == RESEARCH_MEMO_V1.topology_hash()
    assert RESEARCH_MEMO_V1.topology_hash() != QUICK_ANSWER_V1.topology_hash()


def test_a_registered_version_cannot_change_topology() -> None:
    registry = GraphRegistry()
    registry.register(RESEARCH_MEMO_V1)
    registry.register(RESEARCH_MEMO_V1)  # identical re-registration is fine
    changed = dataclasses.replace(
        RESEARCH_MEMO_V1,
        routes=(
            ("supervisor", ("researcher", "coder", "reviewer", "human_gate", END)),
            ("await_approval", ("publish", "coder")),
        ),
    )
    with pytest.raises(ValueError, match="new version"):
        registry.register(changed)
    registry.register(dataclasses.replace(changed, version=2))
    assert registry.get("research-memo").version == 2
    assert registry.get("research-memo", 1) is RESEARCH_MEMO_V1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"entry": "ghost"},
        {"edges": (("a", "ghost"),)},
        {"edges": (("a", END),), "routes": (("a", (END,)),)},  # both static and dynamic
        {"edges": ()},  # node with no way out
    ],
)
def test_invalid_specs_are_rejected(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {
        "graph_id": "g",
        "version": 1,
        "description": "",
        "entry": "a",
        "nodes": ("a",),
        "edges": (("a", END),),
    }
    with pytest.raises(ValueError):
        GraphSpec(**{**base, **kwargs})  # type: ignore[arg-type]


def test_supervisor_routes_match_the_design() -> None:
    assert set(RESEARCH_MEMO_V1.targets("supervisor")) == {
        "researcher",
        "coder",
        "reviewer",
        "human_gate",
        "publish",
        END,
    }


def test_registry_lists_both_graphs() -> None:
    assert [(s.graph_id, s.version) for s in default_registry().all()] == [
        ("quick-answer", 1),
        ("research-memo", 1),
        ("single-agent", 1),
    ]


async def test_quick_answer_runs_with_no_supervisor_and_pins_its_version() -> None:
    runner, deps = build_offline()
    outcome = await runner.start(
        run_id="q1",
        tenant_id="t",
        graph_id="quick-answer",
        task="What is happening with grid storage?",
        budget=make_budget(),
    )
    assert outcome.status == "completed"
    assert (outcome.state.graph_id, outcome.state.graph_version) == ("quick-answer", 1)
    assert outcome.state.sources and outcome.state.research_summary
    nodes = [e.payload["node"] for e in deps.events.of_type("q1", EventType.NODE_STARTED)]  # type: ignore[attr-defined]
    assert nodes == ["researcher"]
    created = deps.events.of_type("q1", EventType.RUN_CREATED)[0].payload  # type: ignore[attr-defined]
    assert created["topology_hash"] == QUICK_ANSWER_V1.topology_hash()


def test_mermaid_rendering_mentions_every_node() -> None:
    diagram = RESEARCH_MEMO_V1.mermaid()
    assert all(node in diagram for node in RESEARCH_MEMO_V1.nodes)
