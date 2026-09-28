"""Deterministic fake LLM provider for tests and CI (no network, no cost).

Responses come from, in order:
1. a per-node script queue (each item: a FakeReply, an exception to raise, or a callable),
2. a policy function of the request,
otherwise the call fails loudly so a test never silently gets an empty answer.
Token usage is computed from text length, so budget math is exercised for real.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orchestrator.gateway.types import LLMRequest, LLMResponse, ToolCall, Usage


@dataclass(frozen=True)
class FakeReply:
    content: str = ""
    tool_calls: tuple[tuple[str, dict[str, Any]], ...] = ()


Policy = Callable[[LLMRequest, str], "FakeReply | Exception"]
ScriptItem = FakeReply | Exception | Policy


@dataclass(frozen=True)
class FakeCall:
    node: str
    model: str
    max_tokens: int
    request: LLMRequest


@dataclass
class FakeLLM:
    policy: Policy | None = None
    script: dict[str, list[ScriptItem]] = field(default_factory=dict)
    name: str = "fake"
    # Real (wall-clock) latency per call. Only the chaos script uses it, so that killing a
    # worker lands in the middle of a run rather than after it.
    delay_s: float = 0.0
    calls: list[FakeCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queues: dict[str, deque[ScriptItem]] = defaultdict(deque)
        for node, items in self.script.items():
            self._queues[node].extend(items)

    def push(self, node: str, *items: ScriptItem) -> None:
        self._queues[node].extend(items)

    async def complete(
        self,
        request: LLMRequest,
        *,
        model: str,
        max_tokens: int,
        params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.calls.append(FakeCall(request.node, model, max_tokens, request))
        queue = self._queues[request.node]
        item: ScriptItem | None = queue.popleft() if queue else self.policy
        if item is None:
            raise AssertionError(f"FakeLLM has no reply for node {request.node!r}")
        reply = item(request, model) if callable(item) and not isinstance(item, FakeReply) else item
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, FakeReply)

        tool_calls = tuple(
            ToolCall(id=f"call_{len(self.calls)}_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(reply.tool_calls)
        )
        out_chars = len(reply.content) + sum(len(json.dumps(a)) for _, a in reply.tool_calls)
        in_chars = sum(len(m.content) for m in request.messages)
        usage = Usage(
            input_tokens=math.ceil(in_chars / 4),
            output_tokens=min(max_tokens, max(1, math.ceil(out_chars / 4))),
        )
        return LLMResponse(
            content=reply.content,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            provider=self.name,
        )
