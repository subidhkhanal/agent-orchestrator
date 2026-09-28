"""Request and response bodies for the public API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunInput(_Body):
    task: str = Field(min_length=3, max_length=4000)
    task_kind: Literal["research", "code_only"] = "research"


class BudgetIn(_Body):
    max_tokens: int = Field(default=60_000, gt=1000, le=2_000_000)
    max_usd: float = Field(default=0.05, gt=0, le=100)
    deadline_s: int | None = Field(default=600, gt=0, le=86_400)


class CheckpointPolicy(_Body):
    # How often a `checkpoint` marker event is written to the audit log (the LangGraph
    # checkpointer itself saves after every step regardless; see ADR 0003).
    event_every_n: int = Field(default=5, ge=0, le=100)


class RunOptions(_Body):
    max_steps: int = Field(default=30, ge=3, le=100)


class CreateRunRequest(_Body):
    graph_id: str
    graph_version: int | None = None
    input: RunInput
    budget: BudgetIn = Field(default_factory=BudgetIn)
    checkpoint_policy: CheckpointPolicy = Field(default_factory=CheckpointPolicy)
    options: RunOptions = Field(default_factory=RunOptions)


class CreateRunResponse(BaseModel):
    run_id: str
    status: str
    graph_id: str
    graph_version: int
    stream_url: str
    created: bool


class ApproveRequest(_Body):
    comment: str | None = Field(default=None, max_length=4000)
    reviewer: str | None = Field(default=None, max_length=200)


class RejectRequest(_Body):
    reason: str = Field(min_length=1, max_length=4000)
    reviewer: str | None = Field(default=None, max_length=200)


class DecisionResponse(BaseModel):
    outcome: Literal["applied", "duplicate"]
    gate_id: str
    decision: str
    run_status: str


class PublishSinkRequest(_Body):
    tenant_id: str
    run_id: str
    artifact_id: str
    artifact_version: int
    content: str


class ErrorBody(BaseModel):
    error: dict[str, Any]
