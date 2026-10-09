"""Claude provider against a mocked HTTP transport (no network, no cost)."""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2
import pytest

from orchestrator.gateway.config import Price
from orchestrator.gateway.providers.anthropic_provider import (
    AnthropicProvider,
    to_anthropic_messages,
)
from orchestrator.gateway.types import (
    InvalidModelOutput,
    LLMRequest,
    Message,
    ProviderError,
    RateLimitError,
    ToolCall,
    ToolSpec,
)

THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "web_search", "input": {"query": "x"}}


def message_json(content: list[dict[str, Any]], stop: str = "tool_use") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
        },
    }


def provider(handler: Any) -> tuple[AnthropicProvider, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(record)),
    )
    return AnthropicProvider("anthropic", "test", client=client), seen


REQUEST = LLMRequest(
    node="researcher",
    messages=(
        Message(role="system", content="You are the researcher."),
        Message(role="user", content="Find sources."),
    ),
    tools=(ToolSpec(name="web_search", description="search", parameters={"type": "object"}),),
)


def test_message_mapping_keeps_raw_blocks_and_groups_tool_results() -> None:
    messages = (
        Message(role="system", content="sys"),
        Message(role="user", content="task"),
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id="toolu_1", name="web_search", arguments={"query": "x"}),),
            raw_content=(THINKING, TOOL_USE),
        ),
        Message(role="tool", content='{"r": 1}', tool_call_id="toolu_1"),
        Message(role="tool", content='{"r": 2}', tool_call_id="toolu_2"),
        Message(role="assistant", content="plain", tool_calls=()),
    )
    system, out = to_anthropic_messages(messages)
    assert system == "sys"
    assert out[1] == {"role": "assistant", "content": [THINKING, TOOL_USE]}  # replayed unchanged
    assert out[2]["role"] == "user"
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["toolu_1", "toolu_2"]  # one turn
    assert out[3] == {"role": "assistant", "content": [{"type": "text", "text": "plain"}]}


async def test_tool_call_response_and_request_shape() -> None:
    p, seen = provider(lambda r: httpx2.Response(200, json=message_json([THINKING, TOOL_USE])))
    response = await p.complete(
        REQUEST,
        model="claude-opus-5-5",
        max_tokens=16000,
        params={
            "output_config": {"effort": "medium"},
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        },
    )
    assert response.tool_calls == (
        ToolCall(id="toolu_1", name="web_search", arguments={"query": "x"}),
    )
    assert response.raw_content is not None and response.raw_content[0]["signature"] == "sig-abc"
    usage = response.usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (10, 100, 20)

    body = json.loads(seen[0].content)
    assert body["system"] == "You are the researcher."
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "medium"} and body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0].headers.get("anthropic-beta", "")


async def test_rate_limit_carries_retry_after() -> None:
    p, _ = provider(
        lambda r: httpx2.Response(
            429,
            headers={"retry-after": "7"},
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        )
    )
    with pytest.raises(RateLimitError) as exc:
        await p.complete(REQUEST, model="claude-opus-5-5", max_tokens=100)
    assert exc.value.retry_after_s == 7


async def test_refusal_is_a_provider_error_and_thinking_only_truncation_is_resampled() -> None:
    refused = provider(lambda r: httpx2.Response(200, json=message_json([], stop="refusal")))[0]
    with pytest.raises(ProviderError):
        await refused.complete(REQUEST, model="claude-opus-5-5", max_tokens=100)

    truncated = provider(
        lambda r: httpx2.Response(200, json=message_json([THINKING], stop="max_tokens"))
    )[0]
    with pytest.raises(InvalidModelOutput):
        await truncated.complete(REQUEST, model="claude-opus-5-5", max_tokens=100)


def test_cache_tokens_are_priced_with_read_and_write_multipliers() -> None:
    price = Price(input_per_mtok=4.0, output_per_mtok=20.0)
    # 1M uncached input ($4) + 1M cache reads ($0.40) + 1M cache writes ($5) + 1M output ($20)
    assert price.cost(1_000_000, 1_000_000, 1_000_000, 1_000_000) == pytest.approx(29.40)
