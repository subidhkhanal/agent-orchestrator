"""Code-enforced routing invariants.

These run after the supervisor's decision and hold no matter what the model outputs. The
supervisor prompt describes the same rules, but only to avoid wasted overrides; the rules are
enforced here, not in the prompt.

Two kinds of guards:
- forced guards look only at the state (budget, step cap) and override any proposal;
- route guards check one proposed target against its precondition and redirect it.
Route guards are applied until nothing changes, so publish -> human_gate -> coder ->
researcher can chain when several preconditions are missing at once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from orchestrator.graphs.spec import END, GraphSpec
from orchestrator.state.models import RunState
from orchestrator.state.patch import PatchOp, SetOp

LOW_BUDGET_FRACTION = 0.10


class InvariantViolation(Exception):
    """Raised when code reaches a state the guards should have made impossible."""


@dataclass(frozen=True)
class GuardOverride:
    guard: str
    proposed: str | None
    forced: str
    detail: str


@dataclass
class RouteResult:
    next_node: str
    overrides: list[GuardOverride] = field(default_factory=list)
    system_ops: list[PatchOp] = field(default_factory=list)


def _terminate(state: RunState, **changes: object) -> SetOp:
    return SetOp(
        path="termination",
        value=state.termination.model_copy(update=changes).model_dump(),
    )


# --- invariant predicates (also used by nodes as a second line of defense) -------------------


def publish_allowed(state: RunState) -> bool:
    """publish needs a human approval for exactly the current artifact version."""
    artifact = state.current_artifact()
    return (
        artifact is not None
        and state.hitl.decision == "approved"
        and not state.hitl.pending
        and state.hitl.artifact_id == artifact.id
        and state.hitl.artifact_version == artifact.version
    )


# --- forced guards ---------------------------------------------------------------------------


def guard_budget_zero(state: RunState, graph: GraphSpec) -> RouteResult | None:
    if not state.budget.is_empty:
        return None
    return RouteResult(
        END,
        [GuardOverride("budget_zero", None, END, "budget is exhausted; ending with partial work")],
        [_terminate(state, budget_exhausted=True, reason="budget_zero")],
    )


def budget_wind_down(state: RunState, graph: GraphSpec, detail: str) -> RouteResult:
    """Final reviewer summary if there is still a reviewer to route to, otherwise END."""
    flags = _terminate(state, budget_exhausted=True, reason="budget_exhausted")
    if not state.termination.final_summary_done and "reviewer" in graph.targets("supervisor"):
        return RouteResult(
            "reviewer",
            [GuardOverride("budget_low", None, "reviewer", detail + "; final reviewer summary")],
            [flags],
        )
    return RouteResult(
        END,
        [GuardOverride("budget_low", None, END, detail + "; final summary done, ending")],
        [flags],
    )


def guard_budget_low(state: RunState, graph: GraphSpec) -> RouteResult | None:
    b = state.budget
    if b.token_fraction >= LOW_BUDGET_FRACTION and b.usd_fraction >= LOW_BUDGET_FRACTION:
        return None
    detail = (
        f"budget below {LOW_BUDGET_FRACTION:.0%} "
        f"(tokens {b.token_fraction:.1%}, usd {b.usd_fraction:.1%})"
    )
    return budget_wind_down(state, graph, detail)


def guard_max_steps(state: RunState, graph: GraphSpec) -> RouteResult | None:
    # The supervisor itself is step `state.step + 1`; the node it routes to would be
    # `state.step + 2`. End now if that node would go past the cap.
    if state.step + 2 <= state.max_steps:
        return None
    return RouteResult(
        END,
        [GuardOverride("max_steps", None, END, f"reached max_steps={state.max_steps}")],
        [_terminate(state, reason="max_steps")],
    )


FORCED_GUARDS: tuple[Callable[[RunState, GraphSpec], RouteResult | None], ...] = (
    guard_budget_zero,
    guard_budget_low,
    guard_max_steps,
)


# --- route guards ----------------------------------------------------------------------------


def guard_publish_requires_approval(state: RunState, proposed: str) -> GuardOverride | None:
    if proposed != "publish" or publish_allowed(state):
        return None
    return GuardOverride(
        "publish_requires_approval",
        proposed,
        "human_gate",
        "publish needs a human approval of the current artifact version",
    )


def guard_human_gate_requires_artifact(state: RunState, proposed: str) -> GuardOverride | None:
    if proposed != "human_gate" or state.current_artifact() is not None:
        return None
    return GuardOverride(
        "human_gate_requires_artifact", proposed, "coder", "nothing to approve yet"
    )


def guard_coder_requires_sources(state: RunState, proposed: str) -> GuardOverride | None:
    if proposed != "coder" or state.task_kind == "code_only" or state.sources:
        return None
    return GuardOverride(
        "coder_requires_sources",
        proposed,
        "researcher",
        "the coder cannot draft before at least one source exists",
    )


def guard_review_before_rewrite(state: RunState, proposed: str) -> GuardOverride | None:
    """The current draft must be reviewed before the coder rewrites it.

    Without this, a supervisor that keeps answering "coder" rewrites the memo in a loop until
    the budget runs out (seen on a live run). Human feedback goes to the coder directly from
    the approval gate, not through this route, so it is unaffected.
    """
    artifact = state.current_artifact()
    if proposed != "coder" or artifact is None or state.latest_verdict(artifact) is not None:
        return None
    return GuardOverride(
        "review_before_rewrite",
        proposed,
        "reviewer",
        f"memo v{artifact.version} has not been reviewed yet",
    )


def guard_review_once_per_version(state: RunState, proposed: str) -> GuardOverride | None:
    """A version that already has a verdict is not reviewed again.

    Seen on a live run: the reviewer's notes said "pass" while its verdict on the same version
    stayed "changes_requested", and the supervisor sent that version back to the reviewer until
    the step cap. A second review of unchanged text cannot change the facts, so a reviewed
    version moves on: to the writer if changes were requested, otherwise towards approval.
    """
    artifact = state.current_artifact()
    if proposed != "reviewer" or artifact is None:
        return None
    verdict = state.latest_verdict(artifact)
    if verdict is None:
        return None
    gated = (
        state.hitl.artifact_id == artifact.id and state.hitl.artifact_version == artifact.version
    )
    if verdict.verdict == "pass" and not gated:
        forced = "human_gate"
    elif verdict.verdict == "pass" and publish_allowed(state):
        forced = "publish"
    else:
        forced = "coder"
    return GuardOverride(
        "review_once_per_version",
        proposed,
        forced,
        f"memo v{artifact.version} already has a '{verdict.verdict}' verdict",
    )


def guard_reviewer_requires_draft(state: RunState, proposed: str) -> GuardOverride | None:
    """There is nothing to review before the first draft (seen on a live run)."""
    if proposed != "reviewer" or state.current_artifact() is not None:
        return None
    return GuardOverride(
        "reviewer_requires_draft", proposed, "coder", "no draft exists yet to review"
    )


ROUTE_GUARDS: tuple[Callable[[RunState, str], GuardOverride | None], ...] = (
    guard_publish_requires_approval,
    guard_human_gate_requires_artifact,
    guard_reviewer_requires_draft,
    guard_review_once_per_version,
    guard_coder_requires_sources,
    guard_review_before_rewrite,
)


def forced_route(state: RunState, graph: GraphSpec) -> RouteResult | None:
    for guard in FORCED_GUARDS:
        result = guard(state, graph)
        if result is not None:
            return result
    return None


def enforce_invariants(state: RunState, proposed: str, graph: GraphSpec) -> RouteResult:
    forced = forced_route(state, graph)
    if forced is not None:
        forced.overrides = [
            GuardOverride(o.guard, proposed, o.forced, o.detail) for o in forced.overrides
        ]
        return forced

    current = proposed
    overrides: list[GuardOverride] = []
    for _ in range(len(ROUTE_GUARDS) + 1):
        changed = False
        for guard in ROUTE_GUARDS:
            override = guard(state, current)
            if override is not None:
                overrides.append(override)
                current = override.forced
                changed = True
        if not changed:
            break
    else:
        raise InvariantViolation(f"route guards did not settle for proposal {proposed!r}")

    system_ops: list[PatchOp] = []
    if current == END and state.termination.reason is None:
        system_ops.append(_terminate(state, reason="supervisor_finished"))
    if current not in graph.targets("supervisor"):
        raise InvariantViolation(f"guards produced {current!r}, not a route in this graph")
    return RouteResult(current, overrides, system_ops)


def termination_for(state: RunState, reason: str) -> SetOp:
    """Helper for nodes that must end the run themselves (e.g. out of budget mid-call)."""
    return _terminate(state, reason=reason, budget_exhausted=reason.startswith("budget"))


__all__ = [
    "LOW_BUDGET_FRACTION",
    "GuardOverride",
    "InvariantViolation",
    "RouteResult",
    "budget_wind_down",
    "enforce_invariants",
    "forced_route",
    "publish_allowed",
    "termination_for",
]
