#!/bin/sh
set -eu

# Host bind mounts are often root-owned; fix ownership when the container starts as root.
mkdir -p /app/logs /app/exports /app/data

if [ "$(id -u)" = "0" ]; then
  if id appuser >/dev/null 2>&1; then
    chown -R appuser:appuser /app/logs /app/exports /app/data 2>/dev/null || true
    exec gosu appuser "$@"
  fi
fi

exec "$@"
