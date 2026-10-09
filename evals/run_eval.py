"""Evaluate research-memo (Graph A) against the single-agent baseline.

    python evals/run_eval.py                          # all runnable tasks, both systems
    python evals/run_eval.py --ids rag-parental --systems research-memo
    python evals/run_eval.py --fake                   # offline smoke test of the harness

Both systems get the same tools, the same model tier for the work, and the same budget. The
human gate in Graph A is approved automatically (a human approval costs no tokens).

Per run it records cost, wall-clock, per-node latency, routing validity, guard overrides and
budget adherence from the event log, and scores the final memo three ways:
- citation validity (deterministic): share of claim lines citing a source that exists in state;
- reference facts (deterministic, where the task has them): every expected fact appears;
- an LLM judge with a rubric (task success, unsupported claims, correct abstention).
Results: evals/results/summary.{json,md}, per-run JSON in evals/results/runs/, and
evals/results/spot_check.md for manual review.

Web tasks need TAVILY_API_KEY; without it they are skipped (not faked).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

# Before any project import: LIBPQ_DIR (Windows) must be on PATH before psycopg loads.
load_dotenv(ROOT / ".env")

from orchestrator.agents import NODE_LIBRARY  # noqa: E402
from orchestrator.citations import check_memo  # noqa: E402
from orchestrator.clock import SystemClock  # noqa: E402
from orchestrator.effects.publisher import InMemoryPublishSink  # noqa: E402
from orchestrator.engine.context import EngineDeps  # noqa: E402
from orchestrator.engine.local import LocalRunner  # noqa: E402
from orchestrator.events.log import EventType, InMemoryEventLog  # noqa: E402
from orchestrator.gateway.budget import BudgetLedger  # noqa: E402
from orchestrator.gateway.gateway import LLMGateway  # noqa: E402
from orchestrator.gateway.types import LLMRequest, Message  # noqa: E402
from orchestrator.graphs.definitions import default_registry  # noqa: E402
from orchestrator.runtime import (  # noqa: E402
    build_providers,
    build_services,
    gateway_config,
    load_settings,
    verify_models,
)
from orchestrator.state.models import Budget, RunState  # noqa: E402
from orchestrator.tools import default_registry as default_tools  # noqa: E402

RESULTS = ROOT / "evals" / "results"
SYSTEMS = ("research-memo", "single-agent")
BUDGET_TOKENS = 600_000  # includes cache reads (tool loops resend the history)
BUDGET_USD = 1.50  # same cap for both systems
DEADLINE_S = 1800

JUDGE_SYSTEM = """\
You grade research memos written by an AI system. Be strict and literal.

You get: the task, the task category, reference facts (if any), whether the task is
unanswerable from the available documents, the sources the system retrieved (id, title,
snippet), and the final memo (or nothing if no memo was produced).

Rules:
- A claim is "supported" only if a cited source's snippet actually states it.
- For unanswerable tasks, success means the memo clearly says the information is not
  available (or refuses the false premise) and does not invent facts or sources.
- For tasks with reference facts, success requires those facts to be stated correctly.
- For other tasks, success means the memo addresses the task with claims supported by the
  retrieved sources.

Reply with JSON only:
{"task_success": true|false, "score": 1-5, "unsupported_claims": <int>,
 "correct_abstention": true|false|null, "rationale": "<two sentences at most>"}
"""


@dataclass
class RunRecord:
    task_id: str
    category: str
    system: str
    status: str
    termination: str | None
    wall_s: float
    usd: float
    tokens: int
    budget_exceeded: bool
    llm_calls: int
    rate_limit_waits: int
    node_latencies: list[tuple[str, int]] = field(default_factory=list)
    routes_valid: int = 0
    routes_invalid: int = 0
    guard_overrides: dict[str, int] = field(default_factory=dict)
    sources: int = 0
    claims: int = 0
    supported_claims: int = 0
    citation_validity: float | None = None
    facts_found: bool | None = None
    judge: dict[str, Any] | None = None
    memo: str | None = None
    error: str | None = None
    models_used: dict[str, int] = field(default_factory=dict)
    sources_brief: list[dict[str, str]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        judged = bool(self.judge and self.judge.get("task_success"))
        return judged and self.facts_found is not False


def _count(items: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def quota_exhausted(error: str | None) -> bool:
    """A provider's *daily* quota ran out: not a property of the system under test."""
    if not error:
        return False
    markers = ("tokens per day", "(TPD)", "requests per day", "(RPD)")
    return any(m in error for m in markers)


