"""Builds the process-wide dependencies from Settings (used by the worker, API and scripts)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from orchestrator.clock import SystemClock
from orchestrator.config import Settings
from orchestrator.db.artifacts import PostgresArtifactStore
from orchestrator.db.effects import HttpPublishSink, LedgerPublisher, PostgresPublishSink
from orchestrator.db.events import PostgresEventLog
from orchestrator.db.pool import Pool
from orchestrator.db.runs import RunStore
from orchestrator.effects.publisher import Publisher
from orchestrator.engine.context import EngineDeps, EngineSettings
from orchestrator.gateway.config import GatewayConfig
from orchestrator.gateway.gateway import LLMGateway, LLMProvider
from orchestrator.gateway.providers.demo_policy import ResearchMemoPolicy
from orchestrator.gateway.providers.fake import FakeLLM
from orchestrator.gateway.providers.openai_compatible import OpenAICompatibleProvider
from orchestrator.metrics import MetricsEventLog
from orchestrator.offline import SAMPLE_RAG_HITS, SAMPLE_SEARCH_HITS
from orchestrator.tools import default_registry as default_tools
from orchestrator.tools.clients import DocQaClient, TavilySearch, UnavailableSearch
from orchestrator.tools.services import (
    RagClient,
    SearchProvider,
    StaticRag,
    StaticSearch,
    StubSandbox,
    ToolServices,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class ModelUnavailable(Exception):
    pass


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    return Settings()


def gateway_config(settings: Settings) -> GatewayConfig:
    path = "config/models.fake.toml" if settings.fake_llm else settings.models_config
    full = Path(path) if Path(path).is_absolute() else REPO_ROOT / path
    return GatewayConfig.load(full)


def build_providers(config: GatewayConfig, settings: Settings) -> dict[str, LLMProvider]:
    providers: dict[str, LLMProvider] = {}
    for name, provider in config.providers.items():
        if provider.kind == "fake":
            providers[name] = FakeLLM(
                name=name, policy=ResearchMemoPolicy(), delay_s=settings.fake_llm_delay_s
            )
        elif provider.kind == "openai_compatible":
            if not provider.base_url or not provider.api_key_env:
                raise ValueError(f"provider {name!r} needs base_url and api_key_env")
            key = os.environ.get(provider.api_key_env)
            if not key:
                raise ValueError(f"{provider.api_key_env} is not set (provider {name!r})")
            providers[name] = OpenAICompatibleProvider(name, provider.base_url, key)
        else:
            raise ValueError(f"unknown provider kind {provider.kind!r}")
    return providers


async def verify_models(config: GatewayConfig, providers: dict[str, LLMProvider]) -> None:
    """Refuse to start if any configured model is not currently offered by its provider."""
    missing: list[str] = []
    for tier_name, tier in config.tiers.items():
        for route in tier.routes():
            provider = providers[route.provider]
            if isinstance(provider, OpenAICompatibleProvider):
                available = await provider.list_models()
                if route.model not in available:
                    missing.append(f"{tier_name}: {route.provider}/{route.model}")
    if missing:
        raise ModelUnavailable("configured models not available: " + ", ".join(missing))


def build_services(settings: Settings, pool: Pool | None) -> ToolServices:
    search: SearchProvider
    rag: RagClient
    if settings.fake_llm:
        search = StaticSearch(default=list(SAMPLE_SEARCH_HITS))
    elif settings.tavily_api_key:
        search = TavilySearch(settings.tavily_api_key)
    else:
        # Real runs never get canned results: the tool reports that search is unavailable.
        search = UnavailableSearch()
    if settings.fake_llm or not settings.docqa_base_url:
        rag = StaticRag(hits=list(SAMPLE_RAG_HITS))
    else:
        rag = DocQaClient(settings.docqa_base_url)
    if pool is None:
        from orchestrator.tools.services import InMemoryArtifactStore

        artifacts: object = InMemoryArtifactStore()
    else:
        artifacts = PostgresArtifactStore(pool)
    return ToolServices(search=search, rag=rag, artifacts=artifacts, sandbox=StubSandbox())  # type: ignore[arg-type]


@dataclass
class Runtime:
    settings: Settings
    pool: Pool
    deps: EngineDeps
    runs: RunStore
    events: PostgresEventLog
    providers: dict[str, LLMProvider]


def build_publisher(settings: Settings, pool: Pool) -> Publisher:
    sink: Publisher
    if settings.publish_sink_url:
        sink = HttpPublishSink(settings.publish_sink_url, settings.publish_sink_token or "")
    else:
        sink = PostgresPublishSink(pool)
    return LedgerPublisher(pool, sink)


def build_runtime(settings: Settings, pool: Pool) -> Runtime:
    config = gateway_config(settings)
    providers = build_providers(config, settings)
    clock = SystemClock()
    runs = RunStore(pool)
    events = PostgresEventLog(pool)
    deps = EngineDeps(
        gateway=LLMGateway(config, providers, clock),
        tools=default_tools(),
        services=build_services(settings, pool),
        events=MetricsEventLog(events),
        publisher=build_publisher(settings, pool),
        clock=clock,
        settings=EngineSettings(checkpoint_event_every_n=settings.checkpoint_event_every_n),
        state_store=runs,
    )
    return Runtime(settings, pool, deps, runs, events, providers)
