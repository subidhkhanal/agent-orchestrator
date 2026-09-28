"""Human-in-the-loop gate and publish.

The gate is two nodes on purpose:
- human_gate writes the pending approval (gate id + artifact version) into state. That patch
  is checkpointed before the run pauses, so the approve/reject API can validate a decision
  against the checkpointed gate.
- await_approval calls interrupt(). On resume LangGraph re-executes this node from the top;
  it has no side effects before interrupt(), so re-execution is harmless.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.agents.guards import InvariantViolation, publish_allowed
from orchestrator.effects.publisher import PublishRequest, effect_key
from orchestrator.engine.context import NodeContext, NodeOutput
from orchestrator.events.log import EventLog, EventType
from orchestrator.state.models import HitlState, Publication, ReviewNote, RunState
from orchestrator.state.patch import AppendOp, PatchOp, SetOp
from orchestrator.tools.services import ArtifactStore

PREVIEW_CHARS = 4000


class HitlDecision(BaseModel):
    """The value passed to Command(resume=...) by the approve/reject endpoints."""

    model_config = ConfigDict(extra="forbid")

    gate_id: str
    decision: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1)
    comment: str | None = Field(default=None, max_length=4000)


def gate_id_for(artifact_id: str, version: int) -> str:
    return f"gate_{artifact_id}_v{version}"


async def human_gate_node(state: RunState, ctx: NodeContext) -> NodeOutput:
    artifact = state.current_artifact()
    if artifact is None:
        raise InvariantViolation("human_gate reached without an artifact")
    gate_id = gate_id_for(artifact.id, artifact.version)
    # No event here: the gate is announced (hitl_required) by the runner once the run has
    # actually paused, so a client never sees an approval request it cannot act on yet.
    pending = HitlState(
        pending=True, gate_id=gate_id, artifact_id=artifact.id, artifact_version=artifact.version
    )
    return NodeOutput(ops=[SetOp(path="hitl", value=pending.model_dump())])


async def gate_announcement(
    artifacts: ArtifactStore, state: RunState, gate: dict[str, Any]
) -> dict[str, Any]:
    """Payload of the hitl_required event: the gate plus an artifact preview."""
    artifact = state.current_artifact()
    preview = ""
    if artifact is not None:
        content = await artifacts.get(artifact.content_ref, tenant_id=state.tenant_id)
        preview = content[:PREVIEW_CHARS]
    return {**gate, "preview": preview, "step": state.step, "node": "await_approval"}


async def announce_gate(
    events: EventLog, artifacts: ArtifactStore, state: RunState, gate: dict[str, Any]
) -> None:
    """Emit hitl_required. Keyed by gate id, so announcing the same gate twice is a no-op."""
    await events.append(
        state.run_id,
        EventType.HITL_REQUIRED,
        await gate_announcement(artifacts, state, gate),
        f"hitl_required:{gate['gate_id']}",
    )


async def await_approval_node(state: RunState, ctx: NodeContext) -> NodeOutput:
    hitl = state.hitl
    if not hitl.pending or hitl.gate_id is None:
        raise InvariantViolation("await_approval reached without a pending gate")

    raw = interrupt(
        {
            "gate_id": hitl.gate_id,
            "artifact_id": hitl.artifact_id,
            "artifact_version": hitl.artifact_version,
        }
    )
    decision = HitlDecision.model_validate(raw)
    if decision.gate_id != hitl.gate_id:
        # The API checks this before resuming; this is the second line of defense.
        raise InvariantViolation(
            f"decision for gate {decision.gate_id!r} but {hitl.gate_id!r} is pending"
        )

    decided = hitl.model_copy(
        update={"pending": False, "decision": decision.decision, "comment": decision.comment}
    )
    ops: list[PatchOp] = [SetOp(path="hitl", value=decided.model_dump())]
    if decision.decision == "rejected":
        note = ReviewNote(
            id=ctx.new_id("note"),
            author=f"human:{decision.reviewer}",
            kind="human_rejection",
            text=decision.comment or "Rejected without a comment.",
            artifact_id=hitl.artifact_id,
            artifact_version=hitl.artifact_version,
        )
        ops.append(AppendOp(path="review_notes", value=note.model_dump()))
    return NodeOutput(
        ops=ops,
        goto="publish" if decision.decision == "approved" else "coder",
        summary={"gate_id": hitl.gate_id, "decision": decision.decision},
    )


async def publish_node(state: RunState, ctx: NodeContext) -> NodeOutput:
    # Same invariant the supervisor guard enforces, checked again at the point of the effect.
    if not publish_allowed(state):
        raise InvariantViolation("publish reached without approval of the current artifact")
    artifact = state.current_artifact()
    assert artifact is not None and state.hitl.gate_id is not None

    key = effect_key(state.run_id, state.hitl.gate_id, artifact.version)
    content = await ctx.deps.services.artifacts.get(artifact.content_ref, tenant_id=state.tenant_id)
    receipt = await ctx.deps.publisher.publish(
        PublishRequest(
            effect_key=key,
            tenant_id=state.tenant_id,
            run_id=state.run_id,
            artifact_id=artifact.id,
            artifact_version=artifact.version,
            content=content,
        )
    )
    await ctx.emit(
        EventType.EFFECT_RECORDED,
        {"effect_key": key, "sink_ref": receipt.sink_ref, "duplicate": receipt.duplicate},
    )
    publication = Publication(
        effect_key=key,
        sink_ref=receipt.sink_ref,
        artifact_id=artifact.id,
        artifact_version=artifact.version,
    )
    done = SetOp(
        path="termination",
        value=state.termination.model_copy(update={"reason": "published"}).model_dump(),
    )
    return NodeOutput(
        ops=[SetOp(path="publication", value=publication.model_dump())],
        system_ops=[done],
        summary={"effect_key": key, "duplicate": receipt.duplicate},
    )
