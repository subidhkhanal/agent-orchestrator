"""The authoritative shared state of a graph run.

Every model here is frozen. Nodes never mutate a RunState; they return a StatePatch
(see patch.py) and the reducer builds the next RunState from it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Source(_Frozen):
    id: str = Field(min_length=1)
    title: str
    url_or_doc_ref: str
    snippet: str
    retrieved_by: str


ArtifactType = Literal["memo", "code", "answer"]


class ArtifactRef(_Frozen):
    """Pointer to artifact content. The content itself lives in the artifacts table."""

    id: str = Field(min_length=1)
    type: ArtifactType
    content_ref: str
    producer_agent: str
    version: int = Field(ge=1)


ReviewKind = Literal[
    "unsupported_claim",
    "policy",
    "redline",
    "verdict",
    "final_summary",
    "human_rejection",
    "general",
]


class ReviewNote(_Frozen):
    id: str = Field(min_length=1)
    author: str
    kind: ReviewKind
    text: str
    artifact_id: str | None = None
    artifact_version: int | None = None
    # Only set on kind == "verdict".
    verdict: Literal["pass", "changes_requested"] | None = None

    @model_validator(mode="after")
    def _verdict_only_on_verdict_notes(self) -> ReviewNote:
        if (self.kind == "verdict") != (self.verdict is not None):
            raise ValueError("verdict must be set exactly when kind == 'verdict'")
        return self


class Budget(_Frozen):
    token_limit: int = Field(gt=0)
    usd_limit: float = Field(gt=0)
    tokens_remaining: int = Field(ge=0)
    usd_remaining: float = Field(ge=0)
    deadline_at: datetime | None = None

    @property
    def token_fraction(self) -> float:
        return self.tokens_remaining / self.token_limit

    @property
    def usd_fraction(self) -> float:
        return self.usd_remaining / self.usd_limit

    @property
    def is_empty(self) -> bool:
        return self.tokens_remaining <= 0 or self.usd_remaining <= 0


class HitlState(_Frozen):
    pending: bool = False
    gate_id: str | None = None
    artifact_id: str | None = None
    artifact_version: int | None = None
    decision: Literal["approved", "rejected"] | None = None
    comment: str | None = None


class Termination(_Frozen):
    budget_exhausted: bool = False
    final_summary_done: bool = False
    reason: (
        Literal[
            "published",
            "supervisor_finished",
            "budget_exhausted",
            "budget_zero",
            "deadline_exceeded",
            "max_steps",
        ]
        | None
    ) = None


class Publication(_Frozen):
    effect_key: str
    sink_ref: str
    artifact_id: str
    artifact_version: int


class RunState(_Frozen):
    # Identity. Set once at run creation; no patch may change these.
    run_id: str
    tenant_id: str
    graph_id: str
    graph_version: int
    task: str
    task_kind: Literal["research", "code_only"] = "research"
    max_steps: int = Field(default=30, gt=0)

    # Working state, changed only through patches.
    constraints: tuple[str, ...] = ()
    sources: tuple[Source, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    review_notes: tuple[ReviewNote, ...] = ()
    open_questions: tuple[str, ...] = ()
    research_summary: str | None = None
    budget: Budget
    hitl: HitlState = HitlState()
    termination: Termination = Termination()
    publication: Publication | None = None

    # Bookkeeping, changed only by the reducer / runtime.
    state_version: int = Field(default=0, ge=0)
    step: int = Field(default=0, ge=0)

    def current_artifact(self, type_: ArtifactType = "memo") -> ArtifactRef | None:
        matches = [a for a in self.artifacts if a.type == type_]
        return matches[-1] if matches else None

    def source_ids(self) -> set[str]:
        return {s.id for s in self.sources}

    def latest_verdict(self, artifact: ArtifactRef) -> ReviewNote | None:
        verdicts = [
            n
            for n in self.review_notes
            if n.kind == "verdict"
            and n.artifact_id == artifact.id
            and n.artifact_version == artifact.version
        ]
        return verdicts[-1] if verdicts else None
