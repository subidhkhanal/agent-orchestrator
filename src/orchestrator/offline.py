"""Offline wiring: fake LLM, canned search/RAG corpora, in-memory stores.

Used by tests and `scripts/demo_offline.py`. No network, no API keys, no cost.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.agents import NODE_LIBRARY
from orchestrator.clock import Clock, FakeClock
from orchestrator.effects.publisher import InMemoryPublishSink
from orchestrator.engine.context import EngineDeps, EngineSettings
from orchestrator.engine.local import LocalRunner
from orchestrator.events.log import InMemoryEventLog
from orchestrator.gateway.config import GatewayConfig
from orchestrator.gateway.gateway import LLMGateway
from orchestrator.gateway.providers.demo_policy import ResearchMemoPolicy
from orchestrator.gateway.providers.fake import FakeLLM
from orchestrator.graphs.definitions import default_registry
from orchestrator.tools import default_registry as default_tools
from orchestrator.tools.services import (
    InMemoryArtifactStore,
    RagHit,
    SearchHit,
    StaticRag,
    StaticSearch,
    StubSandbox,
    ToolServices,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
# Resolved lazily (see runtime.project_path) so it also works from an installed package.
DEFAULT_MODELS_CONFIG = Path("config") / "models.fake.toml"

SAMPLE_SEARCH_HITS = [
    SearchHit(
        title="Grid storage outlook 2026",
        url="https://example.org/grid-storage-2026",
        snippet="Utility-scale battery installations roughly doubled year over year.",
    ),
    SearchHit(
        title="Sodium-ion cost survey",
        url="https://example.org/sodium-ion-costs",
        snippet="Sodium-ion cell prices fell below lithium iron phosphate in two markets.",
    ),
    SearchHit(
        title="Interconnection queue report",
        url="https://example.org/interconnection-queues",
        snippet="Average wait for grid interconnection now exceeds four years.",
    ),
]

SAMPLE_RAG_HITS = [
    RagHit(
        title="Internal note: storage procurement",
        doc_ref="docqa://docs/storage-procurement.pdf#p3",
        snippet="Procurement favors four-hour systems for peak shifting contracts.",
    ),
]


def build_offline(
    *,
    llm: FakeLLM | None = None,
    clock: Clock | None = None,
    search: StaticSearch | None = None,
    rag: StaticRag | None = None,
    settings: EngineSettings | None = None,
    config: GatewayConfig | None = None,
) -> tuple[LocalRunner, EngineDeps]:
    clock = clock or FakeClock()
    llm = llm or FakeLLM(policy=ResearchMemoPolicy())
    if config is None:
        from orchestrator.runtime import project_path

        config = GatewayConfig.load(project_path(DEFAULT_MODELS_CONFIG))
    deps = EngineDeps(
        gateway=LLMGateway(config, {"fake": llm}, clock),
        tools=default_tools(),
        services=ToolServices(
            search=search or StaticSearch(default=list(SAMPLE_SEARCH_HITS)),
            rag=rag or StaticRag(hits=list(SAMPLE_RAG_HITS)),
            artifacts=InMemoryArtifactStore(),
            sandbox=StubSandbox(),
        ),
        events=InMemoryEventLog(clock),
        publisher=InMemoryPublishSink(clock),
        clock=clock,
        settings=settings or EngineSettings(),
    )
    return LocalRunner(default_registry(), deps, NODE_LIBRARY), deps
