"""Gateway configuration: model tiers, per-node caps, and prices.

Model names live in a TOML file (config/models.toml), never in code, so a retired model is a
config change rather than a code change.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Cfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Route(_Cfg):
    provider: str
    model: str
    # Extra provider request fields for this model, e.g. {reasoning_effort = "low"}.
    params: dict[str, Any] = Field(default_factory=dict)


class Tier(_Cfg):
    primary: Route
    fallback: Route | None = None
    max_output_tokens: int = Field(gt=0)
    timeout_s: float = Field(gt=0)
    # Provider limit on one request (input + reserved output). Free tiers enforce their
    # tokens-per-minute quota per request, and count max_tokens against it.
    max_request_tokens: int | None = Field(default=None, gt=0)

    def routes(self) -> list[Route]:
        return [self.primary] if self.fallback is None else [self.primary, self.fallback]


class NodeConfig(_Cfg):
    tier: str
    # Per-node-execution caps. The effective grant is min(node cap, run remaining).
    max_tokens_per_node: int = Field(gt=0)
    max_usd_per_node: float = Field(gt=0)


class Price(_Cfg):
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6


class RetryConfig(_Cfg):
    max_retries: int = Field(default=4, ge=0)
    # Resamples when the provider rejects the model's output (bad JSON / tool call).
    max_invalid_output_retries: int = Field(default=2, ge=0)
    base_delay_s: float = Field(default=1.0, gt=0)
    max_delay_s: float = Field(default=20.0, gt=0)


class ProviderConfig(_Cfg):
    kind: str
    api_key_env: str | None = None
    base_url: str | None = None


class GatewayConfig(_Cfg):
    providers: dict[str, ProviderConfig]
    tiers: dict[str, Tier]
    nodes: dict[str, NodeConfig]
    prices: dict[str, Price]
    retry: RetryConfig = RetryConfig()

    @model_validator(mode="after")
    def _references_resolve(self) -> GatewayConfig:
        for name, node in self.nodes.items():
            if node.tier not in self.tiers:
                raise ValueError(f"node {name!r} uses unknown tier {node.tier!r}")
        for name, tier in self.tiers.items():
            for route in tier.routes():
                if route.provider not in self.providers:
                    raise ValueError(f"tier {name!r} uses unknown provider {route.provider!r}")
                if route.model not in self.prices:
                    raise ValueError(f"model {route.model!r} has no price entry")
        return self

    def node(self, node: str) -> NodeConfig:
        try:
            return self.nodes[node]
        except KeyError:
            raise KeyError(f"no gateway config for node {node!r}") from None

    def tier_for(self, node: str) -> Tier:
        return self.tiers[self.node(node).tier]

    @classmethod
    def load(cls, path: str | Path) -> GatewayConfig:
        with open(path, "rb") as f:
            return cls.model_validate(tomllib.load(f))
