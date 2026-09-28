"""Core schema: tenants, graph versions, runs, events, approvals, effects, artifacts.

Revision ID: 0001
Revises:
Create Date: 2026-09-28

Notes
- Every tenant-owned table carries tenant_id. graph_versions is the exception: graphs are
  registered in code and shared by all tenants.
- graph_events and graph_versions are append-only / immutable. That is enforced by triggers
  here, not just by application code.
- graph_runs.attempt is the lease generation. Each claim increments it; it doubles as a
  fencing token so a worker whose lease expired cannot write after another worker took over.
- The LangGraph checkpointer tables are created by the library's own migrations (0002).
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE = """
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only / immutable (% blocked)', TG_TABLE_NAME, TG_OP;
END;
$$;

CREATE TABLE tenants (
  tenant_id              text PRIMARY KEY,
  name                   text NOT NULL,
  max_usd_per_run        numeric(12, 6) NOT NULL CHECK (max_usd_per_run > 0),
  max_tokens_per_run     integer NOT NULL CHECK (max_tokens_per_run > 0),
  created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
  key_id                 text PRIMARY KEY,
  tenant_id              text NOT NULL REFERENCES tenants (tenant_id),
  key_hash               text NOT NULL UNIQUE,  -- sha256 of the secret; the secret is never stored
  label                  text NOT NULL DEFAULT '',
  created_at             timestamptz NOT NULL DEFAULT now(),
  revoked_at             timestamptz
);
CREATE INDEX api_keys_tenant_idx ON api_keys (tenant_id);

CREATE TABLE graph_versions (
  graph_id               text NOT NULL,
  version                integer NOT NULL CHECK (version > 0),
  topology_hash          text NOT NULL,
  topology               jsonb NOT NULL,
  description            text NOT NULL DEFAULT '',
  created_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (graph_id, version)
);
CREATE TRIGGER graph_versions_immutable
  BEFORE UPDATE OR DELETE ON graph_versions
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE graph_runs (
  run_id                 text PRIMARY KEY,
  tenant_id              text NOT NULL REFERENCES tenants (tenant_id),
  graph_id               text NOT NULL,
  graph_version          integer NOT NULL,
  status                 text NOT NULL DEFAULT 'QUEUED' CHECK (status IN
                           ('QUEUED', 'RUNNING', 'WAITING_HITL',
                            'COMPLETED', 'FAILED', 'CANCELLED')),
  input                  jsonb NOT NULL,
  budget                 jsonb NOT NULL,
  options                jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- Latest committed RunState (a cache for the API; the LangGraph checkpoint is what
  -- execution resumes from). Artifact content is not in here, only references.
  state                  jsonb NOT NULL,
  state_version          integer NOT NULL DEFAULT 0,
  attempt                integer NOT NULL DEFAULT 0,
  lease_owner            text,
  lease_expires_at       timestamptz,
  last_event_id          bigint NOT NULL DEFAULT 0,
  -- Set by approve/reject; consumed by the worker that resumes the paused graph.
  resume_payload         jsonb,
  cancel_requested       boolean NOT NULL DEFAULT false,
  create_idempotency_key text NOT NULL,
  create_request_hash    text NOT NULL,
  error                  text,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  finished_at            timestamptz,
  FOREIGN KEY (graph_id, graph_version) REFERENCES graph_versions (graph_id, version),
  UNIQUE (tenant_id, create_idempotency_key)
);
-- Work queue scan: runnable runs, and running runs whose lease has expired.
CREATE INDEX graph_runs_claimable_idx ON graph_runs (status, lease_expires_at)
  WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX graph_runs_tenant_idx ON graph_runs (tenant_id, created_at DESC);

CREATE TABLE graph_events (
  run_id                 text NOT NULL REFERENCES graph_runs (run_id),
  event_id               bigint NOT NULL,
  tenant_id              text NOT NULL REFERENCES tenants (tenant_id),
  event_type             text NOT NULL,
  payload                jsonb NOT NULL,
  idempotency_key        text NOT NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, event_id),
  UNIQUE (run_id, idempotency_key)
);
CREATE TRIGGER graph_events_append_only
  BEFORE UPDATE OR DELETE ON graph_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE hitl_approvals (
  run_id                 text NOT NULL REFERENCES graph_runs (run_id),
  gate_id                text NOT NULL,
  tenant_id              text NOT NULL REFERENCES tenants (tenant_id),
  artifact_id            text NOT NULL,
  artifact_version       integer NOT NULL,
  status                 text NOT NULL DEFAULT 'PENDING' CHECK (status IN
                           ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
  reviewer               text,
  comment                text,
  requested_at           timestamptz NOT NULL DEFAULT now(),
  expires_at             timestamptz,
  decided_at             timestamptz,
  PRIMARY KEY (run_id, gate_id)
);
CREATE INDEX hitl_approvals_pending_idx ON hitl_approvals (expires_at) WHERE status = 'PENDING';

CREATE TABLE effects (
  effect_key             text PRIMARY KEY,
  run_id                 text NOT NULL REFERENCES graph_runs (run_id),
  tenant_id              text NOT NULL REFERENCES tenants (tenant_id),
  kind                   text NOT NULL,
  status                 text NOT NULL DEFAULT 'PENDING' CHECK (status IN
                           ('PENDING', 'SUCCEEDED', 'FAILED')),
  request                jsonb NOT NULL,
  response               jsonb,
  attempts               integer NOT NULL DEFAULT 0,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX effects_run_idx ON effects (run_id);

CREATE TABLE artifacts (
  run_id                 text NOT NULL REFERENCES graph_runs (run_id),
  artifact_id            text NOT NULL,
  version                integer NOT NULL CHECK (version > 0),
  tenant_id              text NOT NULL REFERENCES tenants (tenant_id),
  type                   text NOT NULL,
  content                text NOT NULL,
  content_sha256         text NOT NULL,
  producer_agent         text NOT NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  -- The hash is part of the key: a node re-executed after a crash may write different
  -- content for the same version; both copies are kept and state references one of them.
  PRIMARY KEY (run_id, artifact_id, version, content_sha256)
);
CREATE TRIGGER artifacts_immutable
  BEFORE UPDATE OR DELETE ON artifacts
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
"""

DOWNGRADE = """
DROP TABLE IF EXISTS artifacts;
DROP TABLE IF EXISTS effects;
DROP TABLE IF EXISTS hitl_approvals;
DROP TABLE IF EXISTS graph_events;
DROP TABLE IF EXISTS graph_runs;
DROP TABLE IF EXISTS graph_versions;
DROP TABLE IF EXISTS api_keys;
DROP TABLE IF EXISTS tenants;
DROP FUNCTION IF EXISTS forbid_mutation();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
