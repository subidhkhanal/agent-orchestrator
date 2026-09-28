from __future__ import annotations

import json

import httpx
import pytest

from orchestrator.agents.workers import COMPACTED_CHARS, KEEP_RECENT_TOOL_RESULTS, compact
from orchestrator.gateway.providers.openai_compatible import OpenAICompatibleProvider
from orchestrator.gateway.types import (
    InvalidModelOutput,
    LLMRequest,
    Message,
    ProviderError,
    RateLimitError,
)

REQUEST = LLMRequest(
    node="supervisor",
    messages=(Message(role="user", content="route"),),
    response_schema={"type": "object"},
)


def provider(handler: httpx.MockTransport) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "groq", "https://api.test/v1", "key", client=httpx.AsyncClient(transport=handler)
    )


@pytest.mark.parametrize("code", ["json_validate_failed", "tool_use_failed", "output_parse_failed"])
async def test_rejected_model_output_is_a_retryable_invalid_output_error(code: str) -> None:
    body = {"error": {"code": code, "failed_generation": "not json"}}
    p = provider(httpx.MockTransport(lambda r: httpx.Response(400, json=body)))
    with pytest.raises(InvalidModelOutput) as exc:
        await p.complete(REQUEST, model="m", max_tokens=500)
    assert exc.value.code == code
    assert exc.value.usage.output_tokens == 500  # worst case charged when usage is unknown


async def test_other_errors_and_rate_limits_are_classified() -> None:
    p = provider(httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(ProviderError):
        await p.complete(REQUEST, model="m", max_tokens=10)
    p = provider(
        httpx.MockTransport(
            lambda r: httpx.Response(429, text="slow", headers={"retry-after": "7"})
        )
    )
    with pytest.raises(RateLimitError) as exc:
        await p.complete(REQUEST, model="m", max_tokens=10)
    assert exc.value.retry_after_s == 7


async def test_request_body_carries_tools_json_mode_and_route_params() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}", "tool_calls": None}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    await provider(httpx.MockTransport(handler)).complete(
        REQUEST, model="m", max_tokens=10, params={"reasoning_effort": "low"}
    )
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["reasoning_effort"] == "low" and seen["max_tokens"] == 10


def test_compaction_shortens_only_older_tool_results() -> None:
    long = "x" * 1000
    msgs = [Message(role="system", content="s")] + [
        Message(role="tool", content=long, tool_call_id=str(i)) for i in range(5)
    ]
    out = compact(msgs)
    tool = [m for m in out if m.role == "tool"]
    shortened = [m for m in tool if len(m.content) < 1000]
    assert len(shortened) == 5 - KEEP_RECENT_TOOL_RESULTS
    assert all(len(m.content) <= COMPACTED_CHARS + 20 for m in shortened)
    assert tool[-1].content == long and out[0].content == "s"
