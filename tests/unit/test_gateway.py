from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from orchestrator.clock import FakeClock
from orchestrator.events.log import EventType
from orchestrator.gateway.budget import BudgetExhausted, BudgetLedger, DeadlineExceeded
from orchestrator.gateway.config import GatewayConfig
from orchestrator.gateway.gateway import AllProvidersFailed, LLMGateway
from orchestrator.gateway.providers.fake import FakeLLM, FakeReply
from orchestrator.gateway.types import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderError,
    RateLimitError,
)


def config(max_retries: int = 3) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "providers": {"p1": {"kind": "fake"}, "p2": {"kind": "fake"}},
            "tiers": {
                "t": {
                    "primary": {"provider": "p1", "model": "m1"},
                    "fallback": {"provider": "p2", "model": "m2"},
                    "max_output_tokens": 500,
                    "timeout_s": 30,
                }
            },
            "nodes": {
                "researcher": {"tier": "t", "max_tokens_per_node": 100_000, "max_usd_per_node": 1}
            },
            "prices": {
                "m1": {"input_per_mtok": 1.0, "output_per_mtok": 2.0},
                "m2": {"input_per_mtok": 1.0, "output_per_mtok": 2.0},
            },
            "retry": {"max_retries": max_retries, "base_delay_s": 1.0, "max_delay_s": 8.0},
        }
    )


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    async def __call__(self, event_type: EventType, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    def of(self, event_type: EventType) -> list[dict[str, Any]]:
        return [p for t, p in self.events if t == event_type]


REQUEST = LLMRequest(node="researcher", messages=(Message(role="user", content="hello " * 20),))


def setup(
    p1: FakeLLM,
    p2: FakeLLM | None = None,
    *,
    deadline_s: float | None = None,
    retries: int = 3,
    usd: float = 1.0,
) -> tuple[LLMGateway, BudgetLedger, Recorder, FakeClock]:
    clock = FakeClock()
    gateway = LLMGateway(
        config(retries), {"p1": p1, "p2": p2 or FakeLLM(name="p2", policy=ok)}, clock
    )
    deadline = clock.now() + timedelta(seconds=deadline_s) if deadline_s is not None else None
    ledger = BudgetLedger(tokens_available=100_000, usd_available=usd, deadline_at=deadline)
    return gateway, ledger, Recorder(), clock


def ok(request: LLMRequest, model: str) -> FakeReply:
    return FakeReply(content=f"answer from {model}")


async def test_successful_call_is_charged_and_recorded() -> None:
    p1 = FakeLLM(name="p1", policy=ok)
    gateway, ledger, rec, _ = setup(p1)
    response = await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert response.model == "m1"
    [call] = rec.of(EventType.LLM_CALL)
    assert call["status"] == "ok" and call["model"] == "m1" and call["node"] == "researcher"
    assert call["input_tokens"] == response.usage.input_tokens
    assert call["usd"] == pytest.approx(ledger.usd_used) and ledger.usd_used > 0
    assert ledger.tokens_used == response.usage.total
    assert p1.calls[0].max_tokens == 500


async def test_rate_limit_backs_off_exponentially_and_emits_waiting() -> None:
    p1 = FakeLLM(name="p1", policy=ok, script={"researcher": [RateLimitError(), RateLimitError()]})
    gateway, ledger, rec, clock = setup(p1)
    response = await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert response.model == "m1"
    assert clock.sleeps == [1.0, 2.0]
    assert [w["delay_s"] for w in rec.of(EventType.WAITING)] == [1.0, 2.0]


async def test_retry_after_header_is_respected() -> None:
    p1 = FakeLLM(name="p1", policy=ok, script={"researcher": [RateLimitError(retry_after_s=5)]})
    gateway, ledger, rec, clock = setup(p1)
    await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert clock.sleeps == [5.0]


async def test_backoff_never_extends_past_the_run_deadline() -> None:
    p1 = FakeLLM(name="p1", script={"researcher": [RateLimitError(), RateLimitError()]})
    p2 = FakeLLM(name="p2", policy=ok)
    gateway, ledger, rec, clock = setup(p1, p2, deadline_s=1.5)
    response = await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    # First wait (1s) fits before the deadline; the second (2s) would not, so fall back.
    assert clock.sleeps == [1.0]
    assert response.model == "m2"
    assert clock.now() < ledger.deadline_at  # type: ignore[operator]


async def test_exhausted_retries_fall_back_to_secondary() -> None:
    p1 = FakeLLM(name="p1", script={"researcher": [RateLimitError()] * 3})
    gateway, ledger, rec, clock = setup(p1, retries=2)
    response = await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert response.model == "m2"
    assert clock.sleeps == [1.0, 2.0]


async def test_provider_error_falls_back_immediately() -> None:
    p1 = FakeLLM(name="p1", script={"researcher": [ProviderError("500")]})
    gateway, ledger, rec, clock = setup(p1)
    response = await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert response.model == "m2" and clock.sleeps == []
    statuses = [c["status"] for c in rec.of(EventType.LLM_CALL)]
    assert statuses == ["ProviderError", "ok"]


async def test_all_routes_failing_raises() -> None:
    p1 = FakeLLM(name="p1", script={"researcher": [ProviderError("down")]})
    p2 = FakeLLM(name="p2", script={"researcher": [ProviderError("down too")]})
    gateway, ledger, rec, _ = setup(p1, p2)
    with pytest.raises(AllProvidersFailed):
        await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert ledger.usd_used == 0


async def test_no_call_is_made_when_the_budget_cannot_cover_it() -> None:
    p1 = FakeLLM(name="p1", policy=ok)
    gateway, ledger, rec, _ = setup(p1, usd=0.00001)
    with pytest.raises(BudgetExhausted):
        await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert p1.calls == []


async def test_no_call_is_made_after_the_deadline() -> None:
    p1 = FakeLLM(name="p1", policy=ok)
    gateway, ledger, rec, clock = setup(p1, deadline_s=1)
    clock.advance(2)
    with pytest.raises(DeadlineExceeded):
        await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert p1.calls == []


class SlowProvider:
    name = "p1"

    async def complete(
        self, request: LLMRequest, *, model: str, max_tokens: int, params: object = None
    ) -> LLMResponse:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")


async def test_call_timeout_is_capped_by_the_deadline_then_falls_back() -> None:
    clock = FakeClock()
    gateway = LLMGateway(
        config(), {"p1": SlowProvider(), "p2": FakeLLM(name="p2", policy=ok)}, clock
    )
    ledger = BudgetLedger(100_000, 1.0, deadline_at=clock.now() + timedelta(seconds=0.05))
    rec = Recorder()
    response = await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert response.model == "m2"
    assert rec.of(EventType.LLM_CALL)[0]["status"] == "timeout"


def test_config_rejects_models_without_prices() -> None:
    data = config().model_dump()
    del data["prices"]["m2"]
    with pytest.raises(ValueError, match="no price"):
        GatewayConfig.model_validate(data)


def capped_config(max_request_tokens: int) -> GatewayConfig:
    data = config().model_dump()
    data["tiers"]["t"]["max_request_tokens"] = max_request_tokens
    return GatewayConfig.model_validate(data)


async def test_output_reservation_shrinks_to_fit_the_per_request_limit() -> None:
    p1 = FakeLLM(name="p1", policy=ok)
    clock = FakeClock()
    gateway = LLMGateway(capped_config(300), {"p1": p1, "p2": FakeLLM(name="p2")}, clock)
    ledger = BudgetLedger(100_000, 1.0, deadline_at=None)
    await gateway.complete(REQUEST, ledger=ledger, emit=Recorder())
    from orchestrator.gateway.budget import estimate_input_tokens

    assert p1.calls[0].max_tokens == 300 - estimate_input_tokens(REQUEST)


async def test_prompt_too_large_for_the_request_limit_is_refused_before_calling() -> None:
    from orchestrator.gateway.gateway import RequestTooLarge

    p1 = FakeLLM(name="p1", policy=ok)
    gateway = LLMGateway(capped_config(60), {"p1": p1, "p2": FakeLLM(name="p2")}, FakeClock())
    with pytest.raises(RequestTooLarge):
        await gateway.complete(
            REQUEST, ledger=BudgetLedger(100_000, 1.0, deadline_at=None), emit=Recorder()
        )
    assert p1.calls == []


async def test_invalid_model_output_is_resampled_on_the_same_route_and_charged() -> None:
    from orchestrator.gateway.types import InvalidModelOutput, Usage

    bad = InvalidModelOutput("tool_use_failed", Usage(input_tokens=100, output_tokens=50))
    p1 = FakeLLM(name="p1", policy=ok, script={"researcher": [bad, bad]})
    gateway, ledger, rec, _ = setup(p1)
    response = await gateway.complete(REQUEST, ledger=ledger, emit=rec)
    assert response.model == "m1"  # same route; the third sample succeeded
    statuses = [c["status"] for c in rec.of(EventType.LLM_CALL)]
    assert statuses == ["invalid_output:tool_use_failed"] * 2 + ["ok"]
    assert ledger.tokens_used >= 300


async def test_persistent_invalid_output_falls_back() -> None:
    from orchestrator.gateway.types import InvalidModelOutput, Usage

    bad = InvalidModelOutput("json_validate_failed", Usage(input_tokens=1, output_tokens=1))
    p1 = FakeLLM(name="p1", script={"researcher": [bad, bad, bad]})
    gateway, ledger, rec, _ = setup(p1)
    assert (await gateway.complete(REQUEST, ledger=ledger, emit=rec)).model == "m2"
