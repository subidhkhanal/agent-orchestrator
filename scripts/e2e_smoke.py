"""End-to-end smoke test against a running API + worker (any environment).

    python scripts/e2e_smoke.py --api http://localhost:8000 [--api-key ak_...]

Creates a research-memo run, waits for the approval gate, approves it, follows the SSE stream
to completion, and checks the published feed. Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    client = httpx.Client(base_url=args.api.rstrip("/"), headers=headers, timeout=30)

    body = {
        "graph_id": "research-memo",
        "input": {"task": "Write a memo summarizing Northwind Labs' paid parental leave."},
        "budget": {"max_usd": 0.03, "max_tokens": 80_000},
    }
    created = client.post(
        "/api/v1/graph-runs", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    created.raise_for_status()
    run_id = created.json()["run_id"]
    print("run", run_id)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        run = client.get(f"/api/v1/graph-runs/{run_id}").json()
        if run["status"] == "WAITING_HITL":
            break
        if run["status"] in ("FAILED", "CANCELLED", "COMPLETED"):
            sys.exit(f"unexpected status {run['status']}: {run.get('error')}")
        time.sleep(1)
    else:
        sys.exit("timed out waiting for the approval gate")
    gate = next(a["gate_id"] for a in run["approvals"] if a["status"] == "PENDING")
    print("gate", gate, "cost so far", run["cost"])
    for _ in range(2):  # double click on purpose
        r = client.post(f"/api/v1/graph-runs/{run_id}/hitl/{gate}/approve", json={})
        print("approve ->", r.status_code, r.json().get("outcome"))

    types: dict[str, int] = {}
    with client.stream(
        "GET", f"/api/v1/graph-runs/{run_id}/stream", timeout=args.timeout
    ) as stream:
        for line in stream.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                types[event["type"]] = types.get(event["type"], 0) + 1
    print("events:", dict(sorted(types.items())))
    final = client.get(f"/api/v1/graph-runs/{run_id}").json()
    published = [
        p for p in client.get("/api/v1/published").json()["published"] if p["run_id"] == run_id
    ]
    print("final status", final["status"], "published rows", len(published))
    if final["status"] != "COMPLETED" or len(published) != 1:
        sys.exit("end-to-end smoke test FAILED")
    print("end-to-end smoke test passed")


if __name__ == "__main__":
    main()
