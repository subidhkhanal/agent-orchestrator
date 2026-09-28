"""In-process runner with an in-memory checkpointer.

Used by unit/integration tests and the offline demo. It drives the same compiled graphs as the
durable worker (milestone 2), which adds Postgres leases, the Postgres checkpointer and run
status tracking on top of the same start/resume flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Checkpointer, Command

from orchestrator.agents.hitl import announce_gate
from orchestrator.engine.context import EngineDeps, NodeFn
from orchestrator.engine.runner import build_graph
from orchestrator.events.log import EventType
from orchestrator.graphs.spec import GraphRegistry, GraphSpec
from orchestrator.state.models import Budget, RunState


@dataclass
class RunOutcome:
    status: Literal["completed", "waiting_hitl", "failed"]
    state: RunState
    interrupt: dict[str, Any] | None = None
    error: str | None = None


class LocalRunner:
    def __init__(
        self,
        graphs: GraphRegistry,
        deps: EngineDeps,
        library: dict[str, NodeFn],
        checkpointer: Checkpointer = None,
    ) -> None:
        self._graphs = graphs
        self._deps = deps
        self._library = library
        self._checkpointer = checkpointer or InMemorySaver()
        self._compiled: dict[tuple[str, int], Any] = {}
        self._pinned: dict[str, GraphSpec] = {}

    def _compiled_for(self, spec: GraphSpec) -> Any:
        key = (spec.graph_id, spec.version)
        if key not in self._compiled:
            self._compiled[key] = build_graph(spec, self._library, self._deps, self._checkpointer)
        return self._compiled[key]

    def _config(self, run_id: str, max_steps: int) -> RunnableConfig:
        return {
            "configurable": {"thread_id": run_id, "attempt": 1},
            "recursion_limit": max_steps + 10,
        }

    async def start(
        self,
        *,
        run_id: str,
        tenant_id: str,
        graph_id: str,
        task: str,
        budget: Budget,
        graph_version: int | None = None,
        max_steps: int = 30,
        task_kind: Literal["research", "code_only"] = "research",
    ) -> RunOutcome:
        spec = self._graphs.get(graph_id, graph_version)
        self._pinned[run_id] = spec
        state = RunState(
            run_id=run_id,
            tenant_id=tenant_id,
            graph_id=spec.graph_id,
            graph_version=spec.version,
            task=task,
            task_kind=task_kind,
            max_steps=max_steps,
            budget=budget,
        )
        await self._deps.events.append(
            run_id,
            EventType.RUN_CREATED,
            {
                "graph_id": spec.graph_id,
                "graph_version": spec.version,
                "topology_hash": spec.topology_hash(),
            },
            "run_created",
        )
        return await self._drive(
            spec, state.max_steps, run_id, {"run": state.model_dump(mode="json")}
        )

    async def resume(self, run_id: str, decision: dict[str, Any]) -> RunOutcome:
        spec = self._pinned[run_id]
        state = await self.state(run_id)
        return await self._drive(spec, state.max_steps, run_id, Command(resume=decision))

    async def state(self, run_id: str) -> RunState:
        spec = self._pinned[run_id]
        snapshot = await self._compiled_for(spec).aget_state(self._config(run_id, 0))
        return RunState.model_validate(snapshot.values["run"])

    async def _drive(
        self, spec: GraphSpec, max_steps: int, run_id: str, graph_input: Any
    ) -> RunOutcome:
        graph = self._compiled_for(spec)
        config = self._config(run_id, max_steps)
        try:
            await graph.ainvoke(graph_input, config)
        except Exception as exc:
            await self._deps.events.append(
                run_id, EventType.FAILED, {"error": f"{type(exc).__name__}: {exc}"}, "failed"
            )
            return RunOutcome(
                "failed", await self.state(run_id), error=f"{type(exc).__name__}: {exc}"
            )

        snapshot = await graph.aget_state(config)
        state = RunState.model_validate(snapshot.values["run"])
        if snapshot.interrupts:
            gate = snapshot.interrupts[0].value
            await announce_gate(self._deps.events, self._deps.services.artifacts, state, gate)
            return RunOutcome("waiting_hitl", state, interrupt=gate)
        await self._deps.events.append(
            run_id,
            EventType.COMPLETED,
            {
                "reason": state.termination.reason,
                "state_version": state.state_version,
                "usd_spent": state.budget.usd_limit - state.budget.usd_remaining,
                "tokens_spent": state.budget.token_limit - state.budget.tokens_remaining,
            },
            "completed",
        )
        return RunOutcome("completed", state)
