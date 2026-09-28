"""Immutable, versioned graph definitions.

A GraphSpec is pure data: node names, static edges and the dynamic routes a node may choose.
Node behavior comes from the node library, looked up by name. A run pins (graph_id, version)
at creation, and the registry refuses to register a different topology under an existing
version. Changing a graph means publishing a new version, so in-flight runs keep the exact
topology they started with.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

END = "END"


@dataclass(frozen=True)
class GraphSpec:
    graph_id: str
    version: int
    description: str
    entry: str
    nodes: tuple[str, ...]
    # Static edges: after `src` finishes, always go to `dst` (dst may be END).
    edges: tuple[tuple[str, str], ...] = ()
    # Dynamic routes: the node returns one of these targets at runtime.
    routes: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        known = set(self.nodes) | {END}
        if self.entry not in self.nodes:
            raise ValueError(f"entry {self.entry!r} is not a node")
        static_src = [src for src, _ in self.edges]
        dynamic_src = [src for src, _ in self.routes]
        for src, dst in self.edges:
            if src not in self.nodes or dst not in known:
                raise ValueError(f"edge {src}->{dst} references an unknown node")
        for src, targets in self.routes:
            if src not in self.nodes or not set(targets) <= known or not targets:
                raise ValueError(f"routes from {src!r} reference unknown nodes")
        if len(set(static_src)) != len(static_src) or len(set(dynamic_src)) != len(dynamic_src):
            raise ValueError("a node may have at most one static edge and one route set")
        for node in self.nodes:
            if (node in static_src) == (node in dynamic_src):
                raise ValueError(f"node {node!r} needs exactly one of: a static edge, routes")

    def targets(self, node: str) -> tuple[str, ...]:
        for src, targets in self.routes:
            if src == node:
                return targets
        return ()

    def static_next(self, node: str) -> str | None:
        return next((dst for src, dst in self.edges if src == node), None)

    def topology(self) -> dict[str, object]:
        return {
            "entry": self.entry,
            "nodes": sorted(self.nodes),
            "edges": sorted([list(e) for e in self.edges]),
            "routes": {src: sorted(targets) for src, targets in sorted(self.routes)},
        }

    def topology_hash(self) -> str:
        canonical = json.dumps(self.topology(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def mermaid(self) -> str:
        lines = ["flowchart TD", f"    START([START]) --> {self.entry}"]
        for src, dst in self.edges:
            lines.append(f"    {src} --> {_mermaid_id(dst)}")
        for src, targets in self.routes:
            lines.extend(f"    {src} -.-> {_mermaid_id(dst)}" for dst in targets)
        return "\n".join(lines)


def _mermaid_id(node: str) -> str:
    return "END_([END])" if node == END else node


class GraphRegistry:
    def __init__(self) -> None:
        self._specs: dict[tuple[str, int], GraphSpec] = {}

    def register(self, spec: GraphSpec) -> None:
        key = (spec.graph_id, spec.version)
        existing = self._specs.get(key)
        if existing is not None and existing.topology_hash() != spec.topology_hash():
            raise ValueError(
                f"{spec.graph_id} v{spec.version} is already registered with a different "
                "topology; publish a new version instead"
            )
        self._specs[key] = spec

    def get(self, graph_id: str, version: int | None = None) -> GraphSpec:
        if version is None:
            versions = [v for (gid, v) in self._specs if gid == graph_id]
            if not versions:
                raise KeyError(graph_id)
            version = max(versions)
        return self._specs[(graph_id, version)]

    def all(self) -> list[GraphSpec]:
        return [self._specs[k] for k in sorted(self._specs)]
