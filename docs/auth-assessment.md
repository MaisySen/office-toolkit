# Auth assessment: bespoke provider vs maintained self-hosted IdP

Scope: private office toolkit for a small team. Web app (Flask session
cookies) plus a mobile app that signs in with OAuth 2.0 authorization code +
PKCE (RFC 7636) against the toolkit's own provider.

## Threat model (what we actually defend)

| Threat | Control in current bespoke stack | Status |
| --- | --- | --- |
| CSRF on state-changing web routes | Synchronizer token per session, enforced on /run/* | Done |
| Login CSRF (attacker logs victim into attacker's account) | Same CSRF token enforced on /login and /register; SameSite=Lax cookie | Done |
| Session fixation | Full session clear + new server-side session id on login | Done |
| Logout not invalidating server state | Server-side web_sessions table; logout drops the row | Done |
| Cross-account file/output access | Per-user job ownership marker (.owner); downloads checked against owner | Done |
| Token scope/audience/client confusion | Access and refresh tokens carry client_id; refresh bound to issuing client | Done |
| Refresh token replay | Rotation with per-client families; reuse across clients revokes the family | Done |
| Redirect/deep-link interception (mobile) | PKCE S256; custom URI scheme noted as residual risk, HTTPS app links recommended when a domain exists | Partial |
| Brute force on login | Per-account lockout after repeated failures | Done |
| Bootstrap account takeover (first-run registration open on a public URL) | Registration closed unless TOOLKIT_BOOTSTRAP_TOKEN is set; token required for the first account; never exposed unauthenticated to a public network | Done |
| Password storage | argon2id | Done |
| Long-lived or abandoned web sessions | Absolute session age (TOOLKIT_SESSION_MAX_AGE, 12 hours by default); expired sessions, codes, tokens and OIDC states purged on each sign in | Done |
| Logout CSRF (forced sign out from another site) | Logout is a POST carrying the synchronizer token; GET only shows a confirm button | Done |
| JWKS fetch amplification from garbage bearer tokens | Tokens screened (shape, length, RS256 header, kid) before any key work; forced JWKS refetch at most once a minute per provider | Done |
| Development redirect URIs or endpoints in production | Release builds refuse http or loopback endpoints in app.config.js; a production server refuses loopback or http mobile redirect URIs | Done |
| CI password grant client reaching a real realm | CI seeding scripts run only in CI against loopback stacks (scripts/ci_guard.py); production provisioning turns the password grant off and verifies it on read back | Done |

Residual risks: rate limiting is in-memory per process (not shared across
workers); mobile deep-link interception needs claimed HTTPS redirect URIs
once a public domain exists; no MFA yet; SQLite session store is single-node.

## Options surveyed (licences verified from project LICENSE files, 2026-10-29)

| Option | Licence (verified) | Fit for this toolkit |
| --- | --- | --- |
| Keycloak | Apache-2.0 (raw.githubusercontent.com/keycloak/keycloak/main/LICENSE.txt) | Mature IdP, OIDC/SAML/LDAP, MFA, admin UI. Heavy JVM service; right answer if we ever need SSO, federation or more than a handful of users. |
| Ory Kratos + Hydra | Apache-2.0 both (kratos and hydra LICENSE files) | Cloud-native identity (Kratos) and OAuth2/OIDC server (Hydra). Two services to run; strong fit for API-first products, more moving parts than we need now. |
| Authelia | Apache-2.0 (authelia LICENSE file) | Forward-auth companion for reverse proxies; web SSO + 2FA. Does not issue tokens for a mobile PKCE flow on its own. |
| Supabase Auth (gotrue) | MIT (supabase/gotrue LICENSE) | Self-hostable, but expects the Postgres/PostgREST stack around it; adopting it is adopting a platform, not a library. Hosted Supabase also means vendor terms, which are an owner gate. |
| authentik | MIT core, enterprise/ directory under a separate licence, website under CC BY-SA 4.0 (goauthentik/authentik LICENSE) | Full IdP with strong UI; licence split means checking which features live under enterprise/ before relying on them. |
| Zitadel | AGPL-3.0 since March 2025 (zitadel.com/blog/apache-to-agpl) | AGPL is exactly the copyleft class we removed from the runtime when we dropped Ghostscript. Excluded on licence grounds. |

## Recommendation

Use the current bespoke provider only for private, internal deployment, and
do not present it as production-grade for arbitrary internet exposure. Before
any production or public deployment, adopt a maintained IdP (Keycloak,
Apache-2.0, is the recommended candidate) rather than shipping the custom
provider, or commission an independent security review of it. A self-written
provider is not made safe by a small user count: the threats in the table
above do not scale with headcount. Rationale for keeping it meanwhile:

1. The user count is a handful of known people in one team; the bespoke
   provider's attack surface is small and fully covered by our tests.
2. Every maintained self-hosted option adds at least one long-running
   service (JVM/Go/Postgres) with its own patching duty; at this scale that
   is more operational risk than it removes.
3. "Self-hosted" is not "zero terms": each option still carries licence
   obligations (all Apache-2.0/MIT above are permissive; authentik has an
   enterprise split; Zitadel is AGPL) and its own security-update cadence.
4. The mobile app uses OAuth 2.0 authorization code + PKCE (not full OIDC:
   no discovery document, id_token or nonce handling yet). Migrating to
   Keycloak or Ory means pointing the same flow at the IdP's endpoints and
   adding OIDC discovery/id_token validation on the app side, which
   expo-auth-session supports. That is a contained change, not a rewrite.

Trigger points to retire the bespoke provider: any production or public
exposure, any SSO or MFA requirement, or a second organisation. At any of
those, adopt Keycloak (Apache-2.0) as the IdP and retire the bespoke
provider rather than extending it.

## Update: web SSO relying party implemented (v0.2.0 work)

The web UI can now delegate sign-in to a maintained external IdP over OIDC
(TOOLKIT_OIDC_* env vars; see README). The relying-party side is built on
Authlib (BSD-3-Clause, maintained), not a bespoke token parser: Authlib
fetches the discovery document and JWKS, and validates the ID token
signature, issuer, audience/authorized party, expiry and nonce. PKCE S256
is enforced on the authorization request, state and nonce are bound to the
browser session by Authlib's Flask integration, and the callback issues a
fresh server-side session (fixation-safe).

Deliberate choices:

- SSO accounts are keyed by email per issuer and never silently linked onto
  an existing password account with the same email, so a misconfigured or
  hostile provider cannot take over a local account. Linking stays manual.
- SSO-only accounts cannot sign in with a password.
- The built-in provider remains the default; SSO activates only when an
  issuer is configured. Mobile still uses the built-in OAuth2+PKCE flow.
- The hand-rolled verification module drafted earlier in this branch was
  replaced by Authlib before merge, on review feedback that maintained RP
  code beats no-new-dependency bespoke security code.

Verified end-to-end against a real maintained IdP: Authelia v4.39.28
(Apache-2.0) running locally with TLS, a file-backed user and a public
PKCE-S256 client. The scripted flow (scripts/oidc_e2e_authelia.py) drove the
real browser path: discovery, authorize redirect, portal login, code
exchange, JWKS signature verification and login. Authelia serves the email
claim from userinfo rather than the ID token, so the app falls back to the
userinfo endpoint and enforces the spec-required subject match; both
behaviours are covered by tests (15 OIDC tests: happy paths plus wrong
signature, wrong audience, wrong issuer, expired token, nonce mismatch,
forged state, state replay, unverified email, no silent linking, subject
continuity across email change, email-reassignment refusal, missing
subject, userinfo fallback and userinfo subject mismatch).

Accounts are keyed by issuer + subject; email is an updatable attribute, so
a provider reassigning an email cannot hand an existing account to a
different subject.

Not yet done: MFA, mobile OIDC discovery, and running against a second IdP
(Keycloak) for portability evidence.
