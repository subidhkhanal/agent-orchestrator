"""State patches: the only way a node can change RunState.

A patch is a list of typed operations plus the state_version it was computed against.
Three operations are enough for this state shape:

- set:    replace a whole field (budget, hitl, termination, ...)
- append: add an item to a list field (review_notes, open_questions, ...)
- upsert: insert or replace an item in an id-keyed list field (sources, artifacts)
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SetPath = Literal["research_summary", "budget", "hitl", "termination", "publication", "step"]
ListPath = Literal["constraints", "sources", "artifacts", "review_notes", "open_questions"]
KeyedListPath = Literal["sources", "artifacts"]


class _Op(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SetOp(_Op):
    op: Literal["set"] = "set"
    path: SetPath
    value: Any


class AppendOp(_Op):
    op: Literal["append"] = "append"
    path: ListPath
    value: Any


class UpsertOp(_Op):
    op: Literal["upsert"] = "upsert"
    path: KeyedListPath
    value: Any


PatchOp = Annotated[SetOp | AppendOp | UpsertOp, Field(discriminator="op")]


class StatePatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_version: int = Field(ge=0)
    author: str
    ops: tuple[PatchOp, ...]
