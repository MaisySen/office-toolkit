"""Full identity journey against a real Keycloak (started by the CI job),
with email captured by Mailpit. Covers: realm/client setup, registration
with email verification, login, password reset, and the mobile PKCE flow.
Fails loudly on any broken step. No external mail leaves the runner."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import requests

KC = "http://localhost:8080"
MAILPIT = "http://localhost:8025"
REALM = "office-toolkit"
APP = "http://localhost:5050"
EMAIL = "journey.test@example.com"
PASSWORD = "correct horse battery"
NEW_PASSWORD = "a fresh password 99"


def fail(msg):
    print("JOURNEY-FAIL:", msg)
    sys.exit(1)


def admin_token():
    r = requests.post(f"{KC}/realms/master/protocol/openid-connect/token",
                      data={"grant_type": "password", "client_id": "admin-cli",
                            "username": os.environ["KC_ADMIN"],
                            "password": os.environ["KC_ADMIN_PASSWORD"]})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def setup_realm(auth):
    r = requests.post(f"{KC}/admin/realms", headers=auth, json={
        "realm": REALM, "enabled": True,
        "registrationAllowed": True, "verifyEmail": True,
        "resetPasswordAllowed": True, "loginWithEmailAllowed": True,
        "smtpServer": {"host": "localhost", "port": "1025",
                       "from": "journey@example.com",
                       "fromDisplayName": "Toolkit CI"}})
    if r.status_code not in (201, 409):
        fail(f"realm create: {r.status_code} {r.text[:200]}")
    for cid, public, redirects in [
            ("office-toolkit-web", False, [APP + "/login/oidc/callback"]),
            ("office-toolkit-mobile", True,
             ["officetoolkit://auth", "http://localhost:8081/auth"])]:
        body = {"clientId": cid, "enabled": True, "publicClient": public,
                "standardFlowEnabled": True, "redirectUris": redirects,
                "attributes": {"pkce.code.challenge.method": "S256"}}
        if not public:
            body["secret"] = "ci-web-secret"
        r = requests.post(f"{KC}/admin/realms/{REALM}/clients",
                          headers=auth, json=body)
        if r.status_code not in (201, 409):
            fail(f"client {cid}: {r.status_code} {r.text[:200]}")
    print("realm + clients ready")


def form_action(html, base=KC):
    m = re.search(r"<form[^>]+action=[\']([^\']+)[\']", html)
    if not m:
        m = re.search(r'<form[^>]+action="([^"]+)"', html)
    if not m:
        return None
    return urllib.parse.urljoin(base, m.group(1).replace("&amp;", "&"))


def mailpit_link(marker, since=0):
    """Wait for a Mailpit message whose text contains marker; return first link."""
    for _ in range(30):
        r = requests.get(f"{MAILPIT}/api/v1/messages")
        for msg in r.json().get("messages", []):
            if msg["To"][0]["Address"] != EMAIL:
                continue
            full = requests.get(f"{MAILPIT}/api/v1/message/{msg['ID']}").json()
            body = full.get("Text", "") + full.get("HTML", "")
            if marker in body:
                m = re.search(r'https?://localhost:8080[^\s"<>]+', body)
                if m:
                    return m.group(0).replace("&amp;", "&")
        time.sleep(2)
    fail(f"no mail containing {marker!r} arrived")


def registration_journey(auth):
    s = requests.Session()
    r = s.get(f"{APP}/login/oidc", allow_redirects=False)
    kc_login = r.headers["Location"]
    r = s.get(kc_login)
    reg = re.search(r'href="([^"]*registration[^"]*)"', r.text)
    if not reg:
        fail("no registration link on the Keycloak login page")
    r = s.get(urllib.parse.urljoin(KC, reg.group(1).replace("&amp;", "&")))
    action = form_action(r.text)
    if action is None:
        print("registration page status:", r.status_code, r.url)
        print(r.text[:1500])
        fail("no form on the registration page")
    r = s.post(action, data={
        "email": EMAIL, "firstName": "Journey", "lastName": "Test",
        "password": PASSWORD, "password-confirm": PASSWORD})
    if r.status_code not in (200, 302):
        fail(f"registration post: {r.status_code}")
    link = mailpit_link("Verify email")
    if not link:
        link = mailpit_link("verify")
    s.get(link)  # completes verification
    # now log in through the same OIDC start
    r = s.get(f"{APP}/login/oidc", allow_redirects=False)
    r = s.get(r.headers["Location"])
    action = form_action(r.text)
    r = s.post(action, data={"username": EMAIL, "password": PASSWORD},
               allow_redirects=False)
    if r.status_code not in (302, 303):
        fail(f"login post: {r.status_code}")
    cb = r.headers["Location"]
    r = s.get(cb, allow_redirects=False)
    if r.status_code != 302 or r.headers.get("Location") != "/":
        fail(f"toolkit callback: {r.status_code} {r.headers.get('Location')}")
    r = s.get(f"{APP}/")
    if r.status_code != 200:
        fail("home page after registration journey")
    print("registration + verify-email + login journey OK")


def reset_journey():
    s = requests.Session()
    r = s.get(f"{APP}/login/oidc", allow_redirects=False)
    r = s.get(r.headers["Location"])
    fp = re.search(r'href="([^"]*reset-credentials[^"]*)"', r.text)
    if not fp:
        fail("no forgot-password link")
    r = s.get(urllib.parse.urljoin(KC, fp.group(1).replace("&amp;", "&")))
    action = form_action(r.text)
    s.post(action, data={"username": EMAIL})
    link = mailpit_link("reset")
    r = s.get(link)
    action = form_action(r.text)
    r = s.post(action, data={"password": NEW_PASSWORD,
                             "password-confirm": NEW_PASSWORD},
               allow_redirects=False)
    print("password reset journey OK")


def mobile_pkce_journey():
    import base64, hashlib, secrets
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    s = requests.Session()
    params = {"response_type": "code", "client_id": "office-toolkit-mobile",
              "redirect_uri": "http://localhost:8081/auth",
              "scope": "openid email profile",
              "code_challenge": challenge, "code_challenge_method": "S256"}
    r = s.get(f"{KC}/realms/{REALM}/protocol/openid-connect/auth",
              params=params)
    action = form_action(r.text)
    r = s.post(action, data={"username": EMAIL, "password": NEW_PASSWORD},
               allow_redirects=False)
    if r.status_code not in (302, 303) or "code=" not in r.headers.get("Location", ""):
        fail(f"mobile authorize: {r.status_code}")
    code = urllib.parse.parse_qs(
        urllib.parse.urlparse(r.headers["Location"]).query)["code"][0]
    r = requests.post(f"{KC}/realms/{REALM}/protocol/openid-connect/token",
                      data={"grant_type": "authorization_code", "code": code,
                            "client_id": "office-toolkit-mobile",
                            "redirect_uri": "http://localhost:8081/auth",
                            "code_verifier": verifier})
    if r.status_code != 200 or "access_token" not in r.json():
        fail(f"mobile token exchange: {r.status_code} {r.text[:200]}")
    print("mobile PKCE journey OK")


def main():
    auth = admin_token()
    setup_realm(auth)
    # toolkit with Keycloak as issuer
    env = dict(os.environ,
               TOOLKIT_OIDC_ISSUER=f"{KC}/realms/{REALM}",
               TOOLKIT_OIDC_CLIENT_ID="office-toolkit-web",
               TOOLKIT_OIDC_CLIENT_SECRET="ci-web-secret",
               TOOLKIT_AUTH_DB="/tmp/ci-journey-auth.db",
               TOOLKIT_SECRET_KEY="ci-journey-key")
    if os.path.exists("/tmp/ci-journey-auth.db"):
        os.unlink("/tmp/ci-journey-auth.db")
    proc = subprocess.Popen(
        [sys.executable, "-m", "flask", "--app", "office_toolkit.web.app",
         "run", "--port", "5050", "--no-reload"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                requests.get(f"{APP}/login", timeout=1)
                break
            except requests.RequestException:
                time.sleep(1)
        registration_journey(auth)
        reset_journey()
        mobile_pkce_journey()
    finally:
        proc.terminate()
    print("ALL JOURNEYS GREEN")


if __name__ == "__main__":
    main()
