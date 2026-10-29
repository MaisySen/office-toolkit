# Office Toolkit mobile (Expo)

Status: exploratory scaffold, pre-decision. Talks to the backend JSON API
(`/api/operations`, `/api/run/<slug>`). Token storage is wired through
expo-secure-store but no sign-in flow exists yet: the auth stack (OAuth2 + PKCE
provider, authorization code + PKCE via expo-auth-session) is an open
decision recorded in ../docs/research-mobile-auth.md.

    cd mobile
    npm install
    npm run typecheck
    npm start

Bundle ID / package: au.fintex.officetoolkit (per org convention au.fintex.<name>).
