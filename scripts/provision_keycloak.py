"""Provision the production Keycloak realm for doctool.multies.si.

Runs ON the host after install.sh, with the keycloak.env values in the
environment (source /etc/office-toolkit/keycloak.env first). Mirrors
scripts/ci_runtime_stack.py; differs only in base URLs, redirect URIs,
secrets and the SMTP sender, which all come from the environment so no
real value ever enters git.

Required env: KC_ADMIN, KC_ADMIN_PASSWORD, WEB_CLIENT_SECRET,
SMTP_HOST, SMTP_PORT, SMTP_FROM. Optional: SMTP_USER, SMTP_PASSWORD
(only when the sender service authenticates), KC_BASE (default
http://127.0.0.1:8080), PUBLIC_BASE (default
https://doctool.multies.si).
"""
import base64
import json
import os
import sys
import time

import requests

KC = os.environ.get("KC_BASE", "http://127.0.0.1:8080")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://doctool.multies.si")
ISSUER = f"{PUBLIC_BASE}/auth/realms/office-toolkit"
REALM = "office-toolkit"


def need(name):
    v = os.environ.get(name)
    if not v:
        print(f"STACK-FAIL: {name} is not set")
        sys.exit(1)
    return v


def fail(msg):
    print(f"STACK-FAIL: {msg}")
    sys.exit(1)


def main():
    admin = need("KC_ADMIN")
    admin_pw = need("KC_ADMIN_PASSWORD")
    web_secret = need("WEB_CLIENT_SECRET")
    smtp = {"host": need("SMTP_HOST"), "port": need("SMTP_PORT"),
            "from": need("SMTP_FROM")}
    if os.environ.get("SMTP_USER"):
        smtp.update({"user": os.environ["SMTP_USER"],
                     "password": need("SMTP_PASSWORD"), "auth": "true"})

    last = "no attempt completed"
    for _ in range(30):
        try:
            r = requests.post(
                f"{KC}/realms/master/protocol/openid-connect/token",
                data={"grant_type": "password", "client_id": "admin-cli",
                      "username": admin, "password": admin_pw},
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

    r = requests.post(f"{KC}/admin/realms", headers=auth, timeout=10,
                      json={"realm": REALM, "enabled": True,
                            "registrationAllowed": True,
                            "verifyEmail": True,
                            "resetPasswordAllowed": True,
                            "loginWithEmailAllowed": True,
                            "smtpServer": smtp})
    if r.status_code not in (201, 409):
        fail(f"realm create: {r.status_code} {r.text[:200]}")

    for cid, public, redirects in [
            ("office-toolkit-web", False,
             [f"{PUBLIC_BASE}/login/oidc/callback"]),
            ("office-toolkit-mobile", True,
             ["officetoolkit://auth"])]:
        body = {"clientId": cid, "enabled": True, "publicClient": public,
                "standardFlowEnabled": True, "redirectUris": redirects,
                "attributes": {"pkce.code.challenge.method": "S256"}}
        if not public:
            body["secret"] = web_secret
            # Lets the issuer check below mint a token without creating
            # a throwaway human user in the production realm.
            body["serviceAccountsEnabled"] = True
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

    # The issuer the apps see must equal the pinned public value: check
    # the discovery document AND a real token end to end through Caddy.
    disc = requests.get(f"{ISSUER}/.well-known/openid-configuration",
                        timeout=10).json()
    if disc.get("issuer") != ISSUER:
        fail(f"discovery issuer {disc.get('issuer')!r} != pinned {ISSUER!r}")
    r = requests.post(f"{ISSUER}/protocol/openid-connect/token",
                      timeout=10,
                      data={"grant_type": "client_credentials",
                            "client_id": "office-toolkit-web",
                            "client_secret": web_secret})
    if r.status_code != 200:
        fail(f"issuer-check token grant: {r.status_code} {r.text[:200]}")
    seg = r.json()["access_token"].split(".")[1]
    seg += "=" * (-len(seg) % 4)
    claims = json.loads(base64.urlsafe_b64decode(seg))
    if claims.get("iss") != ISSUER:
        fail(f"minted token iss {claims.get('iss')!r} != pinned {ISSUER!r}")
    print(f"ISSUER OK: discovery and minted token both pin to {ISSUER}")


if __name__ == "__main__":
    main()
