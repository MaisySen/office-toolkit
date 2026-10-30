# Free tier deploy: doctool.multies.si

v1 runs on free tiers with the app's local accounts (no Keycloak yet).

- App host: Render free web service, Docker runtime, built from this repo
  with deploy/render/Dockerfile. It sleeps after 15 minutes without traffic, so
  the Worker cron pings /healthz every 10 minutes to keep it warm.
- Edge: a Cloudflare Worker (deploy/render/worker.js, wrangler.toml) owns
  https://doctool.multies.si, adds the shared proxy header and forwards to
  the Render address. The app answers 403 to anything without that header,
  so the Render address is not a second way in.
- State: the SQLite auth database lives on the host's temporary disk.
  deploy/render/persist.py restores it at boot and uploads an encrypted
  snapshot (Fernet) to a private Hugging Face dataset when it changes.
- Upload limit: 100 MB per request in the app; the Worker free plan allows
  the same.

Secrets on the Render service (names only): TOOLKIT_SECRET_KEY,
TOOLKIT_BOOTSTRAP_TOKEN, DOCTOOL_PROXY_SECRET, DOCTOOL_STATE_KEY, HF_TOKEN.
Plain settings: DOCTOOL_STATE_REPO, PUBLIC_HOSTNAME, GUNICORN_WORKERS.
Worker secret: PROXY_SECRET (same value as DOCTOOL_PROXY_SECRET).

Engine mode for Multies: TOOLKIT_SERVICE_KEYS (a JSON map of key id to
secret, ids stg-1 and prod-1) turns on the signed internal job API under
/internal/v1, and TOOLKIT_ENGINE_ONLY=1 makes every other route answer 404
(web pages, local accounts, OAuth, /api/run) and skips the auth database
snapshots, which only local accounts need. Multies signs each call; see
office_toolkit/web/service_auth.py and office_toolkit/web/engine.py.

Next steps: Keycloak (needs a host with more memory), SMTP for account mail,
and a paid or self hosted box once traffic needs no cold starts.
