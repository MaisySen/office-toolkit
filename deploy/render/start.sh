#!/bin/sh
# Restore the auth database, keep snapshotting it, then serve.
set -e
python /app/deploy/render/persist.py restore
python /app/deploy/render/persist.py loop &
exec gunicorn -w "${GUNICORN_WORKERS:-2}" -t 300 --graceful-timeout 30 \
  -b "0.0.0.0:${PORT:-10000}" --access-logfile - deploy.render.wsgi:app
