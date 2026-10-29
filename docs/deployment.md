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
4. The mobile app uses a second public client "office-toolkit-mobile":
   standard flow, PKCE S256, public, redirect URIs officetoolkit://auth
   (and http://localhost:8081/auth for development), authorization code +
   PKCE against the same realm's discovery document. Add a protocol
   mapper to it: oidc-audience-mapper, included custom audience =
   "office-toolkit-api", access token only. The API rejects bearer tokens
   whose audience lacks that value (TOOLKIT_OIDC_AUDIENCE overrides the
   expected audience; TOOLKIT_MOBILE_CLIENT_ID overrides the client id
   the API expects as authorized party).

## Email identity

The verification/reset sender address is NOT decided. The Fintex company
contact address is only a recommendation candidate until the owner grounds
the sending identity and scope; .env.example keeps a neutral placeholder.

## Email in tests

No external mail is sent by tests. The CI end-to-end job captures mail from
Keycloak in a local SMTP catcher and completes the verification and reset
journeys against the captured links.

## Hostname and host

The owner selected the production hostname on 2026-10-29:
doctool.multies.si. The multies.si apex advertises HSTS with
includeSubDomains (and the preload directive in its header, but the
domain is not on the Chromium preload list per hstspreload.org's status
API, so only clients that have visited the apex inherit the policy).
Regardless of HSTS, the host must serve valid HTTPS from day one;
Caddy's automatic TLS covers that once DNS points at the machine. Still
owner-level and open: the DNS record itself (the zone is on Cloudflare),
the host machine, and the verification/reset sender identity. Until
those exist the compose file is the reviewed but not-yet-executed
deployment definition; the Keycloak CI journey (below) is the executable
proof.

## Verification status

- Toolkit test suite: 109 tests, green in CI on every push.
- Keycloak journey (26.8, real browser + captured mail) green in CI on
  every push: realm configuration read-back, admin-created user with
  email verification, self-registration through the profile-first form
  and its required-action chain (account provably cannot authenticate
  before verification + password setup, and can after), password reset,
  toolkit OIDC logins, mobile PKCE, and a real Keycloak-issued mobile
  access token authenticating against the toolkit API.
- Android debug APK and iOS simulator builds green in CI on every push
  (debug artifacts only; no signing, no stores). App runtime flows on a
  device or emulator are not yet executed.
- OIDC login additionally verified end-to-end against a real maintained
  IdP (Authelia v4.39.28) locally; Keycloak-specific verification comes
  from the CI journey, not claimed from the Authelia result.
- The Dockerfile and compose file are statically written and reviewed; they
  have not been executed in the development sandbox (resource limits).
