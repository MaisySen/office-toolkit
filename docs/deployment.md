# Deployment

Standard self-hosted layout: the toolkit web app, Keycloak (identity),
Postgres (Keycloak's store) and Caddy (automatic TLS) behind one hostname.
Files: Dockerfile, docker-compose.yml, Caddyfile, .env.example.

## Identity

Production sign-in is delegated to Keycloak (Apache-2.0, latest release
26.8.1 on 2026-10-13:
https://www.keycloak.org/2026/10/keycloak-2681-released). Self-registration,
email verification, password reset and MFA are Keycloak built-ins; no custom
auth code ships for those journeys. The toolkit is an OIDC relying party
(Authlib). The built-in local account system remains for offline development
and tests only, not the production path.

Keycloak setup after first boot (admin console at /realms/master or /admin):

1. Create realm "office-toolkit".
2. Create client "office-toolkit-web": standard flow, PKCE S256, confidential,
   redirect URI https://<host>/login/oidc/callback. Put its secret in .env.
3. Realm settings: login tab - registration on, verify email on, forgot
   password on. Email tab: SMTP from .env values.
4. The mobile app uses a second public client with the custom scheme
   redirect, authorization code + PKCE against the same realm's discovery
   document.

## Email identity

The verification/reset sender address is NOT decided. The Fintex company
contact address is only a recommendation candidate until the owner grounds
the sending identity and scope; .env.example keeps a neutral placeholder.

## Email in tests

No external mail is sent by tests. The CI end-to-end job captures mail from
Keycloak in a local SMTP catcher and completes the verification and reset
journeys against the captured links.

## Hostname and host

A public hostname (DNS) and a host are owner-level decisions; neither exists
yet for this app. Until then the compose file is the reviewed but
not-yet-executed deployment definition; the Keycloak CI journey (below) is
the executable proof.

## Verification status

- Toolkit test suite: 89 tests, green in CI on every push.
- OIDC login verified end-to-end against a real maintained IdP (Authelia
  v4.39.28) locally; Keycloak-specific verification runs in CI (separate
  job), not claimed from the Authelia result.
- The Dockerfile and compose file are statically written and reviewed; they
  have not been executed in the development sandbox (resource limits).
