"""Role-scoped tool routing (least privilege).

Each worker gets a ToolRouter bound to its role. The router is the only way to execute a tool,
and it checks the allowlist on every call. Offering a role only its own tool specs is not
enough on its own: a model can still emit a call to a tool it was never shown, so the check
has to happen at execution time too.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from orchestrator.events.log import EventType
from orchestrator.gateway.types import ToolCall, ToolSpec
from orchestrator.state.patch import PatchOp
from orchestrator.tools.base import Tool, ToolContext, ToolError

_RESEARCHER = ("web_search", "rag_query", "summarize")
_CODER = ("write_artifact", "edit_section", "spawn_sandbox")
_REVIEWER = ("policy_check", "redline", "add_review_note")

ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "supervisor": (),
    "researcher": _RESEARCHER,
    "coder": _CODER,
    "reviewer": _REVIEWER,
    # Eval baseline: one agent with the union of the workers' tools.
    "single_agent": _RESEARCHER + _CODER + _REVIEWER,
}


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool]) -> None:  # type: ignore[type-arg]
        self._tools: dict[str, Tool] = {}  # type: ignore[type-arg]
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool {tool.name!r}")
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:  # type: ignore[type-arg]
        return self._tools.get(name)

    def names(self) -> set[str]:
        return set(self._tools)

    def router_for(self, role: str) -> ToolRouter:
        allowed = ROLE_TOOLS.get(role)
        if allowed is None:
            raise KeyError(f"no tool allowlist for role {role!r}")
        unknown = set(allowed) - self.names()
        if unknown:
            raise ValueError(f"allowlist for {role!r} names unregistered tools {sorted(unknown)}")
        return ToolRouter(self, role, frozenset(allowed))


@dataclass
class ToolOutcome:
    status: Literal["ok", "denied", "invalid_args", "error"]
    output: Any
    ops: list[PatchOp] = field(default_factory=list)

    def for_model(self) -> str:
        if self.status == "ok":
            return json.dumps(self.output, default=str)
        return json.dumps({"error": self.status, "detail": self.output})


class ToolRouter:
    def __init__(self, registry: ToolRegistry, role: str, allowed: frozenset[str]) -> None:
        self._registry = registry
        self.role = role
        self.allowed = allowed

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            tool.spec()
            for name in sorted(self.allowed)
            if (tool := self._registry.get(name)) is not None
        )

    async def invoke(self, call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        tool = self._registry.get(call.name) if call.name in self.allowed else None
        if tool is None:
            await ctx.emit(
                EventType.TOOL_DENIED,
                {"role": self.role, "tool": call.name, "arguments": call.arguments},
            )
            return ToolOutcome("denied", f"tool {call.name!r} is not available to {self.role}")

        try:
            args = tool.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            outcome = ToolOutcome("invalid_args", exc.errors(include_url=False))
        else:
            try:
                result = await tool.handler(ctx, args)
                outcome = ToolOutcome("ok", result.output, list(result.ops))
            except ToolError as exc:
                outcome = ToolOutcome("error", str(exc))

        await ctx.emit(
            EventType.TOOL_CALL,
            {
                "role": self.role,
                "tool": call.name,
                "status": outcome.status,
                "ops": len(outcome.ops),
            },
        )
        return outcome
