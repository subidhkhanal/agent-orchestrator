"""Schema checks against a real Postgres. Skipped unless TEST_DATABASE_URL is set."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.postgres


def alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=env)


@pytest.fixture(scope="module")
def migrated(test_db_url: str) -> str:
    alembic(test_db_url, "downgrade", "base")
    alembic(test_db_url, "upgrade", "head")
    return test_db_url


def test_events_are_append_only_and_graph_versions_immutable(migrated: str) -> None:
    import psycopg

    with psycopg.connect(migrated, autocommit=True) as conn:
        conn.execute("TRUNCATE tenants, graph_versions, graph_runs, graph_events CASCADE")
        conn.execute("INSERT INTO tenants VALUES ('t1', 'T', 1.0, 100000, now())")
        conn.execute(
            "INSERT INTO graph_versions (graph_id, version, topology_hash, topology) "
            "VALUES ('g', 1, 'h', '{}')"
        )
        conn.execute(
            "INSERT INTO graph_runs (run_id, tenant_id, graph_id, graph_version, input, budget, "
            "state, create_idempotency_key, create_request_hash) "
            "VALUES ('r1', 't1', 'g', 1, '{}', '{}', '{}', 'k', 'h')"
        )
        conn.execute(
            "INSERT INTO graph_events (run_id, event_id, tenant_id, event_type, payload, "
            "idempotency_key) VALUES ('r1', 1, 't1', 'node_started', '{}', 'a1')"
        )
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE graph_events SET payload = '{\"x\": 1}'")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM graph_events")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE graph_versions SET topology_hash = 'other'")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO graph_events (run_id, event_id, tenant_id, event_type, payload, "
                "idempotency_key) VALUES ('r1', 2, 't1', 'node_started', '{}', 'a1')"
            )
