"""Turns node functions into LangGraph nodes.

The wrapper is where the state rules from the plan are enforced for every node:
- the node gets a read-only RunState and returns a NodeOutput (a patch, not a new state),
- the patch goes through the reducer (version check, write ACL, schema validation),
- the runtime adds its own patch: step counter and the budget after this node's spend,
- every applied patch, plus node start/finish with tokens, cost and latency, goes to the log.

LangGraph stores the resulting RunState as a plain dict in a single `run` channel, so
checkpoints are plain JSON and never depend on pickling our classes.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END as LG_END
from langgraph.graph import START, StateGraph
from langgraph.types import Checkpointer, Command

from orchestrator.engine.context import EngineDeps, NodeContext, NodeFn, NodeOutput
from orchestrator.events.emitter import NodeEmitter
from orchestrator.events.log import EventType
from orchestrator.gateway.budget import BudgetLedger
from orchestrator.graphs.spec import END, GraphSpec
from orchestrator.state.models import RunState
from orchestrator.state.patch import PatchOp, SetOp, StatePatch
from orchestrator.state.reducer import SYSTEM_AUTHOR, apply_patch


class GraphState(TypedDict):
    run: dict[str, Any]


def _lg(node: str) -> str:
    return LG_END if node == END else node


def _attempt(config: RunnableConfig) -> int:
    return int((config.get("configurable") or {}).get("attempt", 1))


async def _apply_and_log(
    state: RunState, author: str, ops: list[PatchOp], emit: NodeEmitter, deps: EngineDeps
) -> RunState:
    if not ops:
        return state
    patch = StatePatch(base_version=state.state_version, author=author, ops=tuple(ops))
    new_state = apply_patch(state, patch)
    if deps.state_store is not None:
        # Durable optimistic-concurrency check, fenced by the worker's lease generation.
        await deps.state_store.commit(new_state.run_id, emit.attempt, patch.base_version, new_state)
    await emit(
        EventType.STATE_PATCH,
        {
            "author": author,
            "base_version": patch.base_version,
            "state_version": new_state.state_version,
            "ops": [op.model_dump(mode="json") for op in ops],
        },
    )
    return new_state


def wrap_node(name: str, fn: NodeFn, spec: GraphSpec, deps: EngineDeps) -> Any:
    node_config = deps.gateway.config.nodes.get(name)

    async def node(state: GraphState, config: RunnableConfig) -> Command[str] | GraphState:
        run = RunState.model_validate(state["run"])
        step = run.step + 1
        emit = NodeEmitter(deps.events, run.run_id, _attempt(config), step, name)
        ledger = BudgetLedger.for_node(run.budget, node_config)
        started = deps.clock.now()
        await emit(EventType.NODE_STARTED, {"state_version": run.state_version})

        ctx = NodeContext(node=name, run=run, graph=spec, deps=deps, emit=emit, ledger=ledger)
        output: NodeOutput = await fn(run, ctx)

        new = await _apply_and_log(run, name, output.ops, emit, deps)
        runtime_ops: list[PatchOp] = [
            SetOp(path="step", value=step),
            SetOp(path="budget", value=ledger.remaining_after(new.budget).model_dump()),
            *output.system_ops,
        ]
        new = await _apply_and_log(new, SYSTEM_AUTHOR, runtime_ops, emit, deps)

        goto = output.goto
        if goto is not None and goto not in spec.targets(name):
            raise RuntimeError(f"node {name!r} tried to route to {goto!r}")
        if goto is None and spec.targets(name):
            raise RuntimeError(f"node {name!r} must choose a route")

        await emit(
            EventType.NODE_COMPLETED,
            {
                "state_version": new.state_version,
                "next": goto or spec.static_next(name),
                "tokens": ledger.tokens_used,
                "usd": ledger.usd_used,
                "latency_ms": int((deps.clock.now() - started).total_seconds() * 1000),
                **output.summary,
            },
        )
        every = (config.get("configurable") or {}).get("checkpoint_event_every_n")
        if every is None:
            every = deps.settings.checkpoint_event_every_n
        if every > 0 and step % every == 0:
            # The LangGraph checkpointer saves after every super-step regardless; this event
            # is a coarser marker in the audit log (see ADR 0003).
            await emit(EventType.CHECKPOINT, {"state_version": new.state_version})

        update: GraphState = {"run": new.model_dump(mode="json")}
        if goto is not None:
            return Command(goto=_lg(goto), update=update)
        return update

    node.__name__ = f"node_{name}"
    return node


def build_graph(
    spec: GraphSpec,
    library: dict[str, NodeFn],
    deps: EngineDeps,
    checkpointer: Checkpointer = None,
) -> Any:
    missing = set(spec.nodes) - set(library)
    if missing:
        raise ValueError(f"no implementation for nodes {sorted(missing)}")
    graph = StateGraph(GraphState)
    for name in spec.nodes:
        targets = tuple(_lg(t) for t in spec.targets(name))
        graph.add_node(
            name, wrap_node(name, library[name], spec, deps), destinations=targets or None
        )
    graph.add_edge(START, spec.entry)
    for src, dst in spec.edges:
        graph.add_edge(src, _lg(dst))
    return graph.compile(checkpointer=checkpointer)
