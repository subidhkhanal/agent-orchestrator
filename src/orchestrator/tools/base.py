"""Tool abstractions.

A tool never mutates run state. It returns an output (shown to the model) and optionally a list
of patch ops, which the worker folds into its patch. The reducer then checks that the calling
role is allowed to write those fields, so a tool cannot widen a role's write access either.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from orchestrator.events.emitter import Emitter
from orchestrator.gateway.budget import BudgetLedger
from orchestrator.gateway.types import LLMResponse, Message, ToolSpec
from orchestrator.state.models import RunState
from orchestrator.state.patch import PatchOp
from orchestrator.tools.services import ToolServices


class ToolError(Exception):
    """An expected tool failure. Reported back to the model instead of failing the run."""


@dataclass
class ToolContext:
    """What a tool can see and use during one node execution."""

    run_id: str
    tenant_id: str
    node: str
    # The node's working view: the input state plus ops already produced in this node.
    state: RunState
    services: ToolServices
    emit: Emitter
    ledger: BudgetLedger
    # Makes a run-unique, deterministic id (stable if the node re-executes).
    new_id: Callable[[str], str]
    # Calls the LLM gateway under this node's budget (used by e.g. summarize).
    llm: Callable[[tuple[Message, ...]], Awaitable[LLMResponse]]


@dataclass
class ToolResult:
    output: Any
    ops: list[PatchOp] = field(default_factory=list)


@dataclass(frozen=True)
class Tool[ArgsT: BaseModel]:
    name: str
    description: str
    args_model: type[ArgsT]
    handler: Callable[[ToolContext, ArgsT], Awaitable[ToolResult]]

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )
