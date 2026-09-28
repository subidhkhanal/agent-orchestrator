// Thin client for the orchestrator API, including an SSE reader with Last-Event-ID resume.

// Empty in a production build without a backend: the page then shows an offline notice.
export const API_URL = (
  process.env.NEXT_PUBLIC_API_URL ?? (process.env.NODE_ENV === "development" ? "http://localhost:8000" : "")
).replace(/\/$/, "");
export const BACKEND_CONFIGURED = API_URL !== "";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || "";

export type RunEvent = {
  event_id: number;
  type: string;
  payload: Record<string, any>;
  created_at: string;
};

export type GraphInfo = {
  graph_id: string;
  version: number;
  description: string;
  nodes: string[];
  mermaid: string;
  topology_hash: string;
};

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json", ...extra };
  if (API_KEY) h.Authorization = `Bearer ${API_KEY}`;
  return h;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { ...init, headers: headers(init.headers as any) });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = body?.error ?? {};
    throw new ApiError(res.status, err.code ?? "error", err.message ?? res.statusText);
  }
  return body as T;
}

export const api = {
  graphs: () => call<{ graphs: GraphInfo[] }>("/api/v1/graphs"),
  createRun: (body: unknown, idempotencyKey: string) =>
    call<{ run_id: string; status: string }>("/api/v1/graph-runs", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  run: (id: string) => call<any>(`/api/v1/graph-runs/${id}`),
  artifact: (id: string, artifactId = "memo") =>
    call<{ content: string; version: number; sources: any[] }>(
      `/api/v1/graph-runs/${id}/artifacts/${artifactId}`,
    ),
  approve: (id: string, gate: string, comment?: string) =>
    call<any>(`/api/v1/graph-runs/${id}/hitl/${gate}/approve`, {
      method: "POST",
      body: JSON.stringify({ comment: comment || null }),
    }),
  reject: (id: string, gate: string, reason: string) =>
    call<any>(`/api/v1/graph-runs/${id}/hitl/${gate}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  cancel: (id: string) => call<any>(`/api/v1/graph-runs/${id}/cancel`, { method: "POST" }),
  published: () => call<{ published: any[] }>("/api/v1/published"),
};

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

/**
 * Follows a run's event stream. Uses fetch (not EventSource) so it can send the API key
 * header. On any disconnect it reconnects with Last-Event-ID, so the server replays exactly
 * the missed events from the audit log. Returns a function that stops the stream.
 */
export function followRun(
  runId: string,
  onEvent: (e: RunEvent) => void,
  onStatus: (s: "live" | "reconnecting" | "closed") => void,
): () => void {
  let lastId = 0;
  let stopped = false;
  let controller: AbortController | null = null;

  const loop = async () => {
    while (!stopped) {
      controller = new AbortController();
      try {
        const res = await fetch(`${API_URL}/api/v1/graph-runs/${runId}/stream`, {
          headers: headers(lastId ? { "Last-Event-ID": String(lastId) } : {}),
          signal: controller.signal,
        });
        if (!res.ok || !res.body) throw new Error(`stream HTTP ${res.status}`);
        onStatus("live");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            const data = block
              .split("\n")
              .filter((l) => l.startsWith("data: "))
              .map((l) => l.slice(6))
              .join("");
            if (!data) continue;
            const event: RunEvent = JSON.parse(data);
            lastId = event.event_id;
            onEvent(event);
            if (TERMINAL.has(event.type)) {
              stopped = true;
              onStatus("closed");
              return;
            }
          }
        }
      } catch {
        if (stopped) return;
      }
      if (!stopped) {
        onStatus("reconnecting");
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
  };
  loop();
  return () => {
    stopped = true;
    controller?.abort();
  };
}
