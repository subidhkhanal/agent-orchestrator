"""Real clients for the researcher's services: Tavily web search and the Document Q&A API."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from orchestrator.tools.base import ToolError
from orchestrator.tools.services import RagHit, SearchHit


class UnavailableSearch:
    """Used when no search provider is configured, so real runs never see canned results."""

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        raise ToolError("web search is not configured on this deployment")


class TavilySearch:
    """Tavily search API (free tier: 1,000 searches/month, no card)."""

    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.AsyncClient(timeout=20)

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        try:
            response = await self._client.post(
                self.URL,
                headers=self._headers,
                json={"query": query, "max_results": max_results, "search_depth": "basic"},
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"web search unavailable: {exc!r}") from exc
        if response.status_code >= 400:
            raise ToolError(f"web search failed: HTTP {response.status_code}")
        return [
            SearchHit(
                title=r.get("title") or r["url"],
                url=r["url"],
                snippet=(r.get("content") or "")[:600],
            )
            for r in response.json().get("results", [])
        ]


class DocQaClient:
    """Client for the Document Q&A (RAG) platform.

    Two API generations exist:
    - v1 (current code): guest session via POST /api/v1/auth/guest, then
      POST /api/v1/qa/query with stream=false returns authorized evidence passages.
    - legacy (what the deployed instance serves as of 2026-09-28): public POST /api/query that
      streams SSE and ends with a `done` event listing the retrieved source chunks.
    The client tries v1 first and remembers if it has to fall back. Only retrieved passages
    become sources; the platform's own generated answer is never used as a source.
    """

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=90)
        self._token: str | None = None
        self._legacy: bool | None = None
        self._lock = asyncio.Lock()

    async def query(self, question: str, top_k: int) -> list[RagHit]:
        try:
            if self._legacy is not True:
                hits = await self._query_v1(question, top_k)
                if hits is not None:
                    self._legacy = False
                    return hits
                self._legacy = True
            return await self._query_legacy(question, top_k)
        except httpx.HTTPError as exc:
            raise ToolError(f"document search unavailable: {exc!r}") from exc

    # --- v1 --------------------------------------------------------------------------------

    async def _auth(self, refresh: bool = False) -> str | None:
        async with self._lock:
            if self._token is None or refresh:
                response = await self._client.post(f"{self._base}/api/v1/auth/guest")
                if response.status_code == 404:
                    return None
                if response.status_code >= 400:
                    raise ToolError(f"document search login failed: HTTP {response.status_code}")
                self._token = response.json()["access_token"]
            return self._token

    async def _query_v1(self, question: str, top_k: int) -> list[RagHit] | None:
        """Stream the query and keep only the `citation` (evidence) events.

        The platform emits its authorized evidence before it starts generating an answer, so
        we disconnect when generation starts: we never use its answer, and this keeps working
        even when its answer model is unavailable.
        """
        for refresh in (False, True):
            token = await self._auth(refresh=refresh)
            if token is None:
                return None
            evidence: list[dict[str, Any]] = []
            async with self._client.stream(
                "POST",
                f"{self._base}/api/v1/qa/query",
                headers={"Authorization": f"Bearer {token}"},
                json={"query": question, "stream": True},
            ) as response:
                if response.status_code == 401 and not refresh:
                    continue
                if response.status_code >= 400:
                    raise ToolError(f"document search failed: HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type")
                    if kind == "citation":
                        evidence.append(event)
                    elif kind in ("done", "error") or (
                        kind == "status" and event.get("stage") == "generating"
                    ):
                        break
            return [self._hit(ev) for ev in evidence[:top_k]]
        raise ToolError("document search: not authorized")

    @staticmethod
    def _hit(ev: dict[str, Any]) -> RagHit:
        title = ev.get("source_name") or f"document {ev.get('document_id')}"
        if ev.get("section_title") not in (None, "None", ""):
            title += f" - {ev['section_title']}"
        ref = (
            f"docqa://documents/{ev.get('document_id')}/v{ev.get('document_version')}"
            f"#chunk={ev.get('chunk_id')}"
        )
        if ev.get("page_number") not in (None, "None"):
            ref += f"&page={ev['page_number']}"
        return RagHit(title=title, doc_ref=ref, snippet=(ev.get("text") or "")[:800])

    # --- legacy ----------------------------------------------------------------------------

    async def _query_legacy(self, question: str, top_k: int) -> list[RagHit]:
        response = await self._client.post(
            f"{self._base}/api/query", json={"question": question, "top_k": top_k}
        )
        if response.status_code >= 400:
            raise ToolError(f"document search failed: HTTP {response.status_code}")
        done: dict[str, object] = {}
        for line in response.text.splitlines():
            if line.startswith("data: "):
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "done":
                    done = event
        hits = []
        for src in done.get("sources", [])[:top_k]:  # type: ignore[index]
            ref = f"docqa://{src.get('source')}#chunk={src.get('chunk_id')}"
            if src.get("page") is not None:
                ref += f"&page={src['page']}"
            hits.append(
                RagHit(
                    title=str(src.get("source")), doc_ref=ref, snippet=(src.get("text") or "")[:600]
                )
            )
        return hits
