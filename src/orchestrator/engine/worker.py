"""Durable background worker.

Loop: claim a run (Postgres lease) -> run or resume its graph from the latest LangGraph
checkpoint (thread_id = run_id) -> park it for approval, complete it, or fail it.

What happens on a crash: the dead worker stops heartbeating, its lease expires, and another
worker claims the run with attempt+1. That worker loads the latest checkpoint and continues.
The node that was executing when the first worker died **runs again from the start**
(checkpoints are taken between nodes, never inside one). Every side effect inside a node must
therefore be idempotent: events are keyed, artifacts are content-addressed, and publishing
goes through the effect ledger with a deterministic key.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from datetime import timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Checkpointer, Command

from orchestrator.agents.hitl import gate_announcement
from orchestrator.db.runs import ClaimedRun, RunStatus, RunStore
from orchestrator.engine.context import EngineDeps, NodeFn
from orchestrator.engine.runner import build_graph
from orchestrator.events.log import EventType, LeaseLost
from orchestrator.graphs.spec import GraphRegistry, GraphSpec
from orchestrator.state.models import RunState

log = logging.getLogger("orchestrator.worker")


class _Stop(Exception):
    """Raised into the run task when the heartbeat decides to stop it."""


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker:
    def __init__(
        self,
        *,
        deps: EngineDeps,
        runs: RunStore,
        graphs: GraphRegistry,
        library: dict[str, NodeFn],
        checkpointer: Checkpointer,
        worker_id: str | None = None,
        lease_s: float = 30.0,
        heartbeat_s: float = 5.0,
        poll_s: float = 1.0,
        hitl_timeout: timedelta = timedelta(hours=24),
        wake: asyncio.Event | None = None,
        sweep_s: float = 10.0,
    ) -> None:
        # `wake` lets an in-process API signal new work immediately, so the database can be
        # polled rarely (and a scale-to-zero database can actually go idle).
        self.wake = wake
        self.sweep_s = sweep_s
        self.deps = deps
        self.runs = runs
        self.graphs = graphs
        self.library = library
        self.checkpointer = checkpointer
        self.worker_id = worker_id or default_worker_id()
        self.lease_s = lease_s
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.hitl_timeout = hitl_timeout
        self._compiled: dict[tuple[str, int], Any] = {}

    # --- main loop ---------------------------------------------------------------------------

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("worker %s started", self.worker_id)
        last_sweep = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            if loop.time() - last_sweep > self.sweep_s:
                await self.sweep_expired_approvals()
                last_sweep = loop.time()
            if not await self.run_once():
                await self._idle(stop)

    async def _idle(self, stop: asyncio.Event) -> None:
        waiters = [asyncio.ensure_future(stop.wait())]
        if self.wake is not None:
            waiters.append(asyncio.ensure_future(self.wake.wait()))
        try:
            await asyncio.wait(waiters, timeout=self.poll_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            if self.wake is not None:
                self.wake.clear()

    async def run_once(self) -> bool:
        """Claim and process at most one run. Returns False if there was nothing to do."""
        claimed = await self.runs.claim(self.worker_id, self.lease_s)
        if claimed is None:
            return False
        await self.process(claimed)
        return True

    async def sweep_expired_approvals(self) -> None:
        for run_id in await self.runs.expire_hitl():
            await self.deps.events.append(
                run_id, EventType.FAILED, {"error": "hitl_timeout"}, "failed"
            )

    # --- one run ---------------------------------------------------------------------------

    def _graph(self, spec: GraphSpec) -> Any:
        key = (spec.graph_id, spec.version)
        if key not in self._compiled:
            self._compiled[key] = build_graph(spec, self.library, self.deps, self.checkpointer)
        return self._compiled[key]

    async def process(self, claimed: ClaimedRun) -> None:
        run_task = asyncio.create_task(self._execute(claimed))
        stop_reason: list[str] = []
        hb_task = asyncio.create_task(self._heartbeat(claimed, run_task, stop_reason))
        try:
            await run_task
        except asyncio.CancelledError:
            if not stop_reason:
                raise  # the worker itself is being shut down (or killed in a test)
            await self._stopped(claimed, stop_reason[0])
        except LeaseLost:
            log.warning(
                "run %s: lease lost (attempt %s); dropping it", claimed.run_id, claimed.attempt
            )
        except Exception as exc:
            await self._failed(claimed, exc)
        finally:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task

    async def _heartbeat(
        self, claimed: ClaimedRun, run_task: asyncio.Task[None], stop_reason: list[str]
    ) -> None:
        while not run_task.done():
            await asyncio.sleep(self.heartbeat_s)
            beat = await self.runs.heartbeat(claimed.run_id, claimed.attempt, self.lease_s)
            if not beat.alive:
                stop_reason.append("lease_lost")
            elif beat.cancel_requested:
                stop_reason.append("cancelled")
            if stop_reason:
                run_task.cancel()
                return

    async def _stopped(self, claimed: ClaimedRun, reason: str) -> None:
        if reason == "cancelled":
            await self.deps.events.append(
                claimed.run_id,
                EventType.CANCELLED,
                {"by": "user"},
                "cancelled",
                attempt=claimed.attempt,
            )
            await self.runs.finish(claimed.run_id, claimed.attempt, RunStatus.CANCELLED)
        else:
            log.warning("run %s: stopped, %s", claimed.run_id, reason)

    async def _failed(self, claimed: ClaimedRun, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("run %s failed", claimed.run_id)
        with contextlib.suppress(LeaseLost):
            await self.deps.events.append(
                claimed.run_id,
                EventType.FAILED,
                {"error": error[:2000]},
                "failed",
                attempt=claimed.attempt,
            )
            await self.runs.finish(claimed.run_id, claimed.attempt, RunStatus.FAILED, error[:2000])

    async def _execute(self, claimed: ClaimedRun) -> None:
        spec = self.graphs.get(claimed.graph_id, claimed.graph_version)
        graph = self._graph(spec)
        max_steps = int(claimed.state.get("max_steps", 30))
        config: RunnableConfig = {
            "configurable": {
                "thread_id": claimed.run_id,
                "attempt": claimed.attempt,
                "checkpoint_event_every_n": claimed.options.get("checkpoint_event_every_n"),
            },
            "recursion_limit": max_steps + 10,
        }

        snapshot = await graph.aget_state(config)
        graph_input: Any
        if not snapshot.values:
            mode = "start"
            graph_input = {"run": claimed.state}
        else:
            state = RunState.model_validate(snapshot.values["run"])
            await self.runs.rewind(claimed.run_id, claimed.attempt, state)
            if snapshot.interrupts:
                gate = snapshot.interrupts[0].value
                payload = claimed.resume_payload
                if payload is None or payload.get("gate_id") != gate["gate_id"]:
                    # Claimed while still waiting for a human (should not happen): park again.
                    await self._park(claimed, state, gate)
                    return
                mode = "resume_hitl"
                graph_input = Command(resume=payload)
            elif snapshot.next:
                mode = "recover"  # a previous worker died mid-run
                graph_input = None
            else:
                mode = "finalize"
                graph_input = None

        await self.deps.events.append(
            claimed.run_id,
            EventType.RUN_CLAIMED,
            {
                "worker_id": self.worker_id,
                "attempt": claimed.attempt,
                "mode": mode,
                "resume_from": list(snapshot.next) if snapshot.values else [],
            },
            f"claimed:a{claimed.attempt}",
            attempt=claimed.attempt,
        )
        if mode != "finalize":
            await graph.ainvoke(graph_input, config, durability="sync")

        snapshot = await graph.aget_state(config)
        state = RunState.model_validate(snapshot.values["run"])
        if snapshot.interrupts:
            await self._park(claimed, state, snapshot.interrupts[0].value)
            return
        await self.deps.events.append(
            claimed.run_id,
            EventType.COMPLETED,
            {
                "reason": state.termination.reason,
                "state_version": state.state_version,
                "usd_spent": state.budget.usd_limit - state.budget.usd_remaining,
                "tokens_spent": state.budget.token_limit - state.budget.tokens_remaining,
            },
            "completed",
            attempt=claimed.attempt,
        )
        await self.runs.finish(claimed.run_id, claimed.attempt, RunStatus.COMPLETED)

    async def _park(self, claimed: ClaimedRun, state: RunState, gate: dict[str, Any]) -> None:
        announcement = await gate_announcement(self.deps.services.artifacts, state, gate)
        await self.runs.park_for_hitl(
            claimed.run_id, claimed.attempt, gate, self.hitl_timeout, announcement
        )
