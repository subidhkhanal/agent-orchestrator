"""Run research-memo end to end offline (fake LLM, canned sources) and print the timeline.

python scripts/demo_offline.py [--reject-first]
"""

from __future__ import annotations

import argparse
import asyncio

from orchestrator.events import EventType
from orchestrator.gateway.providers.demo_policy import ResearchMemoPolicy
from orchestrator.gateway.providers.fake import FakeLLM
from orchestrator.offline import build_offline
from orchestrator.state.models import Budget

TASK = "Research grid battery storage trends and draft a briefing memo with cited sources"


async def main(reject_first: bool) -> None:
    runner, deps = build_offline(llm=FakeLLM(policy=ResearchMemoPolicy(flawed_first_draft=True)))
    budget = Budget(
        token_limit=100_000, usd_limit=0.50, tokens_remaining=100_000, usd_remaining=0.50
    )
    outcome = await runner.start(
        run_id="demo", tenant_id="demo", graph_id="research-memo", task=TASK, budget=budget
    )
    rejected = False
    while outcome.status == "waiting_hitl":
        assert outcome.interrupt is not None
        gate = outcome.interrupt["gate_id"]
        if reject_first and not rejected:
            rejected = True
            print(f"\n>>> human rejects {gate}\n")
            decision = {
                "gate_id": gate,
                "decision": "rejected",
                "reviewer": "demo",
                "comment": "Say what changed since the last draft.",
            }
        else:
            print(f"\n>>> human approves {gate}\n")
            decision = {"gate_id": gate, "decision": "approved", "reviewer": "demo"}
        outcome = await runner.resume("demo", decision)

    for e in deps.events.all("demo"):  # type: ignore[attr-defined]
        p = e.payload
        if e.event_type == EventType.NODE_COMPLETED:
            print(
                f"{e.event_id:>4}  {p['node']:<15} -> {p['next'] or '':<15} "
                f"tokens={p['tokens']:<5} usd={p['usd']:.5f}  {p.get('reason', '')}"
            )
        elif e.event_type in (
            EventType.GUARD_OVERRIDE,
            EventType.TOOL_DENIED,
            EventType.HITL_REQUIRED,
            EventType.COMPLETED,
            EventType.FAILED,
        ):
            detail = {k: v for k, v in p.items() if k not in ("preview", "node", "step")}
            print(f"{e.event_id:>4}  [{e.event_type.value}] {detail}")

    state = outcome.state
    memo = state.current_artifact()
    print(
        f"\nstatus={outcome.status} reason={state.termination.reason} "
        f"state_version={state.state_version} steps={state.step}"
    )
    print(
        f"spent: {budget.token_limit - state.budget.tokens_remaining} tokens, "
        f"${budget.usd_limit - state.budget.usd_remaining:.4f}"
    )
    if memo:
        text = await deps.services.artifacts.get(memo.content_ref, tenant_id="demo")
        print(f"\n--- memo v{memo.version} ---\n{text}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reject-first", action="store_true")
    asyncio.run(main(parser.parse_args().reject_first))
