#!/bin/sh
# Container entrypoint: N uvicorn workers (WEB_CONCURRENCY), Prometheus
# multiprocess mode when N > 1 so /metrics sums counters across workers.
set -e

WORKERS="${WEB_CONCURRENCY:-1}"
if [ "$WORKERS" -gt 1 ] && [ -z "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
  export PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus-multiproc
fi
if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
  # Stale files from a previous run would be summed into the new counters.
  rm -rf "$PROMETHEUS_MULTIPROC_DIR"
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --workers "$WORKERS" \
  --no-access-log \
  --timeout-keep-alive 75 \
  --backlog 4096
