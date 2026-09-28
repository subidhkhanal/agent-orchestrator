"""Node implementations, looked up by node name when a graph is built."""

from orchestrator.agents.hitl import await_approval_node, human_gate_node, publish_node
from orchestrator.agents.supervisor import supervisor_node
from orchestrator.agents.workers import (
    coder_node,
    researcher_node,
    reviewer_node,
    single_agent_node,
)
from orchestrator.engine.context import NodeFn

NODE_LIBRARY: dict[str, NodeFn] = {
    "supervisor": supervisor_node,
    "researcher": researcher_node,
    "coder": coder_node,
    "reviewer": reviewer_node,
    "human_gate": human_gate_node,
    "await_approval": await_approval_node,
    "publish": publish_node,
    "single_agent": single_agent_node,
}

__all__ = ["NODE_LIBRARY"]
