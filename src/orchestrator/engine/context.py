"""What a node function receives and returns."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from orchestrator.clock import Clock
from orchestrator.effects.publisher import Publisher
from orchestrator.events.emitter import NodeEmitter
from orchestrator.events.log import EventLog
from orchestrator.gateway.budget import BudgetLedger
from orchestrator.gateway.gateway import LLMGateway
from orchestrator.gateway.types import LLMRequest, LLMResponse, Message
from orchestrator.graphs.spec import GraphSpec
from orchestrator.state.models import RunState
from orchestrator.state.patch import PatchOp
from orchestrator.tools.base import ToolContext
from orchestrator.tools.router import ToolRegistry
from orchestrator.tools.services import ToolServices


@dataclass(frozen=True)
class EngineSettings:
    checkpoint_event_every_n: int = 5
    max_tool_rounds: int = 6
    max_route_attempts: int = 3


class StateStore(Protocol):
    """Durable copy of the latest RunState (the Postgres implementation is in db/runs.py)."""

    async def commit(
        self, run_id: str, attempt: int, base_version: int, new_state: RunState
    ) -> None:
        """Persist new_state if the stored version is still base_version and the caller still
        holds the lease (attempt). Raises StalePatchError or LeaseLost otherwise."""
        ...


@dataclass
class EngineDeps:
    """Everything the engine needs, injected once per process."""

    gateway: LLMGateway
    tools: ToolRegistry
    services: ToolServices
    events: EventLog
    publisher: Publisher
    clock: Clock
    settings: EngineSettings = field(default_factory=EngineSettings)
    state_store: StateStore | None = None


@dataclass
class NodeOutput:
    # Ops written under the node's own name (checked against its write ACL).
    ops: list[PatchOp] = field(default_factory=list)
    # Ops written by the runtime on the node's behalf (guard flags, termination).
    system_ops: list[PatchOp] = field(default_factory=list)
    # For nodes with dynamic routes: the chosen target. None for nodes with a static edge.
    goto: str | None = None
    # Extra fields for the node_completed event (e.g. the routing reason).
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class NodeContext:
    node: str
    run: RunState
    graph: GraphSpec
    deps: EngineDeps
    emit: NodeEmitter
    ledger: BudgetLedger
    _id_counter: int = 0

    def new_id(self, prefix: str) -> str:
        # Step numbers are unique per run and survive re-execution, so ids stay stable.
        self._id_counter += 1
        return f"{prefix}_{self.run.step + 1}_{self._id_counter}"

    async def llm(self, request: LLMRequest) -> LLMResponse:
        return await self.deps.gateway.complete(request, ledger=self.ledger, emit=self.emit)

    def tool_context(self, working_state: RunState) -> ToolContext:
        async def llm(messages: tuple[Message, ...]) -> LLMResponse:
            return await self.llm(LLMRequest(node=self.node, messages=messages))

        return ToolContext(
            run_id=self.run.run_id,
            tenant_id=self.run.tenant_id,
            node=self.node,
            state=working_state,
            services=self.deps.services,
            emit=self.emit,
            ledger=self.ledger,
            new_id=self.new_id,
            llm=llm,
        )


NodeFn = Callable[[RunState, NodeContext], Awaitable[NodeOutput]]
