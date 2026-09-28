from __future__ import annotations

from typing import Any

from orchestrator.state.models import Budget, RunState


def make_budget(tokens: int = 100_000, usd: float = 1.0, **kw: Any) -> Budget:
    return Budget(
        token_limit=tokens, usd_limit=usd, tokens_remaining=tokens, usd_remaining=usd, **kw
    )


def make_state(**overrides: Any) -> RunState:
    data: dict[str, Any] = {
        "run_id": "run-1",
        "tenant_id": "tenant-a",
        "graph_id": "research-memo",
        "graph_version": 1,
        "task": "Research grid storage and draft a memo",
        "budget": make_budget(),
    }
    data.update(overrides)
    return RunState.model_validate(data)
