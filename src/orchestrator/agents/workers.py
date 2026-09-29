"""The three workers: researcher, coder, reviewer.

They share one ReAct-style loop: call the model with the role's tool specs, execute tool
calls through the role's ToolRouter, feed results back, and stop when the model answers
without tool calls (or the round limit or the budget is hit). Tool results carry patch ops;
the loop applies them to a working copy of the state as it goes, so later tool calls see
earlier results and a permission problem surfaces at the call that caused it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ValidationError

from orchestrator.agents.prompts import (
    CODER_SYSTEM,
    FINAL_SUMMARY_SYSTEM,
    RESEARCHER_SYSTEM,
    REVIEWER_SYSTEM,
    SINGLE_AGENT_SYSTEM,
    render_view,
    state_view,
)
from orchestrator.citations import check_memo
from orchestrator.engine.context import NodeContext, NodeOutput
from orchestrator.gateway.budget import BudgetExhausted, DeadlineExceeded
from orchestrator.gateway.types import LLMRequest, Message, ToolCall
from orchestrator.state.models import ReviewNote, RunState
from orchestrator.state.patch import AppendOp, PatchOp, SetOp, StatePatch
from orchestrator.state.reducer import apply_patch

# Tool results shown to the model are truncated; full results still land in state.
TOOL_RESULT_CHARS = 1500
# Tool results older than the last few are shortened before each call ("context compaction");
# their content already lives in state (sources, artifacts), so the model loses little.
KEEP_RECENT_TOOL_RESULTS = 3
COMPACTED_CHARS = 200


def compact(messages: list[Message]) -> tuple[Message, ...]:
    tool_positions = [i for i, m in enumerate(messages) if m.role == "tool"]
    old = set(tool_positions[:-KEEP_RECENT_TOOL_RESULTS])
    return tuple(
        m.model_copy(update={"content": m.content[:COMPACTED_CHARS] + " ...[truncated]"})
        if i in old and len(m.content) > COMPACTED_CHARS
        else m
        for i, m in enumerate(messages)
    )


@dataclass
class LoopResult:
    state: RunState
    ops: list[PatchOp] = field(default_factory=list)
    final: str | None = None
    stopped: Literal["answered", "max_rounds", "budget"] = "answered"


async def run_tool_loop(
    role: str,
    state: RunState,
    ctx: NodeContext,
    system: str,
    user: str,
    max_rounds: int | None = None,
    stop_after: frozenset[str] = frozenset(),
    seed: LoopResult | None = None,
) -> LoopResult:
    """`stop_after`: end the loop right after one of these tools succeeds (its job is done).
    `seed`: state and ops already produced by code before the model runs."""
    router = ctx.deps.tools.router_for(role)
    result = seed or LoopResult(state=state)
    state = result.state
    messages: list[Message] = [
        Message(role="system", content=system),
        Message(role="user", content=user),
    ]
    for round_ in range(max_rounds or ctx.deps.settings.max_tool_rounds):
        request = LLMRequest(
            node=role,
            messages=compact(messages),
            tools=router.specs(),
            metadata={"view": state_view(result.state), "round": round_, "role": role},
        )
        try:
            response = await ctx.llm(request)
        except (BudgetExhausted, DeadlineExceeded):
            result.stopped = "budget"
            return result
        if not response.tool_calls:
            result.final = response.content
            return result

        messages.append(
            Message(role="assistant", content=response.content, tool_calls=response.tool_calls)
        )
        for call in response.tool_calls:
            outcome = await router.invoke(call, ctx.tool_context(result.state))
            if outcome.ops:
                patch = StatePatch(
                    base_version=result.state.state_version, author=role, ops=tuple(outcome.ops)
                )
                result.state = apply_patch(result.state, patch)
                result.ops.extend(outcome.ops)
            messages.append(
                Message(
                    role="tool",
                    content=outcome.for_model()[:TOOL_RESULT_CHARS],
                    tool_call_id=call.id,
                )
            )
            if call.name in stop_after and outcome.status == "ok":
                result.stopped = "answered"
                return result
    result.stopped = "max_rounds"
    return result


async def _memo_text(state: RunState, ctx: NodeContext) -> str | None:
    artifact = state.current_artifact()
    if artifact is None:
        return None
    return await ctx.deps.services.artifacts.get(artifact.content_ref, tenant_id=state.tenant_id)


# --- researcher ------------------------------------------------------------------------------


async def _seed_internal_sources(state: RunState, ctx: NodeContext) -> LoopResult:
    """Query the internal knowledge base with the task before the model starts.

    Whether internal documents are consulted should not depend on the model remembering to
    call rag_query (a fast model skipped it on a live run and only searched the web). The call
    goes through the researcher's own ToolRouter, so allowlists and logging still apply.
    """
    seed = LoopResult(state=state)
    if state.sources:  # later research rounds refine; the first round seeds
        return seed
    router = ctx.deps.tools.router_for("researcher")
    call = ToolCall(id="seed_rag", name="rag_query", arguments={"question": state.task[:500]})
    outcome = await router.invoke(call, ctx.tool_context(state))
    if outcome.ops:
        patch = StatePatch(
            base_version=state.state_version, author="researcher", ops=tuple(outcome.ops)
        )
        seed.state = apply_patch(state, patch)
        seed.ops.extend(outcome.ops)
    return seed


async def researcher_node(state: RunState, ctx: NodeContext) -> NodeOutput:
    seed = await _seed_internal_sources(state, ctx)
    loop = await run_tool_loop(
        "researcher",
        state,
        ctx,
        RESEARCHER_SYSTEM,
        render_view(state_view(seed.state)),
        seed=seed,
    )
    ops = list(loop.ops)
    if loop.final:
        ops.append(SetOp(path="research_summary", value=loop.final.strip()))
    if not loop.state.sources:
        ops.append(AppendOp(path="open_questions", value=f"No sources found for: {state.task}"))
    return NodeOutput(
        ops=ops, summary={"stopped": loop.stopped, "sources": len(loop.state.sources)}
    )


# --- coder -----------------------------------------------------------------------------------


async def coder_node(state: RunState, ctx: NodeContext) -> NodeOutput:
    memo = await _memo_text(state, ctx)
    loop = await run_tool_loop(
        "coder",
        state,
        ctx,
        CODER_SYSTEM,
        render_view(state_view(state, memo=memo)),
        stop_after=frozenset({"write_artifact", "edit_section"}),
    )
    artifact = loop.state.current_artifact()
    return NodeOutput(
        ops=loop.ops,
        summary={
            "stopped": loop.stopped,
            "artifact_version": artifact.version if artifact else None,
        },
    )


# --- reviewer --------------------------------------------------------------------------------


class ReviewVerdict(BaseModel):
    verdict: Literal["pass", "changes_requested"]
    summary: str = ""


def _parse_verdict(raw: str | None) -> ReviewVerdict | None:
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return ReviewVerdict.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError):
        return None


def _note(ctx: NodeContext, state: RunState, **fields: object) -> AppendOp:
    artifact = state.current_artifact()
    note = ReviewNote.model_validate(
        {
            "id": ctx.new_id("note"),
            "author": "reviewer",
            "artifact_id": artifact.id if artifact else None,
            "artifact_version": artifact.version if artifact else None,
            **fields,
        }
    )
    return AppendOp(path="review_notes", value=note.model_dump())


async def _final_summary(state: RunState, ctx: NodeContext) -> NodeOutput:
    """Budget-exhausted mode: one short summary call, no tools, then the run ends."""
    request = LLMRequest(
        node="reviewer",
        messages=(
            Message(role="system", content=FINAL_SUMMARY_SYSTEM),
            Message(role="user", content=render_view(state_view(state))),
        ),
        metadata={"view": state_view(state), "role": "reviewer", "mode": "final_summary"},
    )
    try:
        text = (await ctx.llm(request)).content.strip()
    except (BudgetExhausted, DeadlineExceeded):
        text = "Budget exhausted before a final summary could be written."
    done = SetOp(
        path="termination",
        value=state.termination.model_copy(update={"final_summary_done": True}).model_dump(),
    )
    return NodeOutput(
        ops=[_note(ctx, state, kind="final_summary", text=text or "(empty summary)")],
        system_ops=[done],
        summary={"mode": "final_summary"},
    )


async def reviewer_node(state: RunState, ctx: NodeContext) -> NodeOutput:
    if state.termination.budget_exhausted and not state.termination.final_summary_done:
        return await _final_summary(state, ctx)

    memo = await _memo_text(state, ctx)
    if memo is None:
        return NodeOutput(ops=[_note(ctx, state, kind="general", text="No memo to review yet.")])

    loop = await run_tool_loop(
        "reviewer", state, ctx, REVIEWER_SYSTEM, render_view(state_view(state, memo=memo))
    )
    ops = list(loop.ops)
    verdict = _parse_verdict(loop.final) or ReviewVerdict(
        verdict="changes_requested", summary="Reviewer did not return a valid verdict."
    )

    # Code-level backstop: a "pass" cannot stand if the deterministic citation check fails.
    report = check_memo(memo, state.source_ids())
    if verdict.verdict == "pass" and not report.ok:
        verdict = ReviewVerdict(
            verdict="changes_requested",
            summary=f"Automatic check found {len(report.findings)} issue(s); "
            f"model verdict was 'pass'. {verdict.summary}".strip(),
        )
        for finding in report.findings[:10]:
            kind = "policy" if finding.kind == "policy" else "unsupported_claim"
            ops.append(_note(ctx, state, kind=kind, text=f"line {finding.line}: {finding.detail}"))

    ops.append(_note(ctx, state, kind="verdict", verdict=verdict.verdict, text=verdict.summary))
    return NodeOutput(
        ops=ops,
        summary={
            "verdict": verdict.verdict,
            "citation_validity": round(report.validity, 3),
            "stopped": loop.stopped,
        },
    )


# --- single-agent baseline (evaluation only) -------------------------------------------------

SINGLE_AGENT_MAX_ROUNDS = 20


async def single_agent_node(state: RunState, ctx: NodeContext) -> NodeOutput:
    """One ReAct agent with every worker tool and the whole run budget.

    It is the eval baseline for research-memo: same tools, same budget, same model tier as
    the workers, but no supervisor, no separate reviewer and no code-level review backstop.
    """
    loop = await run_tool_loop(
        "single_agent",
        state,
        ctx,
        SINGLE_AGENT_SYSTEM,
        render_view(state_view(state)),
        max_rounds=SINGLE_AGENT_MAX_ROUNDS,
    )
    ops = list(loop.ops)
    if loop.final:
        ops.append(SetOp(path="research_summary", value=loop.final.strip()))
    exhausted = loop.stopped == "budget"
    done = SetOp(
        path="termination",
        value=state.termination.model_copy(
            update={
                "reason": "budget_exhausted" if exhausted else "supervisor_finished",
                "budget_exhausted": exhausted,
            }
        ).model_dump(),
    )
    artifact = loop.state.current_artifact()
    return NodeOutput(
        ops=ops,
        system_ops=[done],
        summary={
            "stopped": loop.stopped,
            "artifact_version": artifact.version if artifact else None,
        },
    )
