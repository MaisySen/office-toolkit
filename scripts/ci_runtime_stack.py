"""Bring up the CI runtime stack state for native runtime jobs: realm +
clients (same shape as the journey), one verified user, and the toolkit
Flask app on port 5000 (the mobile app's default apiBaseUrl). Keycloak
(start-dev, localhost:8080) and Mailpit are started by the workflow
before this script runs."""
import os
import subprocess
import sys
import time

import requests

KC = os.environ.get("KC_BASE", "http://localhost:8080")
# Issuer value embedded in tokens and expected by the toolkit API; in the
# Android job this is the emulator's host alias (mapped on the runner).
PUBLIC_ISSUER = os.environ.get(
    "PUBLIC_ISSUER", f"{KC}/realms/office-toolkit")
REALM = "office-toolkit"
EMAIL = "runtime.user@example.com"
PASSWORD = "runtime-pass-123"


def fail(msg):
    print(f"STACK-FAIL: {msg}")
    sys.exit(1)


def main():
    last = "no attempt completed"
    for _ in range(30):
        try:
            r = requests.post(
                f"{KC}/realms/master/protocol/openid-connect/token",
                data={"grant_type": "password", "client_id": "admin-cli",
                      "username": "admin", "password": "admin"},
                timeout=5)
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code == 200:
                break
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2)
    else:
        fail(f"admin token grant against {KC} never succeeded; "
             f"last result: {last}")
    auth = {"Authorization": "Bearer " + r.json()["access_token"]}

    r = requests.post(f"{KC}/admin/realms", headers=auth, timeout=10, json={
        "realm": REALM, "enabled": True,
        "registrationAllowed": True, "verifyEmail": True,
        "resetPasswordAllowed": True, "loginWithEmailAllowed": True,
        "smtpServer": {"host": "localhost", "port": "1025",
                       "from": "runtime@example.com"}})
    if r.status_code not in (201, 409):
        fail(f"realm create: {r.status_code} {r.text[:200]}")
    for cid, public, redirects in [
            ("office-toolkit-web", False,
             ["http://localhost:5000/login/oidc/callback"]),
            ("office-toolkit-mobile", True,
             ["officetoolkit://auth", "http://localhost:8081/auth"])]:
        body = {"clientId": cid, "enabled": True, "publicClient": public,
                "standardFlowEnabled": True, "redirectUris": redirects,
                "attributes": {"pkce.code.challenge.method": "S256"}}
        if public:
            body["directAccessGrantsEnabled"] = True
        else:
            body["secret"] = "ci-web-secret"
        r = requests.post(f"{KC}/admin/realms/{REALM}/clients",
                          headers=auth, timeout=10, json=body)
        if r.status_code not in (201, 409):
            fail(f"client {cid}: {r.status_code} {r.text[:200]}")
    cid = requests.get(f"{KC}/admin/realms/{REALM}/clients", headers=auth,
                       params={"clientId": "office-toolkit-mobile"},
                       timeout=10).json()[0]["id"]
    requests.post(
        f"{KC}/admin/realms/{REALM}/clients/{cid}/protocol-mappers/models",
        headers=auth, timeout=10, json={
            "name": "office-toolkit-api-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {"included.custom.audience": "office-toolkit-api",
                       "id.token.claim": "false",
                       "access.token.claim": "true"}})

    r = requests.post(f"{KC}/admin/realms/{REALM}/users", headers=auth,
                      timeout=10, json={
        "username": EMAIL, "email": EMAIL, "enabled": True,
        "emailVerified": True, "firstName": "Runtime", "lastName": "User",
        "credentials": [{"type": "password", "value": PASSWORD,
                         "temporary": False}]})
    if r.status_code not in (201, 409):
        fail(f"user create: {r.status_code} {r.text[:200]}")

    # The issuer the app will see must equal the pinned value: check the
    # discovery document AND a real minted token, both fetched through
    # the app-facing host (10.0.2.2 on Android, localhost on iOS).
    disc = requests.get(
        f"{PUBLIC_ISSUER}/.well-known/openid-configuration",
        timeout=10).json()
    if disc.get("issuer") != PUBLIC_ISSUER:
        fail(f"discovery issuer {disc.get('issuer')!r} != pinned "
             f"{PUBLIC_ISSUER!r}")
    r = requests.post(
        f"{PUBLIC_ISSUER}/protocol/openid-connect/token", timeout=10,
        data={"grant_type": "password",
              "client_id": "office-toolkit-mobile",
              "username": EMAIL, "password": PASSWORD})
    if r.status_code != 200:
        fail(f"issuer-check token grant: {r.status_code} {r.text[:200]}")
    import base64
    import json as _json
    seg = r.json()["access_token"].split(".")[1]
    seg += "=" * (-len(seg) % 4)
    claims = _json.loads(base64.urlsafe_b64decode(seg))
    if claims.get("iss") != PUBLIC_ISSUER:
        fail(f"minted token iss {claims.get('iss')!r} != pinned "
             f"{PUBLIC_ISSUER!r}")
    print(f"ISSUER OK: discovery and minted token both pin to "
          f"{PUBLIC_ISSUER}")

    if os.path.exists("/tmp/ci-runtime-auth.db"):
        os.unlink("/tmp/ci-runtime-auth.db")
    env = dict(os.environ,
               TOOLKIT_OIDC_ISSUER=PUBLIC_ISSUER,
               TOOLKIT_OIDC_CLIENT_ID="office-toolkit-web",
               TOOLKIT_OIDC_CLIENT_SECRET="ci-web-secret",
               TOOLKIT_AUTH_DB="/tmp/ci-runtime-auth.db",
               TOOLKIT_SECRET_KEY="ci-runtime-key")
    subprocess.Popen(
        [sys.executable, "-m", "flask", "--app", "office_toolkit.web.app",
         "run", "--port", "5000", "--no-reload"], env=env,
        stdout=open("/tmp/flask.log", "w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    for _ in range(30):
        try:
            requests.get("http://localhost:5000/login", timeout=1)
            break
        except requests.RequestException:
            time.sleep(1)
    else:
        fail("toolkit app never came up on :5000")
    print("STACK READY: realm, clients, verified runtime user, toolkit :5000")


if __name__ == "__main__":
    main()
