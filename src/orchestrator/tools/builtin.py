"""The tool implementations for each worker role."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from orchestrator.citations import check_memo, normalize_citations
from orchestrator.gateway.types import Message
from orchestrator.state.models import ArtifactRef, ReviewNote, Source
from orchestrator.state.patch import AppendOp, UpsertOp
from orchestrator.tools.base import Tool, ToolContext, ToolError, ToolResult

MEMO_ID = "memo"
# Upper bound on results per search call, whatever the model asks for.
MAX_RESULTS = 5


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


# --- researcher ------------------------------------------------------------------------------


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    # Accept what models tend to ask for; the handler clamps to MAX_RESULTS.
    max_results: int = Field(default=4, ge=1, le=50)


async def _web_search(ctx: ToolContext, args: WebSearchArgs) -> ToolResult:
    hits = await ctx.services.search.search(args.query, min(args.max_results, MAX_RESULTS))
    sources = [
        Source(
            id=f"src_{_short_hash(h.url)}",
            title=h.title,
            url_or_doc_ref=h.url,
            snippet=h.snippet,
            retrieved_by=ctx.node,
        )
        for h in hits
    ]
    return ToolResult(
        output=[{"id": s.id, "title": s.title, "snippet": s.snippet[:300]} for s in sources],
        ops=[UpsertOp(path="sources", value=s.model_dump()) for s in sources],
    )


class RagQueryArgs(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=3, ge=1, le=50)


async def _rag_query(ctx: ToolContext, args: RagQueryArgs) -> ToolResult:
    hits = await ctx.services.rag.query(args.question, min(args.top_k, MAX_RESULTS))
    sources = [
        Source(
            id=f"doc_{_short_hash(h.doc_ref)}",
            title=h.title,
            url_or_doc_ref=h.doc_ref,
            snippet=h.snippet,
            retrieved_by=ctx.node,
        )
        for h in hits
    ]
    return ToolResult(
        output=[{"id": s.id, "title": s.title, "snippet": s.snippet[:300]} for s in sources],
        ops=[UpsertOp(path="sources", value=s.model_dump()) for s in sources],
    )


class SummarizeArgs(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    max_words: int = Field(default=150, ge=10, le=500)


async def _summarize(ctx: ToolContext, args: SummarizeArgs) -> ToolResult:
    response = await ctx.llm(
        (
            Message(
                role="system",
                content=f"Summarize the user's text in at most {args.max_words} words. "
                "Keep any [src_...] or [doc_...] citation markers attached to the facts "
                "they support.",
            ),
            Message(role="user", content=args.text),
        )
    )
    return ToolResult(output={"summary": response.content})


# --- coder -----------------------------------------------------------------------------------


class WriteArtifactArgs(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)


async def _store_new_memo_version(ctx: ToolContext, content: str) -> ToolResult:
    content = normalize_citations(content)
    current = ctx.state.current_artifact("memo")
    version = 1 if current is None else current.version + 1
    ref = await ctx.services.artifacts.put(
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        artifact_id=MEMO_ID,
        version=version,
        type_="memo",
        content=content,
    )
    artifact = ArtifactRef(
        id=MEMO_ID, type="memo", content_ref=ref, producer_agent=ctx.node, version=version
    )
    return ToolResult(
        output={"artifact_id": MEMO_ID, "version": version, "chars": len(content)},
        ops=[UpsertOp(path="artifacts", value=artifact.model_dump())],
    )


async def _write_artifact(ctx: ToolContext, args: WriteArtifactArgs) -> ToolResult:
    return await _store_new_memo_version(ctx, args.content)


class EditSectionArgs(BaseModel):
    heading: str = Field(min_length=1, max_length=200, description="Exact text of a ## heading")
    new_body: str = Field(min_length=1, max_length=20_000)


async def _edit_section(ctx: ToolContext, args: EditSectionArgs) -> ToolResult:
    current = ctx.state.current_artifact("memo")
    if current is None:
        raise ToolError("there is no memo yet; call write_artifact first")
    content = await ctx.services.artifacts.get(current.content_ref, tenant_id=ctx.tenant_id)
    pattern = re.compile(
        rf"(^##\s+{re.escape(args.heading.strip())}\s*\n)(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    if not pattern.search(content):
        raise ToolError(f"no section with heading {args.heading!r}")
    updated = pattern.sub(lambda m: m.group(1) + args.new_body.rstrip() + "\n\n", content, count=1)
    return await _store_new_memo_version(ctx, updated.rstrip() + "\n")


class SpawnSandboxArgs(BaseModel):
    language: Literal["python"] = "python"
    code: str = Field(min_length=1, max_length=20_000)


async def _spawn_sandbox(ctx: ToolContext, args: SpawnSandboxArgs) -> ToolResult:
    return ToolResult(output=await ctx.services.sandbox.run(args.language, args.code, 30.0))


# --- reviewer (no network access) ------------------------------------------------------------


class PolicyCheckArgs(BaseModel):
    pass


async def _policy_check(ctx: ToolContext, args: PolicyCheckArgs) -> ToolResult:
    current = ctx.state.current_artifact("memo")
    if current is None:
        raise ToolError("there is no memo to check")
    content = await ctx.services.artifacts.get(current.content_ref, tenant_id=ctx.tenant_id)
    report = check_memo(content, ctx.state.source_ids())
    return ToolResult(
        output={
            "artifact_version": current.version,
            "claims": report.claims,
            "supported_claims": report.supported_claims,
            "findings": [
                {"kind": f.kind, "line": f.line, "detail": f.detail} for f in report.findings
            ],
        }
    )


def _note(ctx: ToolContext, kind: str, text: str) -> ReviewNote:
    current = ctx.state.current_artifact("memo")
    return ReviewNote.model_validate(
        {
            "id": ctx.new_id("note"),
            "author": ctx.node,
            "kind": kind,
            "text": text,
            "artifact_id": current.id if current else None,
            "artifact_version": current.version if current else None,
        }
    )


class RedlineArgs(BaseModel):
    original: str = Field(min_length=1, max_length=5_000)
    suggested: str = Field(max_length=5_000)
    rationale: str = Field(min_length=1, max_length=1_000)


async def _redline(ctx: ToolContext, args: RedlineArgs) -> ToolResult:
    text = json.dumps(
        {"original": args.original, "suggested": args.suggested, "rationale": args.rationale}
    )
    note = _note(ctx, "redline", text)
    return ToolResult(
        output={"note_id": note.id}, ops=[AppendOp(path="review_notes", value=note.model_dump())]
    )


class AddReviewNoteArgs(BaseModel):
    kind: Literal["unsupported_claim", "policy", "general"]
    text: str = Field(min_length=1, max_length=2_000)


async def _add_review_note(ctx: ToolContext, args: AddReviewNoteArgs) -> ToolResult:
    note = _note(ctx, args.kind, args.text)
    return ToolResult(
        output={"note_id": note.id}, ops=[AppendOp(path="review_notes", value=note.model_dump())]
    )


ALL_TOOLS: tuple[Tool, ...] = (  # type: ignore[type-arg]
    Tool(
        "web_search",
        "Search the web. Results are added to the run's sources.",
        WebSearchArgs,
        _web_search,
    ),
    Tool(
        "rag_query",
        "Query the Document Q&A knowledge base. Results are added to sources.",
        RagQueryArgs,
        _rag_query,
    ),
    Tool(
        "summarize", "Summarize a long text, keeping citation markers.", SummarizeArgs, _summarize
    ),
    Tool(
        "write_artifact",
        "Write the full memo (markdown). Creates a new memo version.",
        WriteArtifactArgs,
        _write_artifact,
    ),
    Tool(
        "edit_section",
        "Replace the body of one '## heading' section of the memo.",
        EditSectionArgs,
        _edit_section,
    ),
    Tool(
        "spawn_sandbox",
        "Run code in an isolated sandbox (currently a stub).",
        SpawnSandboxArgs,
        _spawn_sandbox,
    ),
    Tool(
        "policy_check",
        "Check the current memo for uncited claims, unknown sources and PII.",
        PolicyCheckArgs,
        _policy_check,
    ),
    Tool("redline", "Record a suggested edit to the memo.", RedlineArgs, _redline),
    Tool(
        "add_review_note",
        "Record a review finding about the memo.",
        AddReviewNoteArgs,
        _add_review_note,
    ),
)
