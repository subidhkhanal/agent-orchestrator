from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.state import (
    AppendOp,
    PatchPermissionError,
    PatchValidationError,
    SetOp,
    StalePatchError,
    StatePatch,
    UpsertOp,
    apply_patch,
)
from tests.factories import make_state

SOURCE = {
    "id": "src_00000001",
    "title": "A",
    "url_or_doc_ref": "https://example.org/a",
    "snippet": "s",
    "retrieved_by": "researcher",
}


def artifact(version: int) -> dict[str, object]:
    return {
        "id": "memo",
        "type": "memo",
        "content_ref": f"artifact://run-1/memo/v{version}",
        "producer_agent": "coder",
        "version": version,
    }


def patch(author: str, *ops: object, base: int = 0) -> StatePatch:
    return StatePatch(base_version=base, author=author, ops=ops)  # type: ignore[arg-type]


def test_applied_patch_increments_state_version_and_leaves_input_untouched() -> None:
    state = make_state()
    new = apply_patch(state, patch("researcher", UpsertOp(path="sources", value=SOURCE)))
    assert new.state_version == 1
    assert [s.id for s in new.sources] == ["src_00000001"]
    assert state.sources == () and state.state_version == 0


def test_stale_patch_is_rejected() -> None:
    state = make_state()
    v1 = apply_patch(state, patch("researcher", AppendOp(path="open_questions", value="q1")))
    # A second writer computed its patch against version 0, but the state moved to 1.
    stale = patch("coder", AppendOp(path="open_questions", value="q2"), base=0)
    with pytest.raises(StalePatchError) as exc:
        apply_patch(v1, stale)
    assert (exc.value.base_version, exc.value.current_version) == (0, 1)


def test_patch_from_the_future_is_rejected() -> None:
    with pytest.raises(StalePatchError):
        apply_patch(
            make_state(), patch("researcher", AppendOp(path="open_questions", value="q"), base=5)
        )


@pytest.mark.parametrize(
    ("author", "op"),
    [
        ("researcher", UpsertOp(path="artifacts", value=artifact(1))),
        ("coder", UpsertOp(path="sources", value=SOURCE)),
        ("reviewer", SetOp(path="budget", value={})),
        ("supervisor", SetOp(path="hitl", value={"decision": "approved"})),
        ("coder", SetOp(path="termination", value={"reason": "published"})),
        ("nobody", AppendOp(path="open_questions", value="q")),
    ],
)
def test_authors_can_only_write_their_own_fields(author: str, op: object) -> None:
    with pytest.raises(PatchPermissionError):
        apply_patch(make_state(), patch(author, op))


def test_identity_and_bookkeeping_fields_are_not_patchable() -> None:
    for path in ("run_id", "tenant_id", "graph_version", "task", "state_version"):
        with pytest.raises(ValidationError):
            SetOp(path=path, value="x")  # type: ignore[arg-type]


def test_patch_result_is_validated_against_the_schema() -> None:
    bad_source = {**SOURCE, "id": ""}
    with pytest.raises(PatchValidationError):
        apply_patch(make_state(), patch("researcher", UpsertOp(path="sources", value=bad_source)))
    with pytest.raises(PatchValidationError):
        apply_patch(make_state(), patch("system", SetOp(path="step", value=-1)))


def test_artifact_versions_must_advance_by_exactly_one() -> None:
    state = apply_patch(make_state(), patch("coder", UpsertOp(path="artifacts", value=artifact(1))))
    with pytest.raises(PatchValidationError):
        apply_patch(state, patch("coder", UpsertOp(path="artifacts", value=artifact(3)), base=1))
    with pytest.raises(PatchValidationError):
        apply_patch(state, patch("coder", UpsertOp(path="artifacts", value=artifact(1)), base=1))
    v2 = apply_patch(state, patch("coder", UpsertOp(path="artifacts", value=artifact(2)), base=1))
    assert [a.version for a in v2.artifacts] == [2]
    assert v2.current_artifact().version == 2  # type: ignore[union-attr]


def test_first_artifact_version_must_be_one() -> None:
    with pytest.raises(PatchValidationError):
        apply_patch(make_state(), patch("coder", UpsertOp(path="artifacts", value=artifact(2))))


def test_upsert_replaces_by_id_and_append_keeps_order() -> None:
    state = apply_patch(make_state(), patch("researcher", UpsertOp(path="sources", value=SOURCE)))
    updated = {**SOURCE, "snippet": "newer"}
    state = apply_patch(
        state,
        patch(
            "researcher",
            UpsertOp(path="sources", value=updated),
            AppendOp(path="open_questions", value="a"),
            AppendOp(path="open_questions", value="b"),
            base=1,
        ),
    )
    assert [s.snippet for s in state.sources] == ["newer"]
    assert state.open_questions == ("a", "b")


def test_multi_op_patch_is_all_or_nothing() -> None:
    state = make_state()
    with pytest.raises(PatchValidationError):
        apply_patch(
            state,
            patch(
                "researcher",
                AppendOp(path="open_questions", value="fine"),
                UpsertOp(path="sources", value={**SOURCE, "title": None}),
            ),
        )
    assert state.open_questions == ()


def test_verdict_notes_require_a_verdict() -> None:
    note = {"id": "n1", "author": "reviewer", "kind": "verdict", "text": "ok"}
    with pytest.raises(PatchValidationError):
        apply_patch(make_state(), patch("reviewer", AppendOp(path="review_notes", value=note)))
