# Deployment

Target: the toolkit web app and Keycloak behind Caddy on one host,
served as https://doctool.multies.si with Keycloak under /auth.

## What is automated here

* `deploy/Caddyfile` - edge proxy: `/auth/*` to Keycloak on 127.0.0.1:8080,
  everything else to the toolkit on 127.0.0.1:5000. Caddy issues and
  renews the TLS certificate itself.
* `deploy/keycloak.service` and `deploy/office-toolkit.service` -
  systemd units. Keycloak runs `kc.sh start --optimized`; run
  `/opt/keycloak/bin/kc.sh build` once during install (and after any
  config change) so the optimized start is valid.
* `deploy/env.example` - every environment value both services need,
  as placeholders. Real values go in `/etc/office-toolkit/*.env` with
  mode 600 and never enter git.

## Host setup (once)

1. Point `doctool.multies.si` at the host (A/AAAA, or CNAME to the
   host's name) in the zone's DNS. Until this record exists nothing
   below is reachable.
2. Install Caddy, Java 17+, Python 3.12+, PostgreSQL, and copy this
   repo to /opt/office-toolkit with a venv (`pip install -r
   requirements.txt`, plus gunicorn).
3. Unpack the Keycloak 26.x tarball to /opt/keycloak, create the
   `keycloak` and `toolkit` users, place the env files, run
   `kc.sh build`, enable and start both units, and install the
   Caddyfile (`caddy reload`).
4. Create the `office-toolkit` realm and the two clients
   (`office-toolkit-web` confidential with redirect
   `https://doctool.multies.si/login/oidc/callback`,
   `office-toolkit-mobile` public with redirect
   `officetoolkit://auth`, PKCE S256 on both). The mobile client also
   needs the protocol mapper that adds the `office-toolkit-api` audience
   to its tokens: office_toolkit/web/app.py rejects bearer JWTs without
   that aud, so a realm built without the mapper lets sign-in complete
   and then fails every API call. scripts/ci_runtime_stack.py
   shows the exact admin-API calls the CI uses for the same setup;
   the production run differs only in base URL, redirect URIs and
   secrets.
5. Configure the realm's smtpServer so verification and reset emails
   send. The sender service is an owner decision (which mailbox or
   transactional service); CI uses a local catch-all on port 1025.

## Mobile builds

The app reads its endpoints from `extra` in mobile/app.config.js, so
production builds set TOOLKIT_OIDC_ISSUER and TOOLKIT_API_BASE_URL to
the public URLs above. expo-constants embeds them at build time (its
gradle/Xcode createExpoConfig step re-evaluates app.config.js on every
build), so the variables must be present in the build environment, not
only at prebuild time.

## Still gated on access, not on work

* DNS record for doctool.multies.si (zone is on Cloudflare; the record
  does not exist yet).
* A host to run this on (no server access exists in this workspace).
* An approved SMTP sender service for the realm.
