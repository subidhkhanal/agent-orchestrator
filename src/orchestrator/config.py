"""Process-level settings, read from the environment (and a local .env file)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str | None = None
    models_config: str = "config/models.toml"
    # Use the offline fake LLM (config/models.fake.toml + scripted policy). CI, chaos tests.
    fake_llm: bool = False
    fake_llm_delay_s: float = 0.0

    # Researcher services. Without a Tavily key the researcher uses canned web results.
    tavily_api_key: str | None = None
    docqa_base_url: str | None = "https://document-qa-api.vercel.app"

    # Engine
    checkpoint_event_every_n: int = 5
    default_max_steps: int = 30
    hitl_timeout_hours: float = 24.0

    # Database pool size per process (keep small behind hosted connection poolers).
    db_pool_size: int = 10
    # 0 + a short idle timeout lets a scale-to-zero database suspend when the app is idle.
    db_pool_min: int = 1
    db_pool_max_idle_s: float = 600.0

    # Worker
    # Run the worker inside the API process (single small container hosts).
    embedded_worker: bool = False
    worker_id: str | None = None  # default: hostname:pid
    worker_metrics_port: int | None = None  # serve Prometheus metrics from the worker
    worker_lease_s: float = 30.0
    worker_heartbeat_s: float = 5.0
    worker_poll_s: float = 1.0
    worker_sweep_s: float = 10.0  # how often to expire timed-out approvals
    # Where the publish node sends memos. Empty: write to the sink table directly.
    publish_sink_url: str | None = None
    publish_sink_token: str | None = None

    # Free hosts that scale idle services to zero: ping our own /health to stay warm.
    keepalive_url: str | None = None
    keepalive_interval_s: float = 1200.0

    # API
    cors_origins: str = "http://localhost:3000"
    public_base_url: str = "http://localhost:8000"

    # Demo mode: requests without an API key act as the demo tenant, under hard caps.
    demo_mode: bool = False
    demo_tenant_id: str = "demo"
    demo_max_usd_per_run: float = 0.03
    demo_max_tokens_per_run: int = 100_000
    daily_usd_cap: float = 1.00
    # Free LLM tiers limit tokens per day, which binds long before the USD cap does.
    daily_token_cap: int = 400_000
    max_task_chars: int = 500
    rate_limit_runs_per_hour: int = 10
