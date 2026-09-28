"""Create (or update) the Render web service described in render.yaml, using RENDER_API_KEY.

    python scripts/render_deploy.py

Secrets come from the local .env (DEPLOY_DATABASE_URL, GROQ_API_KEY, TAVILY_API_KEY) and are sent
only to Render's API. Non-secret settings come from render.yaml, so there is one source of truth.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import yaml  # type: ignore[import-untyped]
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.render.com/v1"
REPO = "https://github.com/subidhkhanal/agent-orchestrator"


def main() -> None:
    env = dotenv_values(ROOT / ".env")
    key = env.get("RENDER_API_KEY")
    if not key:
        sys.exit("RENDER_API_KEY missing in .env")
    client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {key}"}, timeout=60)

    spec = yaml.safe_load((ROOT / "render.yaml").read_text())["services"][0]
    secrets = {
        "DATABASE_URL": env.get("DEPLOY_DATABASE_URL"),
        "GROQ_API_KEY": env.get("GROQ_API_KEY"),
        "TAVILY_API_KEY": env.get("TAVILY_API_KEY"),
    }
    missing = [k for k, v in secrets.items() if not v]
    if missing:
        sys.exit(f"missing in .env: {missing}")
    env_vars = [
        {"key": e["key"], "value": secrets[e["key"]] if e.get("sync") is False else e["value"]}
        for e in spec["envVars"]
    ]

    owner = client.get("/owners").json()[0]["owner"]["id"]
    existing = [
        s["service"] for s in client.get("/services", params={"name": spec["name"]}).json()
    ]
    if existing:
        service = existing[0]
        client.put(f"/services/{service['id']}/env-vars", json=env_vars).raise_for_status()
        client.post(f"/services/{service['id']}/deploys", json={}).raise_for_status()
        print("updated", service["id"], service["serviceDetails"]["url"])
        return

    body = {
        "type": "web_service",
        "name": spec["name"],
        "ownerId": owner,
        "repo": REPO,
        "branch": "main",
        "autoDeploy": "yes",
        "envVars": env_vars,
        "serviceDetails": {
            "runtime": "docker",
            "plan": spec["plan"],
            "region": spec["region"],
            "healthCheckPath": spec["healthCheckPath"],
            "envSpecificDetails": {"dockerfilePath": "./Dockerfile", "dockerContext": "."},
        },
    }
    response = client.post("/services", json=body)
    if response.status_code >= 400:
        sys.exit(f"Render API error {response.status_code}: {response.text[:500]}")
    service = response.json()["service"]
    print("created", service["id"], service["serviceDetails"]["url"])


if __name__ == "__main__":
    main()
