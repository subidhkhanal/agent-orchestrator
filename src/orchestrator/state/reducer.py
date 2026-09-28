"""The reducer: the single function that turns (state, patch) into the next state.

It enforces four things, in this order:
1. Optimistic concurrency: the patch must be based on the current state_version.
2. Write permissions: each author may only touch the fields its role owns.
3. Artifact versions only move forward by exactly one.
4. The result validates against the RunState schema.

It is a pure function. Persisting the result and logging the event happen in the node runner.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from orchestrator.state.models import ArtifactRef, RunState
from orchestrator.state.patch import AppendOp, SetOp, StatePatch, UpsertOp

SYSTEM_AUTHOR = "system"

# Which fields each author may write. "system" is the runtime itself (budget accounting,
# step counter, guard flags); LLM-driven nodes never write under that name.
WRITE_ACL: dict[str, frozenset[str]] = {
    "supervisor": frozenset({"open_questions", "constraints"}),
    "researcher": frozenset({"sources", "open_questions", "research_summary"}),
    "coder": frozenset({"artifacts", "open_questions"}),
    "reviewer": frozenset({"review_notes", "open_questions"}),
    "human_gate": frozenset({"hitl"}),
    "await_approval": frozenset({"hitl", "review_notes"}),
    "publish": frozenset({"publication"}),
    "single_agent": frozenset(
        {"sources", "artifacts", "review_notes", "open_questions", "research_summary"}
    ),
    SYSTEM_AUTHOR: frozenset({"budget", "termination", "step", "hitl"}),
}


class PatchRejected(Exception):
    """Base class for every reason a patch can be refused."""


class StalePatchError(PatchRejected):
    def __init__(self, base_version: int, current_version: int) -> None:
        super().__init__(
            f"patch based on state_version {base_version}, current is {current_version}"
        )
        self.base_version = base_version
        self.current_version = current_version


class PatchPermissionError(PatchRejected):
    pass


class PatchValidationError(PatchRejected):
    pass


def apply_patch(state: RunState, patch: StatePatch) -> RunState:
    if patch.base_version != state.state_version:
        raise StalePatchError(patch.base_version, state.state_version)

    allowed = WRITE_ACL.get(patch.author)
    if allowed is None:
        raise PatchPermissionError(f"unknown patch author {patch.author!r}")
    for op in patch.ops:
        if op.path not in allowed:
            raise PatchPermissionError(f"{patch.author!r} may not write {op.path!r}")

    data: dict[str, Any] = state.model_dump()
    for op in patch.ops:
        if isinstance(op, SetOp):
            data[op.path] = op.value
        elif isinstance(op, AppendOp):
            data[op.path] = [*data[op.path], op.value]
        elif isinstance(op, UpsertOp):
            data[op.path] = _upsert(op.path, data[op.path], op.value)

    data["state_version"] = state.state_version + 1
    try:
        return RunState.model_validate(data)
    except ValidationError as exc:
        raise PatchValidationError(str(exc)) from exc


def _upsert(path: str, items: list[Any], value: Any) -> list[Any]:
    item_id = value.get("id") if isinstance(value, dict) else getattr(value, "id", None)
    if not item_id:
        raise PatchValidationError(f"upsert into {path!r} needs an 'id'")

    existing = next((i for i, it in enumerate(items) if _id_of(it) == item_id), None)
    if path == "artifacts":
        new = ArtifactRef.model_validate(value)
        expected = 1 if existing is None else _version_of(items[existing]) + 1
        if new.version != expected:
            raise PatchValidationError(
                f"artifact {item_id!r} version must be {expected}, got {new.version}"
            )
    if existing is None:
        return [*items, value]
    # Replace in place, but move it to the end so "latest" ordering stays meaningful.
    return [*items[:existing], *items[existing + 1 :], value]


def _id_of(item: Any) -> Any:
    return item.get("id") if isinstance(item, dict) else getattr(item, "id", None)


def _version_of(item: Any) -> int:
    return int(item["version"] if isinstance(item, dict) else item.version)
