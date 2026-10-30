#!/bin/sh
# Serve the toolkit. In engine only mode (Multies) there are no local
# accounts, so nothing is restored or snapshotted; otherwise restore the
# auth database and keep snapshotting it first.
set -e
if [ "${TOOLKIT_ENGINE_ONLY:-}" != "1" ]; then
  python /app/deploy/render/persist.py restore
  python /app/deploy/render/persist.py loop &
fi
exec gunicorn -w "${GUNICORN_WORKERS:-2}" -t 300 --graceful-timeout 30 \
  -b "0.0.0.0:${PORT:-10000}" --access-logfile - deploy.render.wsgi:app
