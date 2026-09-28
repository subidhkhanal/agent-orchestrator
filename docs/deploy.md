# Deploying

Status (2026-09-29): **the frontend is live at https://agent-orchestrator-opal.vercel.app (offline mode); the backend is not hosted.**

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
| `GROQ_API_KEY`, `TAVILY_API_KEY` | keys for this project only |
| `DEMO_MODE` | `true` |
| `CORS_ORIGINS` | the Vercel URL |
| `DEMO_MAX_USD_PER_RUN` | `0.03` (hard per-run cap; the demo tenant's allowance) |
| `DAILY_USD_CAP` / `DAILY_TOKEN_CAP` | `1.00` / `400000` (new runs get 503 once reached) |
| `RATE_LIMIT_RUNS_PER_HOUR`, `MAX_TASK_CHARS` | `10`, `500` |

Verify after deploying: `python scripts/e2e_smoke.py --api https://<backend>`; then check that a
request above $0.03 gets 402, a 501-character task gets 422, the 11th run in an hour from one
IP gets 429, and runs after the daily cap get 503 with `Retry-After`.

The publish sink only writes to the app's own `published_memos` table. It never sends email or
posts anywhere else.
