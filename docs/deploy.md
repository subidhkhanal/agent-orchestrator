# Deploying

Status (2026-10-09): **live, on the Claude API.**

| Part | Where | Notes |
|---|---|---|
| Frontend | https://agent-orchestrator-opal.vercel.app | Vercel (Next.js) |
| Backend (API + in-process worker) | https://agent-orchestrator-api-xdcq.onrender.com | Render free web service, Docker, `render.yaml` |
| Database | Neon free (US East), provisioned through the Vercel marketplace | direct (unpooled) connection |

Deploy or update the backend with `python scripts/render_deploy.py` (reads `RENDER_API_KEY` and the
secrets from `.env`; pushes to `main` do not auto-deploy because the repo is not connected to
Render's GitHub app). The frontend deploys automatically on every push to `main` (the Vercel
project is linked to the repo with Root Directory `frontend`).

Verified on 2026-09-29 with a real task on the live site (browser-driven): the first draft was
rejected with a comment, the writer's next version added what the comment asked for, the
approved version was published exactly once (1 row, 1 delivery), in 157 s and 27,851 tokens.
On 2026-10-09 the demo moved from Groq's free tier to the Claude API (`config/models.toml`:
Claude Opus 5.5 for the researcher, writer and reviewer; Claude Haiku 5.5 for routing). A
measured local run reached the approval gate for $0.24 and 62k tokens, and the approved memo
was published once.

How the free tiers are handled:
- Render sleeps a free service after 15 minutes without traffic; the app pings its own `/livez`
  every 10 minutes (`KEEPALIVE_*`), so visitors do not hit a cold start.
- Neon suspends after 5 minutes without queries and the free plan has 100 compute-hours a
  month; the worker is woken in-process instead of polling (`WORKER_POLL_S=900`), idle
  connections close after 60 s, and the SSE listener only connects while someone watches a run.
- The Claude API is paid, so the demo has hard caps: $1.50 and 600k tokens per run, $4.00 and
  8M tokens per day (new runs get 503 once reached), and 10 runs per hour per IP.

## What changed from the original plan

The plan was Hugging Face Spaces (backend container) + Neon (Postgres) + Vercel (frontend).
When the Space was created, Hugging Face answered `402 Payment Required`: Docker and Gradio
Spaces on the free CPU tier now need a PRO subscription. The owner chose to deploy the frontend
only for now. Without a backend, the page shows a clear "backend not deployed yet" notice with
the Graph A diagram, and the Run button is disabled (nothing on the page pretends to work).

## Frontend (Vercel)

From `frontend/`:

```bash
npx vercel login          # once; opens the browser
npx vercel --prod         # creates the project on first run and deploys
```

Do not set `NEXT_PUBLIC_API_URL` until a backend exists. When it does, set it in the Vercel
project (Settings → Environment Variables) to the backend's public URL and redeploy.

## Backend options (when you want the live demo)

The container (`Dockerfile`, `scripts/start.sh`) runs migrations, one worker and the API, and
needs a long-running process, so request-scoped serverless platforms (including Vercel
Functions) do not fit without redesigning the worker.

| Host | Cost | Notes |
|---|---|---|
| Hugging Face Space (Docker) | PRO, $9/month | Deploy straight from this folder with `huggingface_hub`; sleeps after ~48 h idle |
| Render web service (Docker) | free tier | Needs the code in a Git repo; sleeps after 15 min idle (~1 min cold start) |
| Any small VM | varies | `docker compose up` with the included compose file |

Database: a new Neon project (free), created from the Vercel dashboard (Storage → Create →
Neon) or on neon.tech. Use the **direct (unpooled)** connection string: `LISTEN/NOTIFY` and the
LangGraph checkpointer need session-level connections. Then run `alembic upgrade head` once
against it.

Backend environment (never commit these):

| Name | Value |
|---|---|
| `PORT` | whatever the host expects (7860 on Hugging Face) |
| `DATABASE_URL` | Neon direct URL |
| `ANTHROPIC_API_KEY`, `TAVILY_API_KEY` | keys for this project only |
| `DEMO_MODE` | `true` |
| `CORS_ORIGINS` | the Vercel URL |
| `DEMO_MAX_USD_PER_RUN` | `1.50` (hard per-run cap; the demo tenant's allowance) |
| `DAILY_USD_CAP` / `DAILY_TOKEN_CAP` | `4.00` / `8000000` (new runs get 503 once reached) |
| `RATE_LIMIT_RUNS_PER_HOUR`, `MAX_TASK_CHARS` | `10`, `500` |

Verify after deploying: `python scripts/e2e_smoke.py --api https://<backend>`; then check that a
request above $1.50 gets 402, a 501-character task gets 422, the 11th run in an hour from one
IP gets 429, and runs after the daily cap get 503 with `Retry-After`.

The publish sink only writes to the app's own `published_memos` table. It never sends email or
posts anywhere else.
