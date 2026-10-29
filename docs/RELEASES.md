# Releases

Per org convention: every build or publish recorded here - number, time,
commit, CI run, what changed. Nothing shipped yet.

| Date | What | Commit | Notes |
| --- | --- | --- | --- |
| 2026-10-29 12:10 AEDT | Published v0.1.0 to MaisySen/office-toolkit (public) | 06d6f6c + fixes fde897d, 88b2d7e | Approved by owner 11:59; deploy key write; CI running |

## v0.1.0 (candidate, commit 06d6f6c)

First consolidated release: PDF toolkit (merge, split, rotate, compress,
metadata scrub, password remove/add, watermark, PDF to JPEG, images to PDF,
permanent redaction, OCR via Tesseract, PDF to DOCX text, DOCX/XLSX to PDF),
Flask web app with hardened self-hosted auth (OAuth2 + PKCE for mobile,
session cookies for web), JSON API, Expo mobile app.

Verification at this commit:
- Python suite: 72/72 passed (pdfops 30, web 21, auth 21 - run pytest for
  the per-test list).
- Mobile: jest-expo smoke test 1/1 (App mounts, sign-in gate renders);
  typecheck clean; Android and iOS Hermes bundles export successfully.
- Real documents: 14/14 operations on IRS W-4 (AcroForm) + W3C sample PDF;
  DOCX and XLSX to PDF conversion verified visually.

Known limits: no on-device mobile runtime yet; bespoke auth is for private
deployment only (Keycloak or external review before production exposure);
30-minute file deletion is a target, not a guarantee.
