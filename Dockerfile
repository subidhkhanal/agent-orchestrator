# One image, two processes: the API (uvicorn) and a background worker.
# Small free hosts give us one container, so scripts/start.sh runs both; at scale they would be
# separate deployments (see README, "Scaling to production").
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8000
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY scripts/start.sh ./scripts/start.sh

RUN useradd --create-home app && chmod +x scripts/start.sh
USER app
EXPOSE 8000
CMD ["./scripts/start.sh"]
