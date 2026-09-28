"""LangGraph checkpointer tables and the publish sink's table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

The checkpointer owns its schema and its own migration list (checkpoint_migrations table), so
we call its setup() instead of copying its DDL. It creates indexes CONCURRENTLY, which cannot
run inside Alembic's transaction, so it uses its own autocommit connection.

published_memos stands in for an external downstream system. It lives in this database only
for convenience; the code talks to it through the publish-sink HTTP endpoint (or its client),
never through the effects ledger's tables.
"""

import os

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _setup_checkpointer() -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    url = op.get_bind().engine.url.render_as_string(hide_password=False)
    url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE published_memos (
          id                 bigserial PRIMARY KEY,
          idempotency_key    text NOT NULL UNIQUE,
          tenant_id          text NOT NULL,
          run_id             text NOT NULL,
          artifact_id        text NOT NULL,
          artifact_version   integer NOT NULL,
          content            text NOT NULL,
          published_at       timestamptz NOT NULL DEFAULT now(),
          -- How many times a publish with this key was received (1 + deduplicated replays).
          deliveries         integer NOT NULL DEFAULT 1
        );
        CREATE INDEX published_memos_tenant_idx ON published_memos (tenant_id, published_at DESC);
        """
    )
    if not context_is_offline():
        _setup_checkpointer()


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS published_memos;
        DROP TABLE IF EXISTS checkpoint_writes;
        DROP TABLE IF EXISTS checkpoint_blobs;
        DROP TABLE IF EXISTS checkpoints;
        DROP TABLE IF EXISTS checkpoint_migrations;
        """
    )


def context_is_offline() -> bool:
    from alembic import context

    return context.is_offline_mode() or os.environ.get("SKIP_CHECKPOINTER_SETUP") == "1"
