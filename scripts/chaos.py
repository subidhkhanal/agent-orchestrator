"""Chaos test: start N runs, hard-kill worker processes at random, and count duplicate effects.

    python scripts/chaos.py --runs 50 --workers 3 --kill-every 1.5

What it does
- Creates N research-memo runs for a dedicated `chaos` tenant (fake LLM, so no API cost).
- Keeps W worker processes alive (`python -m orchestrator.worker`). Every ~K seconds it kills
  one with TerminateProcess/SIGKILL (no cleanup, like a crash or OOM kill) and starts a new
  one. Short leases (3 s) make orphaned runs reclaimable quickly.
- Plays the human: approves every waiting gate, sometimes with 2-3 concurrent approve calls
  to simulate double clicks.
- Waits until every run is terminal, then reports completion rate, how many runs were resumed
  by another worker, and duplicate external effects (target: 0).

Results go to evals/results/chaos.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestrator.runtime import load_settings  # noqa: E402

settings = load_settings()

import orchestrator.db  # noqa: E402, F401
from orchestrator.db.graphs import sync_graph_versions  # noqa: E402
from orchestrator.db.hitl import DecisionConflict, HitlStore, NotWaiting  # noqa: E402
from orchestrator.db.pool import open_pool  # noqa: E402
from orchestrator.db.runs import RunStore, new_run_id, request_hash  # noqa: E402
from orchestrator.db.tenants import TenantStore  # noqa: E402
from orchestrator.graphs.definitions import default_registry  # noqa: E402
from orchestrator.state.models import Budget, RunState  # noqa: E402

TENANT = "chaos"


def spawn_worker(i: int, lease_s: float) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "FAKE_LLM": "true",
        "FAKE_LLM_DELAY_S": "0.03",
        "WORKER_LEASE_S": str(lease_s),
        "WORKER_HEARTBEAT_S": str(lease_s / 5),
        "WORKER_POLL_S": "0.1",
        "PYTHONPATH": str(ROOT / "src"),
        "WORKER_ID": f"chaos-{i}",
    }
    log = open(ROOT / "evals" / "results" / f"chaos-worker-{i}.log", "wb")  # noqa: SIM115
    return subprocess.Popen(
        [sys.executable, "-m", "orchestrator.worker"], env=env, stdout=log, stderr=log, cwd=ROOT
    )


def hard_kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill without cleanup. On Windows a venv's python.exe is a launcher that runs the real
    interpreter as a child process, so the whole process tree has to go."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    proc.wait()


async def main(args: argparse.Namespace) -> None:
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set")
    (ROOT / "evals" / "results").mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    pool = await open_pool(settings.database_url, max_size=10)
    runs, hitl = RunStore(pool), HitlStore(pool)
    await TenantStore(pool).create_tenant(TENANT, "Chaos test", 10.0, 1_000_000)
    registry = default_registry()
    await sync_graph_versions(pool, registry)
    spec = registry.get("research-memo", 1)

    started_at = time.time()
    run_ids = []
    for i in range(args.runs):
        run_id = new_run_id()
        state = RunState(
            run_id=run_id,
            tenant_id=TENANT,
            graph_id=spec.graph_id,
            graph_version=spec.version,
            task=f"Chaos task {i}: research grid storage and draft a memo",
            budget=Budget(
                token_limit=100_000, usd_limit=1.0, tokens_remaining=100_000, usd_remaining=1.0
            ),
        )
        await runs.create(
            tenant_id=TENANT,
            idempotency_key=run_id,
            body_hash=request_hash({"i": i}),
            initial_state=state,
            topology_hash=spec.topology_hash(),
            options={},
        )
        run_ids.append(run_id)

    workers = {i: spawn_worker(i, args.lease) for i in range(args.workers)}
    next_id = args.workers
    kills = kills_mid_run = approvals = double_clicks = 0
    next_kill = time.time() + rng.uniform(0.5, 2 * args.kill_every)
    deadline = time.time() + args.timeout

    async def approve(run_id: str, gate_id: str) -> str:
        try:
            result = await hitl.decide(
                tenant_id=TENANT,
                run_id=run_id,
                gate_id=gate_id,
                decision="approved",
                reviewer="chaos",
                comment=None,
            )
            return result.outcome
        except (NotWaiting, DecisionConflict) as exc:
            return type(exc).__name__

    try:
        while time.time() < deadline:
            async with pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT status, count(*) AS n FROM graph_runs WHERE run_id = ANY(%s) "
                        "GROUP BY status",
                        (run_ids,),
                    )
                ).fetchall()
                pending = await (
                    await conn.execute(
                        "SELECT run_id, gate_id FROM hitl_approvals WHERE run_id = ANY(%s) "
                        "AND status = 'PENDING'",
                        (run_ids,),
                    )
                ).fetchall()
            counts = {r["status"]: r["n"] for r in rows}
            terminal = sum(counts.get(s, 0) for s in ("COMPLETED", "FAILED", "CANCELLED"))
            if int(time.time() - started_at) % 10 == 0:
                print(f"{time.time() - started_at:6.1f}s {counts} kills={kills}", flush=True)
            if terminal == len(run_ids):
                break

            for gate in pending:
                clicks = rng.choice([1, 1, 2, 3])
                double_clicks += clicks > 1
                approvals += 1
                await asyncio.gather(
                    *(approve(gate["run_id"], gate["gate_id"]) for _ in range(clicks))
                )

            if time.time() >= next_kill:
                # Target a worker that currently holds a lease, so the kill interrupts a run
                # mid-flight (killing an idle worker proves nothing).
                async with pool.connection() as conn:
                    owners = await (
                        await conn.execute(
                            "SELECT lease_owner FROM graph_runs WHERE run_id = ANY(%s) "
                            "AND status = 'RUNNING' AND lease_owner IS NOT NULL",
                            (run_ids,),
                        )
                    ).fetchall()
                holders = {o["lease_owner"] for o in owners}
                busy = [i for i in workers if f"chaos-{i}" in holders]
                victim = rng.choice(busy or list(workers))
                hard_kill(workers[victim])
                kills += 1
                kills_mid_run += bool(busy)
                del workers[victim]
                workers[next_id] = spawn_worker(next_id, args.lease)
                next_id += 1
                next_kill = time.time() + rng.uniform(0.5, 2 * args.kill_every)
            await asyncio.sleep(0.2)
    finally:
        for proc in workers.values():
            hard_kill(proc)
        print()

    async with pool.connection() as conn:

        async def one(sql: str) -> list[dict[str, object]]:
            return await (await conn.execute(sql, (run_ids,))).fetchall()

        status_rows = await one(
            "SELECT status, count(*) AS n FROM graph_runs WHERE run_id = ANY(%s) GROUP BY status"
        )
        per_run_publishes = await one(
            "SELECT run_id, count(*) AS rows, sum(deliveries) AS deliveries FROM published_memos "
            "WHERE run_id = ANY(%s) GROUP BY run_id"
        )
        resumed = await one(
            "SELECT count(DISTINCT run_id) AS n FROM graph_events WHERE run_id = ANY(%s) "
            "AND event_type = 'run_claimed' AND payload->>'mode' = 'recover'"
        )
        recoveries = await one(
            "SELECT count(*) AS n FROM graph_events WHERE run_id = ANY(%s) "
            "AND event_type = 'run_claimed' AND payload->>'mode' = 'recover'"
        )
        decided = await one(
            "SELECT run_id, payload->>'gate_id' AS gate_id, count(*) AS n FROM graph_events "
            "WHERE run_id = ANY(%s) "
            "AND event_type = 'hitl_decided' GROUP BY 1, 2 HAVING count(*) > 1"
        )
        failed = await one(
            "SELECT run_id, error FROM graph_runs WHERE run_id = ANY(%s) AND status = 'FAILED'"
        )
    await pool.close()

    statuses = {r["status"]: r["n"] for r in status_rows}
    completed = int(statuses.get("COMPLETED", 0))  # type: ignore[call-overload]
    duplicate_publishes = sum(int(r["rows"]) - 1 for r in per_run_publishes)  # type: ignore[call-overload]
    deduped_deliveries = sum(int(r["deliveries"]) - int(r["rows"]) for r in per_run_publishes)  # type: ignore[call-overload]
    result = {
        "runs": len(run_ids),
        "workers": args.workers,
        "worker_kills": kills,
        "kills_while_holding_a_run": kills_mid_run,
        "statuses": statuses,
        "completed_pct": round(100 * completed / len(run_ids), 1),
        "runs_resumed_by_another_worker": resumed[0]["n"],
        "total_recoveries": recoveries[0]["n"],
        "runs_published": len(per_run_publishes),
        "duplicate_external_effects": duplicate_publishes,
        "deduplicated_redeliveries": deduped_deliveries,
        "approvals": approvals,
        "approvals_with_double_clicks": double_clicks,
        "gates_resumed_more_than_once": len(decided),
        "failed_runs": [dict(r) for r in failed],
        "wall_clock_s": round(time.time() - started_at, 1),
        "seed": args.seed,
    }
    (ROOT / "evals" / "results" / "chaos.json").write_text(
        json.dumps(result, indent=2, default=str)
    )
    print(json.dumps(result, indent=2, default=str))
    ok = duplicate_publishes == 0 and not decided and completed == len(run_ids)
    if not ok:
        sys.exit("chaos test failed: duplicates, double resumes or unfinished runs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--kill-every", type=float, default=1.5, help="mean seconds between kills")
    parser.add_argument("--lease", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--seed", type=int, default=7)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    asyncio.run(main(parser.parse_args()))
