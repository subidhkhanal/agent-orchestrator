"""External services the tools depend on, as narrow interfaces.

Real implementations (web search provider, the Document Q&A RAG client, the Postgres artifact
store) arrive in later milestones. The in-memory fakes here keep unit tests offline.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str


class RagHit(BaseModel):
    title: str
    doc_ref: str
    snippet: str


class SearchProvider(Protocol):
    async def search(self, query: str, max_results: int) -> list[SearchHit]: ...


class RagClient(Protocol):
    async def query(self, question: str, top_k: int) -> list[RagHit]: ...


class ArtifactStore(Protocol):
    async def put(
        self,
        *,
        run_id: str,
        tenant_id: str,
        artifact_id: str,
        version: int,
        type_: str,
        content: str,
    ) -> str:
        """Store content and return its content_ref."""
        ...

    async def get(self, content_ref: str, *, tenant_id: str) -> str: ...


class Sandbox(Protocol):
    async def run(self, language: str, code: str, timeout_s: float) -> dict[str, Any]: ...


@dataclass
class ToolServices:
    search: SearchProvider
    rag: RagClient
    artifacts: ArtifactStore
    sandbox: Sandbox


def artifact_ref(run_id: str, artifact_id: str, version: int, content: str) -> str:
    """Content-addressed reference.

    The hash matters after a crash: a node that re-executes may produce *different* content
    for the same version number (LLM output is not deterministic). Both copies are kept
    (storage is append-only); state points at whichever one its committed patch named.
    """
    digest = hashlib.sha256(content.encode()).hexdigest()[:16]
    return f"artifact://{run_id}/{artifact_id}/v{version}/{digest}"


def parse_artifact_ref(ref: str) -> tuple[str, str, int, str]:
    run_id, artifact_id, version, digest = ref.removeprefix("artifact://").split("/")
    return run_id, artifact_id, int(version.removeprefix("v")), digest


# --- In-memory implementations -------------------------------------------------------------


@dataclass
class StaticSearch:
    """Returns canned hits. Matches when every word of a key appears in the query."""

    corpus: dict[str, list[SearchHit]] = field(default_factory=dict)
    default: list[SearchHit] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        self.queries.append(query)
        q = query.lower()
        for key, hits in self.corpus.items():
            if all(word in q for word in key.lower().split()):
                return hits[:max_results]
        return self.default[:max_results]


@dataclass
class StaticRag:
    hits: list[RagHit] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    async def query(self, question: str, top_k: int) -> list[RagHit]:
        self.questions.append(question)
        return self.hits[:top_k]


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._content: dict[str, tuple[str, str]] = {}  # content_ref -> (tenant_id, content)

    async def put(
        self,
        *,
        run_id: str,
        tenant_id: str,
        artifact_id: str,
        version: int,
        type_: str,
        content: str,
    ) -> str:
        ref = artifact_ref(run_id, artifact_id, version, content)
        self._content[ref] = (tenant_id, content)
        return ref

    async def get(self, content_ref: str, *, tenant_id: str) -> str:
        owner, content = self._content[content_ref]
        if owner != tenant_id:
            raise KeyError(content_ref)
        return content


class StubSandbox:
    """Placeholder until the autonomous-coding-agent project provides a real sandbox."""

    async def run(self, language: str, code: str, timeout_s: float) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "detail": "code execution sandbox is not implemented in this project",
            "language": language,
        }
