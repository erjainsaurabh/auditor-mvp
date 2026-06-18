#!/bin/sh
# docker-entrypoint.sh
#
# Flow YAMLs and test_data.yaml are delivered at runtime via the API.
# This script only ensures the required directories exist on the persistent
# volume before starting the server.
#
# /app/flows   → persistent volume: fingerprints + strategy_stats accumulate here
# /app/evidence → run output (screenshots, reports) — ephemeral is fine

set -e

mkdir -p /app/flows /app/evidence

echo "[entrypoint] starting uvicorn..."
exec uvicorn api:app --host 0.0.0.0 --port 8000
