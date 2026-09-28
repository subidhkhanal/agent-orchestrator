#!/bin/sh
# Container entrypoint: migrate, then run the API and the worker.
# With EMBEDDED_WORKER=true (small free hosts) the worker runs inside the API process.
set -e
alembic upgrade head
if [ "${EMBEDDED_WORKER:-false}" = "true" ]; then
  exec python -m orchestrator.serve --port "${PORT:-8000}"
fi
python -m orchestrator.worker &
WORKER=$!
python -m orchestrator.serve --port "${PORT:-8000}" &
API=$!
trap 'kill $WORKER $API 2>/dev/null' TERM INT
while kill -0 $WORKER 2>/dev/null && kill -0 $API 2>/dev/null; do sleep 2; done
echo "a process exited; stopping container" >&2
kill $WORKER $API 2>/dev/null || true
exit 1
