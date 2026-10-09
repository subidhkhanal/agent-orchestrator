"""The LLM gateway: the only code path from a node to a model provider.

For every call it:
- picks the model for the node's tier from config,
- grants a max_output_tokens that fits the node's remaining budget (the waterfall),
- caps the call timeout by the run deadline,
- retries 429s with exponential backoff (emitting a `waiting` event), never past the deadline,
- falls back to the tier's secondary route on failure,
- charges actual usage and records an `llm_call` event.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Protocol

from orchestrator.clock import Clock
from orchestrator.events.emitter import Emitter
from orchestrator.events.log import EventType
from orchestrator.gateway.budget import (
    MIN_OUTPUT_TOKENS,
    BudgetLedger,
    DeadlineExceeded,
    estimate_input_tokens,
)
from orchestrator.gateway.config import GatewayConfig, Route
from orchestrator.gateway.types import (
    InvalidModelOutput,
    LLMRequest,
    LLMResponse,
    ProviderError,
    ProviderTimeout,
    RateLimitError,
)


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        request: LLMRequest,
        *,
        model: str,
        max_tokens: int,
        params: dict[str, Any] | None = None,
    ) -> LLMResponse: ...


class AllProvidersFailed(Exception):
    pass


class RequestTooLarge(Exception):
    """The prompt alone is too big for the tier's per-request limit."""


class LLMGateway:
    def __init__(
        self, config: GatewayConfig, providers: dict[str, LLMProvider], clock: Clock
    ) -> None:
        missing = set(config.providers) - set(providers)
        if missing:
            raise ValueError(f"no provider implementation for {sorted(missing)}")
        self.config = config
        self._providers = providers
        self._clock = clock

    async def complete(
        self, request: LLMRequest, *, ledger: BudgetLedger, emit: Emitter
    ) -> LLMResponse:
        tier = self.config.tier_for(request.node)
        max_output = tier.max_output_tokens
        if tier.max_request_tokens is not None:
            # Waterfall one level further down: the request itself must fit the provider's
            # per-request limit, so the output reservation shrinks as the prompt grows.
            max_output = min(max_output, tier.max_request_tokens - estimate_input_tokens(request))
            if max_output < MIN_OUTPUT_TOKENS:
                raise RequestTooLarge(
                    f"prompt for {request.node!r} (~{estimate_input_tokens(request)} tokens) "
                    f"leaves no room under the {tier.max_request_tokens}-token request limit"
                )
        errors: list[str] = []
        for route in tier.routes():
            try:
                return await self._call_route(
                    request, route, max_output, tier.timeout_s, ledger, emit
                )
            except ProviderError as exc:
                errors.append(f"{route.provider}/{route.model}: {exc!r}")
        raise AllProvidersFailed("; ".join(errors))

    async def _call_route(
        self,
        request: LLMRequest,
        route: Route,
        tier_max_output: int,
        tier_timeout_s: float,
        ledger: BudgetLedger,
        emit: Emitter,
    ) -> LLMResponse:
        price = self.config.prices[route.model]
        provider = self._providers[route.provider]
        retry = self.config.retry

        attempt = 0
        invalid_outputs = 0
        while True:
            # Re-grant on every attempt: time and budget may have moved.
            max_tokens = ledger.grant(estimate_input_tokens(request), tier_max_output, price)
            timeout_s = self._timeout(tier_timeout_s, ledger)
            started = self._clock.now()
            try:
                response = await asyncio.wait_for(
                    provider.complete(
                        request, model=route.model, max_tokens=max_tokens, params=route.params
                    ),
                    timeout=timeout_s,
                )
            except TimeoutError:
                await self._record_failure(emit, request, route, started, "timeout")
                raise ProviderTimeout(f"no response within {timeout_s:.1f}s") from None
            except InvalidModelOutput as exc:
                usd = ledger.charge(exc.usage, price)
                await emit(
                    EventType.LLM_CALL,
                    {
                        "node": request.node,
                        "provider": route.provider,
                        "model": route.model,
                        "status": f"invalid_output:{exc.code}",
                        "input_tokens": exc.usage.input_tokens,
                        "output_tokens": exc.usage.output_tokens,
                        "usd": usd,
                        "latency_ms": self._elapsed_ms(started),
                    },
                )
                invalid_outputs += 1
                if invalid_outputs > retry.max_invalid_output_retries:
                    raise
                continue
            except RateLimitError as exc:
                await self._record_failure(emit, request, route, started, "rate_limited")
                if attempt >= retry.max_retries:
                    raise
                delay = min(retry.max_delay_s, retry.base_delay_s * 2**attempt)
                if exc.retry_after_s is not None:
                    delay = max(delay, exc.retry_after_s)
                if not self._fits_before_deadline(delay, ledger):
                    # Waiting would overrun the run deadline; let the fallback route try.
                    raise
                await emit(
                    EventType.WAITING,
                    {
                        "node": request.node,
                        "provider": route.provider,
                        "model": route.model,
                        "reason": "rate_limited",
                        "delay_s": delay,
                        "attempt": attempt + 1,
                    },
                )
                await self._clock.sleep(delay)
                attempt += 1
                continue
            except ProviderError as exc:
                await self._record_failure(emit, request, route, started, type(exc).__name__)
                raise

            usd = ledger.charge(response.usage, price)
            await emit(
                EventType.LLM_CALL,
                {
                    "node": request.node,
                    "provider": route.provider,
                    "model": route.model,
                    "status": "ok",
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cache_read_tokens": response.usage.cache_read_tokens,
                    "cache_write_tokens": response.usage.cache_write_tokens,
                    "usd": usd,
                    "latency_ms": self._elapsed_ms(started),
                    "granted_max_tokens": max_tokens,
                },
            )
            return response

    def _timeout(self, tier_timeout_s: float, ledger: BudgetLedger) -> float:
        if ledger.deadline_at is None:
            return tier_timeout_s
        left = (ledger.deadline_at - self._clock.now()).total_seconds()
        if left <= 0:
            raise DeadlineExceeded("run deadline has passed")
        return min(tier_timeout_s, left)

    def _fits_before_deadline(self, delay_s: float, ledger: BudgetLedger) -> bool:
        if ledger.deadline_at is None:
            return True
        return (ledger.deadline_at - self._clock.now()).total_seconds() > delay_s

    def _elapsed_ms(self, started: datetime) -> int:
        return int((self._clock.now() - started).total_seconds() * 1000)

    async def _record_failure(
        self, emit: Emitter, request: LLMRequest, route: Route, started: datetime, status: str
    ) -> None:
        await emit(
            EventType.LLM_CALL,
            {
                "node": request.node,
                "provider": route.provider,
                "model": route.model,
                "status": status,
                "input_tokens": 0,
                "output_tokens": 0,
                "usd": 0.0,
                "latency_ms": self._elapsed_ms(started),
            },
        )
