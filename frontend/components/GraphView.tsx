"use client";

import { useEffect, useId, useRef } from "react";

// Renders the pinned graph version's Mermaid diagram and highlights the active node.
export default function GraphView({ mermaid, active }: { mermaid: string; active?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId().replace(/:/g, "");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const m = (await import("mermaid")).default;
      const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
      m.initialize({ startOnLoad: false, theme: dark ? "dark" : "neutral", securityLevel: "strict" });
      const highlight = active
        ? `\n    classDef active fill:#f59e0b,stroke:#b45309,color:#111,stroke-width:2px;\n    class ${active} active;`
        : "";
      const { svg } = await m.render(`g${id}`, mermaid + highlight);
      if (!cancelled && ref.current) ref.current.innerHTML = svg;
    })().catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [mermaid, active, id]);

  return <div ref={ref} className="graph" aria-label="graph diagram" />;
}
