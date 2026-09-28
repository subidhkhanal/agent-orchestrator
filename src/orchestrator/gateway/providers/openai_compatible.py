"""Provider for any OpenAI-compatible chat completions API (Groq, OpenRouter, vLLM, ...)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from orchestrator.gateway.budget import estimate_input_tokens
from orchestrator.gateway.types import (
    InvalidModelOutput,
    LLMRequest,
    LLMResponse,
    Message,
    ProviderError,
    ProviderTimeout,
    RateLimitError,
    ToolCall,
    Usage,
)


def _message(m: Message) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in m.tool_calls
        ]
    return out


INVALID_OUTPUT_CODES = {"json_validate_failed", "tool_use_failed", "output_parse_failed"}


def _error_code(response: httpx.Response) -> str | None:
    try:
        return str(response.json().get("error", {}).get("code"))
    except ValueError:
        return None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class OpenAICompatibleProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        # The gateway enforces the real per-call timeout; this is only a backstop.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0))

    def build_body(
        self, request: LLMRequest, model: str, max_tokens: int, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [_message(m) for m in request.messages],
            "max_tokens": max_tokens,
            **(params or {}),
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        elif request.response_schema is not None:
            # JSON mode, not strict schema mode: support for strict schemas differs per model,
            # and our own validation (with retry-on-error) is the real check anyway.
            body["response_format"] = {"type": "json_object"}
        return body

    async def complete(
        self,
        request: LLMRequest,
        *,
        model: str,
        max_tokens: int,
        params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        body = self.build_body(request, model, max_tokens, params)
        try:
            response = await self._client.post(self._url, headers=self._headers, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport error: {exc!r}") from exc

        if response.status_code == 429:
            raise RateLimitError(response.text[:300], retry_after_s=_retry_after(response))
        if response.status_code == 400:
            code = _error_code(response)
            if code in INVALID_OUTPUT_CODES:
                # The provider rejected what the *model* generated (invalid JSON, a tool call
                # that does not match its schema, unparseable output). A resample usually
                # works, so this is retryable on the same model, unlike a provider failure.
                raise InvalidModelOutput(
                    code,
                    usage=Usage(
                        input_tokens=estimate_input_tokens(request), output_tokens=max_tokens
                    ),
                )
        if response.status_code >= 400:
            raise ProviderError(f"HTTP {response.status_code}: {response.text[:300]}")

        data = response.json()
        message = data["choices"][0]["message"]
        tool_calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            raw = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                # Let the tool router report invalid arguments back to the model.
                args = {"_unparseable_arguments": raw}
            tool_calls.append(
                ToolCall(
                    id=call["id"],
                    name=call["function"]["name"],
                    arguments=args if isinstance(args, dict) else {"_value": args},
                )
            )
        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=tuple(tool_calls),
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            model=data.get("model", model),
            provider=self.name,
        )

    async def list_models(self) -> set[str]:
        response = await self._client.get(
            self._url.removesuffix("/chat/completions") + "/models", headers=self._headers
        )
        response.raise_for_status()
        return {m["id"] for m in response.json()["data"]}
