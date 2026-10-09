"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import GraphView from "@/components/GraphView";
import Memo from "@/components/Memo";
import { api, ApiError, BACKEND_CONFIGURED, followRun, GraphInfo, RunEvent } from "@/lib/api";

const EXAMPLES = [
  {
    label: "Parental leave memo (internal docs)",
    graph: "research-memo",
    task: "Write a memo summarizing Northwind Labs' paid parental leave for primary and secondary caregivers.",
  },
  {
    label: "Travel cheat sheet (internal docs)",
    graph: "research-memo",
    task: "Write a travel-policy cheat sheet for Northwind Labs staff: international meal per diem, hotel cap outside major cities, and mileage reimbursement rate.",
  },
  {
    label: "Impossible task (should refuse to invent)",
    graph: "research-memo",
    task: "Draft a profile memo of Northwind Labs' CEO based on the company's internal documents.",
  },
];

const NODE_EVENTS = new Set(["node_completed", "route_decided", "guard_override", "hitl_required", "hitl_decided", "waiting", "tool_denied", "run_claimed", "completed", "failed", "cancelled"]);

export default function Home() {
  const [graphs, setGraphs] = useState<GraphInfo[]>([]);
  const [graphId, setGraphId] = useState("research-memo");
  const [task, setTask] = useState(EXAMPLES[0].task);
  const [maxUsd, setMaxUsd] = useState(1.5);
  const [maxTokens, setMaxTokens] = useState(600000);
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [streamStatus, setStreamStatus] = useState("idle");
  const [run, setRun] = useState<any>(null);
  const [memo, setMemo] = useState<{ content: string; version: number; sources: any[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [offline, setOffline] = useState(false);
  const stopRef = useRef<(() => void) | undefined>(undefined);
  const timelineRef = useRef<HTMLOListElement>(null);

  useEffect(() => {
    // Keep the newest event in view as the run progresses.
    const el = timelineRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  useEffect(() => {
    const goOffline = () => {
      setOffline(true);
      setGraphs([OFFLINE_GRAPH]);
    };
    if (!BACKEND_CONFIGURED) {
      goOffline();
      return;
    }
    api
      .graphs()
      .then((g) => setGraphs(g.graphs.filter((x) => x.graph_id !== "single-agent")))
      .catch(goOffline);
    const fromUrl = new URLSearchParams(window.location.search).get("run");
    if (fromUrl) attach(fromUrl);
    return () => stopRef.current?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = useCallback(async (id: string) => {
    try {
      const r = await api.run(id);
      setRun(r);
      if (r.state?.artifacts?.length) setMemo(await api.artifact(id));
    } catch {
      /* transient */
    }
  }, []);

  const attach = useCallback(
    (id: string) => {
      stopRef.current?.();
      setRunId(id);
      setEvents([]);
      setMemo(null);
      window.history.replaceState(null, "", `?run=${id}`);
      refresh(id);
      stopRef.current = followRun(
        id,
        (e) => {
          setEvents((prev) => (prev.some((p) => p.event_id === e.event_id) ? prev : [...prev, e]));
          if (["node_completed", "hitl_required", "hitl_decided", "completed", "failed", "cancelled"].includes(e.type)) refresh(id);
        },
        setStreamStatus,
      );
    },
    [refresh],
  );

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = {
        graph_id: graphId,
        input: { task },
        budget: { max_usd: maxUsd, max_tokens: maxTokens, deadline_s: 900 },
      };
      const res = await api.createRun(body, crypto.randomUUID());
      attach(res.run_id);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status} ${e.code}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  };

  const decide = async (approve: boolean) => {
    if (!runId || !pendingGate) return;
    setBusy(true);
    setError(null);
    try {
      if (approve) await api.approve(runId, pendingGate, comment);
      else await api.reject(runId, pendingGate, comment || "Please revise.");
      setComment("");
      refresh(runId);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status} ${e.code}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  };

  const graph = graphs.find((g) => g.graph_id === (run?.graph_id ?? graphId));
  const status: string = run?.status ?? "";
  const pendingGate: string | undefined = run?.approvals?.find((a: any) => a.status === "PENDING")?.gate_id;
  const preview = [...events].reverse().find((e) => e.type === "hitl_required" && e.payload.gate_id === pendingGate)?.payload.preview;

  const activeNode = useMemo(() => {
    if (status === "WAITING_HITL") return "await_approval";
    const lastStart = [...events].reverse().find((e) => e.type === "node_started");
    if (!lastStart) return undefined;
    const done = events.some((e) => e.type === "node_completed" && e.event_id > lastStart.event_id && e.payload.node === lastStart.payload.node);
    return done ? undefined : lastStart.payload.node;
  }, [events, status]);

  const spent = events.filter((e) => e.type === "llm_call").reduce((s, e) => s + (e.payload.usd || 0), 0);
  const limit = run?.cost?.usd_limit ?? maxUsd;
  const patches = events.filter((e) => e.type === "state_patch");
  const timeline = events.filter((e) => NODE_EVENTS.has(e.type));

  return (
    <main>
      <header>
        <h1>agent-orchestrator</h1>
        <p className="muted">
          Supervisor-worker agents on LangGraph + Postgres. A supervisor routes between a researcher, a writer and a reviewer;
          code guards enforce the safety rules; a human approves before anything is published.
        </p>
      </header>

      <section className="card">
        <h2>Start a run</h2>
        <div className="examples">
          {EXAMPLES.map((ex) => (
            <button key={ex.label} className="chip" onClick={() => { setTask(ex.task); setGraphId(ex.graph); }}>
              {ex.label}
            </button>
          ))}
        </div>
        <label>
          Graph
          <select value={graphId} onChange={(e) => setGraphId(e.target.value)}>
            {graphs.map((g) => (
              <option key={`${g.graph_id}@${g.version}`} value={g.graph_id}>
                {g.graph_id} v{g.version}
              </option>
            ))}
          </select>
        </label>
        <label>
          Task
          <textarea rows={3} maxLength={500} value={task} onChange={(e) => setTask(e.target.value)} />
        </label>
        <div className="row">
          <label>
            Max USD
            <input type="number" step="0.05" min="0.05" value={maxUsd} onChange={(e) => setMaxUsd(Number(e.target.value))} />
          </label>
          <label>
            Max tokens
            <input type="number" step="10000" min="5000" value={maxTokens} onChange={(e) => setMaxTokens(Number(e.target.value))} />
          </label>
          <button className="primary" disabled={offline || busy || task.trim().length < 3} onClick={start}>
            Run
          </button>
        </div>
        {error && <p className="error">{error}</p>}
      </section>

      {offline && (
        <section className="card offline">
          <h2>The live backend is not deployed yet</h2>
          <p>
            This page is the real UI, but the API and worker (FastAPI + LangGraph + Postgres) are not hosted yet, so
            runs cannot start here. Everything runs locally; the repository README explains how.
          </p>
          <p className="muted">
            When connected, a run shows here as a live timeline (node, routing reason, tokens, cost, latency), this
            graph with the active node highlighted, a state inspector listing every validated patch, an approval panel,
            and the final memo with numbered citations.
          </p>
          <h2>research-memo v1</h2>
          <GraphView mermaid={RESEARCH_MEMO_MERMAID} />
          <p className="muted">
            Dotted edges are chosen at runtime by the supervisor, then checked by code guards: publish needs a human
            approval of the current version, the writer needs a source first, and the budget winds the run down at 10%.
          </p>
        </section>
      )}

      {runId && (
        <>
          <section className="card status-bar">
            <span>
              Run <code>{runId}</code>
            </span>
            <span className={`badge ${status}`}>{status || "…"}</span>
            <span className="muted">stream: {streamStatus}</span>
            <span>
              state_version <strong>{run?.state_version ?? 0}</strong>
            </span>
            <div className="meter" title={`$${spent.toFixed(4)} of $${limit}`}>
              <div style={{ width: `${Math.min(100, (100 * spent) / limit)}%` }} />
              <span>
                ${spent.toFixed(4)} / ${limit}
              </span>
            </div>
            {!["COMPLETED", "FAILED", "CANCELLED"].includes(status) && (
              <button onClick={() => api.cancel(runId).then(() => refresh(runId))}>Cancel</button>
            )}
          </section>

          <div className="grid">
            <section className="card">
              <h2>Graph</h2>
              {graph && <GraphView mermaid={graph.mermaid} active={activeNode} />}
              <p className="muted">
                {graph?.graph_id} v{graph?.version} · topology <code>{graph?.topology_hash.slice(0, 10)}</code>
              </p>
            </section>

            <section className="card">
              <h2>Timeline</h2>
              <ol className="timeline" ref={timelineRef}>
                {timeline.map((e) => (
                  <li key={e.event_id} className={e.type}>
                    <span className="eid">#{e.event_id}</span> <Line e={e} />
                  </li>
                ))}
              </ol>
            </section>
          </div>

          {pendingGate && (
            <section className="card approval">
              <h2>Approval needed · {pendingGate}</h2>
              <pre className="preview">{preview ?? "(loading preview)"}</pre>
              <textarea rows={2} placeholder="Comment (a rejection reason is sent to the writer)" value={comment} onChange={(e) => setComment(e.target.value)} />
              <div className="row">
                <button className="primary" disabled={busy} onClick={() => decide(true)}>
                  Approve &amp; publish
                </button>
                <button disabled={busy} onClick={() => decide(false)}>
                  Reject
                </button>
              </div>
            </section>
          )}

          <div className="grid">
            <section className="card">
              <h2>Memo {memo && <span className="muted">v{memo.version}</span>}</h2>
              {memo ? <Memo content={memo.content} sources={memo.sources} /> : <p className="muted">No draft yet.</p>}
              {run?.state?.research_summary && !memo && <p>{run.state.research_summary}</p>}
              {run?.state?.review_notes?.filter((n: any) => n.kind === "final_summary").map((n: any) => (
                <p key={n.id} className="note">Final summary: {n.text}</p>
              ))}
            </section>

            <section className="card">
              <h2>State inspector</h2>
              <p className="muted">
                Every change is a validated patch against a version. {patches.length} patches applied.
              </p>
              <ol className="patches">
                {patches.slice(-40).map((e) => (
                  <li key={e.event_id}>
                    v{e.payload.base_version}→v{e.payload.state_version} <strong>{e.payload.author}</strong>{" "}
                    <span className="muted">
                      {e.payload.ops.map((o: any) => `${o.op} ${o.path}`).join(", ")}
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          </div>
        </>
      )}
      <footer className="muted">
        Demo mode: small per-run and daily spend caps; publishing only writes to this app&apos;s own feed.
      </footer>
    </main>
  );
}

function Line({ e }: { e: RunEvent }) {
  const p = e.payload;
  switch (e.type) {
    case "node_completed":
      return (
        <>
          <strong>{p.node}</strong> → {p.next ?? "END"}{" "}
          <span className="muted">
            {p.tokens} tok · ${Number(p.usd).toFixed(4)} · {(p.latency_ms / 1000).toFixed(1)}s
          </span>
          {p.reason && <div className="reason">{p.reason}</div>}
        </>
      );
    case "route_decided":
      return (
        <span className="muted">
          supervisor chose {p.proposed ?? "(forced)"} → {p.next_node}
        </span>
      );
    case "guard_override":
      return (
        <span className="warn">
          guard <strong>{p.guard}</strong>: {p.proposed ?? "—"} → {p.forced} ({p.detail})
        </span>
      );
    case "hitl_required":
      return <span className="warn">waiting for approval of {p.gate_id}</span>;
    case "hitl_decided":
      return <span>human {p.decision} {p.gate_id}{p.comment ? `: “${p.comment}”` : ""}</span>;
    case "waiting":
      return <span className="muted">rate limited on {p.model}; waiting {p.delay_s}s</span>;
    case "tool_denied":
      return <span className="warn">denied tool {p.tool} for {p.role}</span>;
    case "run_claimed":
      return <span className="muted">worker {p.worker_id} claimed (attempt {p.attempt}, {p.mode})</span>;
    default:
      return (
        <strong>
          {e.type}
          {p.reason ? `: ${p.reason}` : ""}
          {p.error ? `: ${p.error}` : ""}
        </strong>
      );
  }
}

// The registered topology of research-memo v1 (same text GET /api/v1/graphs returns).
const RESEARCH_MEMO_MERMAID = `flowchart TD
    START([START]) --> supervisor
    researcher --> supervisor
    coder --> supervisor
    reviewer --> supervisor
    human_gate --> await_approval
    publish --> END_([END])
    supervisor -.-> researcher
    supervisor -.-> coder
    supervisor -.-> reviewer
    supervisor -.-> human_gate
    supervisor -.-> publish
    supervisor -.-> END_([END])
    await_approval -.-> publish
    await_approval -.-> coder`;

const OFFLINE_GRAPH: GraphInfo = {
  graph_id: "research-memo",
  version: 1,
  description: "Supervisor, researcher, writer, reviewer, human approval, publish.",
  nodes: [],
  mermaid: RESEARCH_MEMO_MERMAID,
  topology_hash: "",
};
