"""Provider for Claude via the official Anthropic Python SDK.

Mapping from the gateway's provider-neutral types:
- the first `system` message becomes the top-level `system` parameter;
- an assistant message produced by Claude is sent back with its *raw* content blocks
  (`Message.raw_content`), unchanged. Claude's thinking blocks must be replayed exactly as
  received in a tool loop; rebuilding the turn from text + tool calls would drop them;
- consecutive `tool` messages become one user turn of `tool_result` blocks (parallel tool
  results belong in a single message).

Retries are the gateway's job (backoff bounded by the run deadline, `waiting` events), so the
SDK's own retries are disabled. Route `params` from config/models.toml are passed through as
request arguments (effort, refusal fallbacks, beta flags), so tuning them is a config change.
"""

from __future__ import annotations

from typing import Any

import anthropic

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


def _assistant_content(m: Message) -> list[dict[str, Any]] | str:
    if m.raw_content:
        return list(m.raw_content)
    blocks: list[dict[str, Any]] = []
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    for call in m.tool_calls:
        blocks.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return blocks or m.content or "(no content)"


def to_anthropic_messages(messages: tuple[Message, ...]) -> tuple[str | None, list[dict[str, Any]]]:
    system: str | None = None
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        if m.role == "system" and system is None and not out:
            system = m.content
            continue
        if m.role == "tool":
            pending_results.append(
                {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            )
            continue
        flush()
        if m.role == "assistant":
            out.append({"role": "assistant", "content": _assistant_content(m)})
        else:  # user (or a later system message, folded into a user turn)
            out.append({"role": "user", "content": m.content or "(empty)"})
    flush()
    return system, out


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class AnthropicProvider:
    def __init__(
        self,
        name: str,
        api_key: str,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.name = name
        # The gateway owns retries and the per-call timeout (it is capped by the run deadline).
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=0, timeout=600.0
        )

    def build_kwargs(
        self, request: LLMRequest, model: str, max_tokens: int, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        system, messages = to_anthropic_messages(request.messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            # Automatic prompt caching: tool loops resend a growing, append-only history.
            "cache_control": {"type": "ephemeral"},
            **(params or {}),
        }
        if system:
            kwargs["system"] = system
        if request.tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
        return kwargs

    async def complete(
        self,
        request: LLMRequest,
        *,
        model: str,
        max_tokens: int,
        params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        kwargs = self.build_kwargs(request, model, max_tokens, params)
        try:
            # The beta namespace accepts both GA requests and beta flags passed via params.
            response = await self._client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise RateLimitError(str(exc)[:300], retry_after_s=_retry_after(exc)) from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeout(str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"connection error: {exc!r}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"HTTP {exc.status_code}: {str(exc)[:300]}") from exc

        if response.stop_reason == "refusal":
            # The whole fallback chain declined. Not retryable on the same route.
            raise ProviderError("model declined the request (stop_reason=refusal)")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {"_value": block.input}
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=args))

        if response.stop_reason == "max_tokens" and not text_parts and not tool_calls:
            # Output budget spent before anything usable (e.g. all on thinking): resample.
            raise InvalidModelOutput(
                "max_tokens",
                usage=Usage(input_tokens=estimate_input_tokens(request), output_tokens=max_tokens),
            )

        usage = response.usage
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tuple(tool_calls),
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
            ),
            model=response.model,
            provider=self.name,
            raw_content=tuple(block.to_dict() for block in response.content),
        )

    async def list_models(self) -> set[str]:
        return {m.id async for m in self._client.models.list()}
