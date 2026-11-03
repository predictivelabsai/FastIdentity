#!/bin/sh
# Container entrypoint: background worker (matching, flows, retention) + web app.
# Set RUN_WORKER=0 when the worker runs as its own service (see docker-compose.yml).
set -e
if [ "${RUN_WORKER:-1}" = "1" ]; then
  python -m workers.run &
fi
exec python app.py
