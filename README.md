# Office Toolkit

A local-first web app for everyday office PDF work. Files are processed on the
machine running the app; nothing is sent to third-party services.

## Status

Early build. Core operations are implemented and covered by automated tests
(`python3 -m pytest`). It has been exercised on small generated sample files,
not yet on large real-world documents.

Privacy behaviour of the web app: the browser uploads files to the server
running this app; processing happens on that server (OCR and Office
conversion run as local subprocesses there). Temporary working copies live in a
per-job directory. Deletion is targeted at 30 minutes after creation: a
per-job timer plus a sweep on each new job and at process start (which also
recovers orphans left by a crash). This is a best-effort target, not a hard
guarantee - a killed process defers deletion to the next start or request.
No analytics, no third-party upload destinations.

## What it does today

- Merge, split/extract, and rotate PDF pages
- Compress PDFs
- Remove document metadata
- Remove a PDF password **when you know the password** (it does not crack passwords)
- Watermark PDFs with text and/or an image; watermark plain images
- Convert PDF pages to JPEG
- Permanent redaction: finds exact text in the text layer and destroys it.
  Affected pages are rebuilt as images so the removed text cannot be
  recovered; annotations on those pages are dropped and metadata is scrubbed.
  The output is verified - if the target text survives, the operation fails
  instead of succeeding silently. Limits: scanned documents need OCR first,
  and embedded file attachments are not searched. Tradeoff: affected pages
  lose selectable text and get larger.
- Convert Office files (DOCX, XLSX, PPTX, ODT) to PDF via LibreOffice.
  Layout is close but not a perfect match for Microsoft Office.
- OCR: add a searchable text layer to scanned PDFs (Tesseract, installed
  languages only). Pages are rebuilt from renders, so vector content becomes
  an image.
- Replace short literal text in simple PDFs (no paragraph reflow)

## Auth

Self-hosted by default: no third-party identity service required. Accounts
in local SQLite, argon2id password hashing, session cookie for the web UI,
and standard OAuth2 authorization-code + PKCE (S256) for the mobile app
with rotating refresh tokens. First registered account bootstraps the
install; later registration is closed unless TOOLKIT_REGISTRATION_OPEN=1.

Optional SSO: the web UI can delegate sign-in to a maintained external
identity provider over OpenID Connect (authorization-code flow with PKCE,
ID token verified by Authlib against the provider's JWKS). Set:

    TOOLKIT_OIDC_ISSUER         e.g. https://idp.example.com/realms/main
    TOOLKIT_OIDC_CLIENT_ID      client id registered at the provider
    TOOLKIT_OIDC_CLIENT_SECRET  optional; public clients use PKCE only

Register <app-url>/login/oidc/callback as the redirect URI at the provider.
SSO logins map to local accounts by email; a pre-existing password account
with the same email is never linked silently.

The mobile app talks to the same provider directly (public client with
PKCE, client id in TOOLKIT_MOBILE_CLIENT_ID, default office-toolkit-mobile)
and calls the API with the provider's access tokens. The API accepts such
a token only when its issuer is exactly the configured TOOLKIT_OIDC_ISSUER,
its audience contains TOOLKIT_OIDC_AUDIENCE (default office-toolkit-api,
independent of the authorized party), the authorized party is the mobile
client, it is unexpired, and its email is verified. At the provider, add
an audience mapper putting the API audience into the mobile client's
access tokens (Keycloak: oidc-audience-mapper, included client audience =
the same value as TOOLKIT_OIDC_AUDIENCE).

## Run

    pip install -r requirements.txt
    # system packages needed: libreoffice, tesseract-ocr, ocrmypdf
    FLASK_APP=office_toolkit.web.app flask run

Then open http://127.0.0.1:5000

## Test

    python3 -m pytest

## Licence

Apache-2.0, copyright Bidur Khatri; see LICENSE and NOTICE. The runtime path
contains no AGPL component: OCR calls Tesseract directly after the earlier
OCRmyPDF/Ghostscript design was removed. See `docs/dependencies.md`.
Reintroducing any copyleft dependency is part of the owner's licence
decision.
