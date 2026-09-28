"""The graphs this platform ships with."""

from __future__ import annotations

from orchestrator.graphs.spec import END, GraphRegistry, GraphSpec

RESEARCH_MEMO_V1 = GraphSpec(
    graph_id="research-memo",
    version=1,
    description="Supervisor routes between researcher, coder and reviewer; a human approves "
    "the memo before it is published.",
    entry="supervisor",
    nodes=(
        "supervisor",
        "researcher",
        "coder",
        "reviewer",
        "human_gate",
        "await_approval",
        "publish",
    ),
    edges=(
        ("researcher", "supervisor"),
        ("coder", "supervisor"),
        ("reviewer", "supervisor"),
        # human_gate records the pending approval in state (and so in the checkpoint) before
        # await_approval pauses the run with interrupt().
        ("human_gate", "await_approval"),
        ("publish", END),
    ),
    routes=(
        ("supervisor", ("researcher", "coder", "reviewer", "human_gate", "publish", END)),
        ("await_approval", ("publish", "coder")),
    ),
)

QUICK_ANSWER_V1 = GraphSpec(
    graph_id="quick-answer",
    version=1,
    description="A single researcher step that answers from sources. No supervisor.",
    entry="researcher",
    nodes=("researcher",),
    edges=(("researcher", END),),
)


SINGLE_AGENT_V1 = GraphSpec(
    graph_id="single-agent",
    version=1,
    description="Eval baseline: one agent with all worker tools and the same budget as "
    "research-memo. No supervisor, no human gate.",
    entry="single_agent",
    nodes=("single_agent",),
    edges=(("single_agent", END),),
)


def default_registry() -> GraphRegistry:
    registry = GraphRegistry()
    registry.register(RESEARCH_MEMO_V1)
    registry.register(QUICK_ANSWER_V1)
    registry.register(SINGLE_AGENT_V1)
    return registry