def quota_reset_seconds(error: str) -> float:
    match = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", error)
    if not match or not any(match.groups()):
        return 1800.0
    h, m, sec = (float(g) if g else 0.0 for g in match.groups())
    return h * 3600 + m * 60 + sec + 60  # plus a minute of slack


def interleave(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin across categories, so a partially finished eval stays balanced."""
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        by_cat.setdefault(t["category"], []).append(t)
    out: list[dict[str, Any]] = []
    while any(by_cat.values()):
        for cat in sorted(by_cat):
            if by_cat[cat]:
                out.append(by_cat[cat].pop(0))
    return out


async def wait_for_quota(args: argparse.Namespace, error: str) -> None:
    if not args.wait_for_quota:
        sys.exit(f"daily provider quota exhausted; re-run later with --resume\n{error[:300]}")
    # The provider's window is rolling: retrying as soon as a few tokens free up starts runs
    # that die halfway and are thrown away. Wait long enough for a whole run to fit.
    delay = max(quota_reset_seconds(error), args.quota_min_wait)
    until = time.strftime("%H:%M", time.localtime(time.time() + delay))
    print(f"daily quota exhausted; waiting {delay / 60:.0f} min (until ~{until})", flush=True)
    await asyncio.sleep(delay)


def load_tasks(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def facts_present(memo: str, groups: list[list[str]]) -> bool:
    text = memo.lower()
    return all(any(alt.lower() in text for alt in group) for group in groups)


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


class Harness:
    def __init__(self, fake: bool) -> None:
        settings = load_settings()
        if fake:
            settings.fake_llm = True
        self.settings = settings
        config = gateway_config(settings)
        if not fake:
            # Primary models only: a quota fallback to a different model mid-run would make
            # runs incomparable. A daily-quota error pauses the eval instead (see main()).
            tiers = {n: t.model_copy(update={"fallback": None}) for n, t in config.tiers.items()}
            config = config.model_copy(update={"tiers": tiers})
        self.providers = build_providers(config, settings)
        clock = SystemClock()
        self.events = InMemoryEventLog(clock)
        self.gateway = LLMGateway(config, self.providers, clock)
        self.deps = EngineDeps(
            gateway=self.gateway,
            tools=default_tools(),
            services=build_services(settings, None),
            events=self.events,
            publisher=InMemoryPublishSink(clock),
            clock=clock,
        )
        self.runner = LocalRunner(default_registry(), self.deps, NODE_LIBRARY)
        self.config = config

    async def verify(self) -> None:
        await verify_models(self.config, self.providers)

    async def run(self, task: dict[str, Any], system: str, run_id: str) -> RunRecord:
        from datetime import UTC, datetime, timedelta

        budget = Budget(
            token_limit=BUDGET_TOKENS,
            usd_limit=BUDGET_USD,
            tokens_remaining=BUDGET_TOKENS,
            usd_remaining=BUDGET_USD,
            deadline_at=datetime.now(UTC) + timedelta(seconds=DEADLINE_S),
        )
        started = time.monotonic()
        error = None
        try:
            outcome = await self.runner.start(
                run_id=run_id,
                tenant_id="eval",
                graph_id=system,
                task=task["task"],
                budget=budget,
            )
            for _ in range(3):  # approve the gate(s) automatically
                if outcome.status != "waiting_hitl" or outcome.interrupt is None:
                    break
                outcome = await self.runner.resume(
                    run_id,
                    {
                        "gate_id": outcome.interrupt["gate_id"],
                        "decision": "approved",
                        "reviewer": "eval-harness",
                    },
                )
            state: RunState | None = outcome.state
            status = outcome.status
            error = outcome.error
        except Exception as exc:  # a harness-level failure is recorded, not fatal
            state, status, error = None, "failed", f"{type(exc).__name__}: {exc}"
        wall = time.monotonic() - started

        events = self.events.all(run_id)
        by_type: dict[EventType, list[Any]] = {}
        for e in events:
            by_type.setdefault(e.event_type, []).append(e.payload)
        llm = by_type.get(EventType.LLM_CALL, [])
        usd = sum(p.get("usd", 0.0) for p in llm)
        tokens = sum(p.get("input_tokens", 0) + p.get("output_tokens", 0) for p in llm)
        overrides: dict[str, int] = {}
        for p in by_type.get(EventType.GUARD_OVERRIDE, []):
            overrides[p["guard"]] = overrides.get(p["guard"], 0) + 1

        memo = None
        if state is not None and (artifact := state.current_artifact()) is not None:
            memo = await self.deps.services.artifacts.get(artifact.content_ref, tenant_id="eval")
        elif state is not None:
            # No memo: grade the run's final written output instead (research summary and any
            # final-summary notes), the same way for both systems.
            parts = [state.research_summary or ""]
            parts += [n.text for n in state.review_notes if n.kind == "final_summary"]
            memo = "\n\n".join(p for p in parts if p) or None
        record = RunRecord(
            task_id=task["id"],
            category=task["category"],
            system=system,
            status=status,
            termination=state.termination.reason if state else None,
            wall_s=round(wall, 2),
            usd=round(usd, 6),
            tokens=tokens,
            budget_exceeded=usd > BUDGET_USD + 1e-9,
            llm_calls=sum(1 for p in llm if p.get("status") == "ok"),
            rate_limit_waits=len(by_type.get(EventType.WAITING, [])),
            node_latencies=[
                (p["node"], p["latency_ms"]) for p in by_type.get(EventType.NODE_COMPLETED, [])
            ],
            routes_valid=sum(
                1 for p in by_type.get(EventType.ROUTE_DECIDED, []) if p.get("proposed") is not None
            ),
            routes_invalid=len(by_type.get(EventType.ROUTE_INVALID, [])),
            guard_overrides=overrides,
            sources=len(state.sources) if state else 0,
            memo=memo,
            error=error,
            models_used=_count(p["model"] for p in llm if p.get("status") == "ok"),
            sources_brief=[
                {"id": s.id, "title": s.title, "snippet": s.snippet[:300]}
                for s in (state.sources if state else ())
            ],
        )
        if memo is not None and state is not None:
            report = check_memo(memo, state.source_ids())
            record.claims, record.supported_claims = report.claims, report.supported_claims
            record.citation_validity = round(report.validity, 4)
        if task.get("expected"):
            record.facts_found = facts_present(memo or "", task["expected"])
        if not quota_exhausted(record.error):
            record.judge = await self.judge(task, record.sources_brief, memo)
        return record

    async def judge(
        self, task: dict[str, Any], sources: list[dict[str, str]], memo: str | None
    ) -> dict[str, Any] | None:
        prompt = json.dumps(
            {
                "task": task["task"],
                "category": task["category"],
                "reference_facts": task.get("expected"),
                "unanswerable": bool(task.get("unanswerable")),
                "sources": sources,
                "memo": memo,
            },
            indent=1,
        )
        ledger = BudgetLedger(tokens_available=60_000, usd_available=0.30, deadline_at=None)

        async def no_events(*_: Any) -> None:
            return None

        for _ in range(2):
            try:
                response = await self.gateway.complete(
                    LLMRequest(
                        node="judge",
                        messages=(
                            Message(role="system", content=JUDGE_SYSTEM),
                            Message(role="user", content=prompt),
                        ),
                        response_schema={"type": "object"},
                        metadata={"view": {}, "judge": True},
                    ),
                    ledger=ledger,
                    emit=no_events,
                )
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.content.strip())
                verdict: dict[str, Any] = json.loads(text)
                verdict["judge_usd"] = round(ledger.usd_used, 6)
                return verdict
            except Exception as exc:
                last = repr(exc)
        return {"error": last}


def summarize(records: list[RunRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for system in SYSTEMS:
        rs = [r for r in records if r.system == system]
        if not rs:
            continue
        judged = [r for r in rs if r.judge and "task_success" in r.judge]
        claims = sum(r.claims for r in rs)
        latencies: dict[str, list[float]] = {}
        for r in rs:
            for node, ms in r.node_latencies:
                latencies.setdefault(node, []).append(ms / 1000)
        valid = sum(r.routes_valid for r in rs)
        invalid = sum(r.routes_invalid for r in rs)
        per_category = {}
        for cat in sorted({r.category for r in rs}):
            cr = [r for r in rs if r.category == cat]
            per_category[cat] = {"runs": len(cr), "success": sum(r.success for r in cr)}
        overrides: dict[str, int] = {}
        for r in rs:
            for k, v in r.guard_overrides.items():
                overrides[k] = overrides.get(k, 0) + v
        summary[system] = {
            "runs": len(rs),
            "completed": sum(r.status == "completed" for r in rs),
            "task_success_rate": round(sum(r.success for r in rs) / len(rs), 3),
            "judge_mean_score": round(statistics.mean(r.judge["score"] for r in judged), 2)
            if judged
            else None,
            "citation_validity": round(sum(r.supported_claims for r in rs) / claims, 3)
            if claims
            else None,
            "facts_found_rate": _rate([r.facts_found for r in rs if r.facts_found is not None]),
            "avg_usd": round(statistics.mean(r.usd for r in rs), 5),
            "avg_tokens": round(statistics.mean(r.tokens for r in rs)),
            "avg_wall_s": round(statistics.mean(r.wall_s for r in rs), 1),
            "p95_node_latency_s": {n: round(p95(v), 2) for n, v in sorted(latencies.items())},
            "routing_validity": round(valid / (valid + invalid), 3) if valid + invalid else None,
            "guard_overrides": overrides,
            "runs_over_budget": sum(r.budget_exceeded for r in rs),
            "rate_limit_waits": sum(r.rate_limit_waits for r in rs),
            "per_category": per_category,
        }
    return summary


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def to_markdown(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    rows = [
        ("Task success (judge + reference facts)", "task_success_rate", "{:.0%}"),
        ("Judge mean score (1-5)", "judge_mean_score", "{}"),
        ("Citation validity (claims citing a real source)", "citation_validity", "{:.0%}"),
        ("Reference facts present", "facts_found_rate", "{:.0%}"),
        ("Avg cost per run (USD)", "avg_usd", "${:.4f}"),
        ("Avg tokens per run", "avg_tokens", "{:,}"),
        ("Avg wall-clock per run (s)", "avg_wall_s", "{}"),
        ("Supervisor routing validity", "routing_validity", "{:.0%}"),
        ("Runs over max_usd", "runs_over_budget", "{}"),
    ]
    systems = [s for s in SYSTEMS if s in summary]
    out = ["| Metric | " + " | ".join(systems) + " |", "|---|" + "---|" * len(systems)]
    for label, key, fmt in rows:
        cells = []
        for s in systems:
            v = summary[s].get(key)
            cells.append("n/a" if v is None else fmt.format(v))
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("Per category (successful / runs):")
    out.append("")
    cats = sorted({c for s in systems for c in summary[s]["per_category"]})
    out.append("| Category | " + " | ".join(systems) + " |")
    out.append("|---|" + "---|" * len(systems))
    for c in cats:
        cells = []
        for s in systems:
            pc = summary[s]["per_category"].get(c)
            cells.append(f"{pc['success']}/{pc['runs']}" if pc else "-")
        out.append(f"| {c} | " + " | ".join(cells) + " |")
    out.append("")
    for s in systems:
        lat = ", ".join(f"{n} {v}s" for n, v in summary[s]["p95_node_latency_s"].items())
        out.append(f"- p95 node latency, {s}: {lat}")
    out.append("")
    out.append(f"Run metadata: {json.dumps(meta)}")
    return "\n".join(out) + "\n"


def spot_check(records: list[RunRecord]) -> str:
    parts = ["# Spot-check list\n", "Read these by hand; the judge can be wrong.\n"]
    for r in sorted(records, key=lambda r: (r.task_id, r.system)):
        j = r.judge or {}
        parts.append(f"## {r.task_id} / {r.system}\n")
        parts.append(
            f"- status: {r.status} ({r.termination}); success: {r.success}; judge: "
            f"{j.get('task_success')} score {j.get('score')}; facts: {r.facts_found}; "
            f"citation validity: {r.citation_validity}; cost ${r.usd}\n"
            f"- judge rationale: {j.get('rationale', j.get('error'))}\n"
        )
        parts.append("```markdown\n" + (r.memo or "(no memo)")[:3000] + "\n```\n")
    return "\n".join(parts)


async def main(args: argparse.Namespace) -> None:
    if args.summarize_only:
        write_reports(RESULTS, _saved_records(RESULTS), {"partial": True})
        return
    harness = Harness(fake=args.fake)
    if not args.fake:
        await harness.verify()
    tasks = load_tasks(ROOT / "evals" / "tasks.jsonl")
    if args.ids:
        tasks = [t for t in tasks if t["id"] in args.ids.split(",")]
    skipped = []
    if not harness.settings.tavily_api_key and not args.fake:
        skipped = [t["id"] for t in tasks if t["category"] in ("web", "both")]
        tasks = [t for t in tasks if t["category"] not in ("web", "both")]
    tasks = interleave(tasks)
    if args.limit:
        tasks = tasks[: args.limit]
    systems = args.systems.split(",")
    out_dir = RESULTS / ("fake" if args.fake else "")
    (out_dir / "runs").mkdir(parents=True, exist_ok=True)

    records: list[RunRecord] = []
    for i, task in enumerate(tasks):
        for system in systems:
            path = out_dir / "runs" / f"{system}__{task['id']}.json"
            if args.resume and path.exists():
                records.append(RunRecord(**json.loads(path.read_text(encoding="utf-8"))))
                continue
            attempt = 0
            while True:
                attempt += 1
                record = await harness.run(
                    task, system, f"eval-{system}-{task['id']}-{i}-a{attempt}"
                )
                judge_error = (record.judge or {}).get("error")
                if quota_exhausted(judge_error):
                    await wait_for_quota(args, judge_error)
                    record.judge = await harness.judge(task, record.sources_brief, record.memo)
                if not quota_exhausted(record.error):
                    break
                # Not saved: the run is redone from scratch after the quota resets.
                await wait_for_quota(args, record.error or "")
            path.write_text(json.dumps(asdict(record), indent=1), encoding="utf-8")
            records.append(record)
            j = record.judge or {}
            print(
                f"[{len(records)}] {task['id']:<24} {system:<14} {record.status:<10} "
                f"success={record.success!s:<5} judge={j.get('score')} "
                f"cit={record.citation_validity} ${record.usd:.4f} {record.wall_s}s "
                f"waits={record.rate_limit_waits}",
                flush=True,
            )
            if args.pause and not args.fake:
                await asyncio.sleep(args.pause)

    meta = {
        "tasks": len(tasks),
        "skipped_no_web_search_key": skipped,
        "models_config": "fake" if args.fake else harness.settings.models_config,
    }
    write_reports(out_dir, records, meta)


def _saved_records(out_dir: Path) -> list[RunRecord]:
    return [
        RunRecord(**json.loads(f.read_text(encoding="utf-8")))
        for f in sorted((out_dir / "runs").glob("*.json"))
    ]


def write_reports(out_dir: Path, records: list[RunRecord], meta: dict[str, Any]) -> None:
    summary = summarize(records)
    meta = {
        **meta,
        "runs": len(records),
        "budget": {"tokens": BUDGET_TOKENS, "usd": BUDGET_USD},
        "date": time.strftime("%Y-%m-%d"),
    }
    (out_dir / "summary.json").write_text(
        json.dumps({"summary": summary, "meta": meta}, indent=1), encoding="utf-8"
    )
    (out_dir / "summary.md").write_text(to_markdown(summary, meta), encoding="utf-8")
    (out_dir / "spot_check.md").write_text(spot_check(records), encoding="utf-8")
    print(to_markdown(summary, meta))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--systems", default=",".join(SYSTEMS))
    parser.add_argument("--ids", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--pause",
        type=float,
        default=15.0,
        help="seconds between runs (free-tier token-per-minute limits)",
    )
    parser.add_argument("--fake", action="store_true", help="offline harness smoke test")
    parser.add_argument("--resume", action="store_true", help="reuse finished per-run results")
    parser.add_argument(
        "--summarize-only", action="store_true", help="rebuild reports from saved runs"
    )
    parser.add_argument(
        "--quota-min-wait",
        type=float,
        default=7200,
        help="minimum seconds to wait after a daily-quota error",
    )
    parser.add_argument(
        "--wait-for-quota",
        action="store_true",
        help="on a daily-quota error, sleep until the quota resets and continue",
    )
    asyncio.run(main(parser.parse_args()))
