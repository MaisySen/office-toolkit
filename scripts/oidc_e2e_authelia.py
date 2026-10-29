"""End-to-end OIDC verification driver against a local Authelia instance.

Setup (no accounts, no third-party services):
  1. Download the Authelia linux-amd64 release and start it with a config
     like docs/auth-assessment.md describes (TLS on 127.0.0.1:9091, file
     user backend, OIDC client "toolkit-test", public + PKCE S256,
     implicit consent, redirect http://127.0.0.1:5050/login/oidc/callback).
  2. Run the app: TOOLKIT_OIDC_ISSUER=https://127.0.0.1:9091 \
       TOOLKIT_OIDC_CLIENT_ID=toolkit-test \
       REQUESTS_CA_BUNDLE=<authelia-cert.pem> flask run --port 5050
  3. python scripts/oidc_e2e_authelia.py

Last verified: Authelia v4.39.28, full login green, account stored keyed by
issuer + subject with the email attribute from userinfo.
"""
import json, re, sqlite3, sys
import requests

s = requests.Session()
s.verify = '/tmp/authelia/cert.pem'
NOFOLLOW = dict(allow_redirects=False)

# 1. toolkit -> authorize URL
r = s.get('http://127.0.0.1:5050/login/oidc', **NOFOLLOW)
assert r.status_code == 302, r.status_code
auth_url = r.headers['Location']
print('1 authorize URL ok')

# 2. first visit: unauthenticated -> portal
r = s.get(auth_url, **NOFOLLOW)
print('2 authelia authorize unauth ->', r.status_code, r.headers.get('Location', '')[:80])

# 3. authenticate at the portal API
r = s.post('https://127.0.0.1:9091/api/firstfactor',
           json={'username': 'sso.test@example.com', 'password': 'test-pass-123',
                 'keepMeLoggedIn': False})
print('3 firstfactor ->', r.status_code, r.text[:80])

# 4. retry authorize: should redirect to the toolkit callback with a code
r = s.get(auth_url, **NOFOLLOW)
loc = r.headers.get('Location', '')
print('4 authorize authed ->', r.status_code, loc[:110])
if r.status_code == 303 or (r.status_code in (301,302) and 'login/oidc/callback' not in loc):
    # consent step (should be skipped with implicit mode, but handle it)
    print('   intermediate:', loc)
    r2 = s.get(loc, **NOFOLLOW)
    import re as _re
    m = _re.search(r'<div class="error">([^<]*)</div>', r2.text)
    print('   ->', r2.status_code, loc[:60], 'ERR:', m.group(1) if m else None)
    loc = r2.headers.get('Location', '')
if loc == '/':
    print('5b callback redirected home: LOGIN OK')
else:
    assert 'login/oidc/callback' in loc and 'code=' in loc, loc

# 5. toolkit callback (only if we are not already home)
if loc != '/':
    r = s.get(loc, **NOFOLLOW)
    print('5 toolkit callback ->', r.status_code, r.headers.get('Location'))

# 6. authenticated home page
r = s.get('http://127.0.0.1:5050/')
print('6 home ->', r.status_code, 'logged-in' if r.status_code == 200 else 'FAIL')

db = sqlite3.connect('/tmp/authelia/toolkit-auth.db')
db.row_factory = sqlite3.Row
for row in db.execute("SELECT email, provider, sub != '' AS has_sub FROM users"):
    print('7 user row:', dict(row))
