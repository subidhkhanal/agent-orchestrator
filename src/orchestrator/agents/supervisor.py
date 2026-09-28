"""The supervisor: an LLM router with no tools.

Flow per invocation:
1. If a forced guard applies (budget, step cap), route without calling the model.
2. Ask the model for {"next_node", "reason", "state_patch"}; validate it against the pinned
   graph version's allowed targets. On invalid output, retry with the validation error in
   the prompt. Three invalid outputs in a row fail the run.
3. Run the route guards on the valid proposal, and route to whatever they allow.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orchestrator.agents.guards import (
    RouteResult,
    budget_wind_down,
    enforce_invariants,
    forced_route,
    termination_for,
)
from orchestrator.agents.prompts import SUPERVISOR_SYSTEM, render_view, state_view
from orchestrator.engine.context import NodeContext, NodeOutput
from orchestrator.events.log import EventType
from orchestrator.gateway.budget import BudgetExhausted, DeadlineExceeded
from orchestrator.gateway.types import LLMRequest, Message
from orchestrator.graphs.spec import END
from orchestrator.state.models import RunState
from orchestrator.state.patch import AppendOp, PatchOp


class SupervisorRoutingError(Exception):
    """The supervisor produced invalid routing output too many times in a row."""


class SupervisorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_questions: list[str] = Field(default_factory=list, max_length=5)
    constraints: list[str] = Field(default_factory=list, max_length=5)


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next_node: str
    reason: str = Field(min_length=1, max_length=1000)
    state_patch: SupervisorPatch = Field(default_factory=SupervisorPatch)


class RouteValidationError(ValueError):
    pass


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_route(raw: str, allowed: tuple[str, ...]) -> RouteDecision:
    text = _FENCE.sub("", raw.strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RouteValidationError(f"output is not valid JSON ({exc.msg})") from None
    try:
        decision = RouteDecision.model_validate(data)
    except ValidationError as exc:
        raise RouteValidationError(
            "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        ) from None
    if decision.next_node not in allowed:
        raise RouteValidationError(
            f"next_node {decision.next_node!r} is not one of {list(allowed)}"
        )
    return decision


def response_schema(allowed: tuple[str, ...]) -> dict[str, Any]:
    schema = RouteDecision.model_json_schema()
    schema["properties"]["next_node"] = {"type": "string", "enum": list(allowed)}
    return schema


async def _routed(
    ctx: NodeContext,
    proposed: str | None,
    reason: str,
    result: RouteResult,
    extra_ops: list[PatchOp] | None = None,
) -> NodeOutput:
    for o in result.overrides:
        await ctx.emit(
            EventType.GUARD_OVERRIDE,
            {"guard": o.guard, "proposed": o.proposed, "forced": o.forced, "detail": o.detail},
        )
    await ctx.emit(
        EventType.ROUTE_DECIDED,
        {
            "proposed": proposed,
            "next_node": result.next_node,
            "reason": reason,
            "overridden": bool(result.overrides),
        },
    )
    return NodeOutput(
        ops=extra_ops or [],
        system_ops=result.system_ops,
        goto=result.next_node,
        summary={"reason": reason, "proposed": proposed},
    )


async def supervisor_node(state: RunState, ctx: NodeContext) -> NodeOutput:
    forced = forced_route(state, ctx.graph)
    if forced is not None:
        return await _routed(ctx, None, forced.overrides[0].detail, forced)

    allowed = ctx.graph.targets("supervisor")
    # The router needs to know what exists, not what it says: titles only, no snippets.
    view = state_view(state, snippets=False)
    messages: list[Message] = [
        Message(role="system", content=SUPERVISOR_SYSTEM),
        Message(
            role="user",
            content=f"Allowed next_node values: {list(allowed)}\n\n" + render_view(view),
        ),
    ]
    decision: RouteDecision | None = None
    max_attempts = ctx.deps.settings.max_route_attempts
    for attempt in range(1, max_attempts + 1):
        request = LLMRequest(
            node="supervisor",
            messages=tuple(messages),
            response_schema=response_schema(allowed),
            metadata={"view": view, "allowed": list(allowed), "attempt": attempt},
        )
        try:
            response = await ctx.llm(request)
        except BudgetExhausted as exc:
            # Too little left to even route: wind down exactly like the <10% guard.
            result = budget_wind_down(state, ctx.graph, f"cannot afford a routing call ({exc})")
            return await _routed(ctx, None, result.overrides[0].detail, result)
        except DeadlineExceeded as exc:
            result = RouteResult(END, system_ops=[termination_for(state, "deadline_exceeded")])
            return await _routed(ctx, None, f"run deadline passed: {exc}", result)
        try:
            decision = parse_route(response.content, allowed)
            break
        except RouteValidationError as exc:
            await ctx.emit(
                EventType.ROUTE_INVALID,
                {"attempt": attempt, "error": str(exc), "raw": response.content[:500]},
            )
            messages += [
                Message(role="assistant", content=response.content),
                Message(
                    role="user",
                    content=f"Your routing output was invalid: {exc}. "
                    f"Reply with JSON only. next_node must be one of {list(allowed)}.",
                ),
            ]
    if decision is None:
        raise SupervisorRoutingError(f"{max_attempts} consecutive invalid routing outputs")

    result = enforce_invariants(state, decision.next_node, ctx.graph)
    ops: list[PatchOp] = [
        *(AppendOp(path="open_questions", value=q) for q in decision.state_patch.open_questions),
        *(AppendOp(path="constraints", value=c) for c in decision.state_patch.constraints),
    ]
    return await _routed(ctx, decision.next_node, decision.reason, result, ops)
