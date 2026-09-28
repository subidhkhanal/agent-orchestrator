"""Prometheus metrics, derived from the event stream.

Instead of sprinkling counters through the engine, MetricsEventLog wraps the event log and
updates metrics from the events that are actually committed. The audit log and the metrics
therefore cannot disagree about what happened.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from orchestrator.events.log import Event, EventLog, EventType

NODE_LATENCY = Histogram(
    "orchestrator_node_latency_seconds",
    "Wall-clock time per node execution",
    ["node"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 80),
)
NODE_TOKENS = Counter("orchestrator_node_tokens_total", "LLM tokens spent", ["node"])
NODE_USD = Counter("orchestrator_node_usd_total", "LLM spend in USD", ["node"])
LLM_CALLS = Counter("orchestrator_llm_calls_total", "LLM calls", ["node", "model", "status"])
GUARD_OVERRIDES = Counter("orchestrator_guard_overrides_total", "Code guard overrides", ["guard"])
ROUTES = Counter("orchestrator_routes_total", "Supervisor routing outcomes", ["outcome"])
TOOL_DENIALS = Counter(
    "orchestrator_tool_denials_total", "Out-of-allowlist tool calls", ["role", "tool"]
)
RUNS_FINISHED = Counter(
    "orchestrator_runs_finished_total", "Runs reaching a terminal state", ["status"]
)
WAITING = Counter("orchestrator_rate_limit_waits_total", "429 backoffs", ["model"])
RUNS_BY_STATUS = Gauge("orchestrator_runs", "Runs by status (sampled at scrape)", ["status"])
HTTP_REQUESTS = Counter("orchestrator_http_requests_total", "API requests", ["route", "status"])


def observe(event_type: EventType, payload: dict[str, Any]) -> None:
    node = str(payload.get("node", ""))
    if event_type == EventType.NODE_COMPLETED:
        NODE_LATENCY.labels(node).observe(payload.get("latency_ms", 0) / 1000)
        NODE_TOKENS.labels(node).inc(payload.get("tokens", 0))
        NODE_USD.labels(node).inc(payload.get("usd", 0.0))
    elif event_type == EventType.LLM_CALL:
        LLM_CALLS.labels(node, payload.get("model", ""), payload.get("status", "")).inc()
    elif event_type == EventType.GUARD_OVERRIDE:
        GUARD_OVERRIDES.labels(payload.get("guard", "")).inc()
    elif event_type == EventType.ROUTE_DECIDED:
        ROUTES.labels("valid").inc()
    elif event_type == EventType.ROUTE_INVALID:
        ROUTES.labels("invalid").inc()
    elif event_type == EventType.TOOL_DENIED:
        TOOL_DENIALS.labels(payload.get("role", ""), payload.get("tool", "")).inc()
    elif event_type == EventType.WAITING:
        WAITING.labels(payload.get("model", "")).inc()
    elif event_type in (EventType.COMPLETED, EventType.FAILED, EventType.CANCELLED):
        RUNS_FINISHED.labels(str(event_type)).inc()


class MetricsEventLog:
    def __init__(self, inner: EventLog) -> None:
        self._inner = inner

    async def append(
        self,
        run_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        idempotency_key: str,
        attempt: int | None = None,
    ) -> Event | None:
        event = await self._inner.append(run_id, event_type, payload, idempotency_key, attempt)
        if event is not None:
            observe(event_type, payload)
        return event

    async def read(self, run_id: str, after_event_id: int = 0, limit: int = 100) -> list[Event]:
        return await self._inner.read(run_id, after_event_id, limit)
