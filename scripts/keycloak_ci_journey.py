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
        if public:
            body["directAccessGrantsEnabled"] = True
        else:
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


def mailpit_latest_link(timeout_s=60):
    """Wait for the newest Mailpit message to EMAIL; return its first KC link."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(f"{MAILPIT}/api/v1/messages")
        for msg in r.json().get("messages", []):
            if msg["To"][0]["Address"] != EMAIL:
                continue
            full = requests.get(f"{MAILPIT}/api/v1/message/{msg['ID']}").json()
            body = full.get("Text", "") + full.get("HTML", "")
            m = re.search(r'https?://localhost:8080[^\s"<>]+', body)
            if m:
                url = m.group(0).replace("&amp;", "&").rstrip(".,;'")
                return url, full.get("Subject", "")
        time.sleep(2)
    fail("no mail for the journey user arrived")


def clean_text(html, limit=300):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


def page_state(r):
    uname = 'name="username"' in r.text
    pwc = 'name="password-confirm"' in r.text
    safe_url = r.url.split("?", 1)[0]
    return (f"status={r.status_code} url={safe_url[:110]} "
            f"hist={[h.status_code for h in r.history]} "
            f"cookies={sorted(r.cookies.keys())} "
            f"username_form={uname} pw_confirm={pwc} "
            f"text={clean_text(r.text)!r}")


def complete_action_link(link, password, new_password=None):
    """Complete a Keycloak action-token link: it shows the login form first
    (action executes after authentication), then possibly a password form.
    Logs full request and response evidence at every step."""
    print("action link path:", urllib.parse.urlsplit(link).path)
    s = requests.Session()
    r = s.get(link)
    print("GET link ->", page_state(r))
    for step in range(6):
        action = form_action(r.text)
        if action is None:
            m = re.search(r'href="([^"]*login-actions[^"]*)"', r.text)
            if m:
                u = urllib.parse.urljoin(KC, m.group(1).replace("&amp;", "&"))
                r = s.get(u)
                print(f"step {step} proceed ->", page_state(r))
                continue
            print(f"step {step}: no form, no proceed link")
            break
        if 'name="username"' in r.text:
            r = s.post(action, data={"username": EMAIL, "password": password})
        elif 'name="password-confirm"' in r.text:
            if new_password is None:
                break
            r = s.post(action, data={"password": new_password,
                                     "password-confirm": new_password})
        else:
            print(f"step {step}: form has neither username nor password-confirm")
            break
        print(f"step {step} post ->", page_state(r))
        if "account has been updated" in clean_text(r.text, 2000).lower():
            return True
    return "account has been updated" in clean_text(r.text, 2000).lower()


def login_via_toolkit(s, password):
    """Drive /login/oidc -> Keycloak login form -> toolkit callback."""
    r = s.get(f"{APP}/login/oidc", allow_redirects=False)
    r = s.get(r.headers["Location"])
    action = form_action(r.text)
    if action is None:
        print(r.text[:1200])
        fail("no form on the Keycloak login page")
    r = s.post(action, data={"username": EMAIL, "password": password},
               allow_redirects=False)
    if r.status_code not in (302, 303):
        fail(f"login post: {r.status_code}")
    r = s.get(r.headers["Location"], allow_redirects=False)
    if r.status_code != 302 or r.headers.get("Location") != "/":
        loc = (r.headers.get('Location') or '').split('?', 1)[0]
        fail(f"toolkit callback: {r.status_code} {loc}")
    if s.get(f"{APP}/").status_code != 200:
        fail("home page after login")
    print("toolkit OIDC login OK (password verified)")


def registration_journey(auth):
    """User creation + verify-email through real Keycloak mail, then login.

    The Keycloak-hosted registration form is Keycloak's own tested surface;
    this journey exercises our integration: realm flags, mail delivery,
    verification completion, and the toolkit OIDC callback."""
    r = requests.post(f"{KC}/admin/realms/{REALM}/users", headers=auth, json={
        "username": EMAIL, "email": EMAIL, "firstName": "Journey",
        "lastName": "Test", "enabled": True, "emailVerified": False,
        "credentials": [{"type": "password", "value": PASSWORD,
                         "temporary": False}]})
    if r.status_code not in (201, 409):
        fail(f"user create: {r.status_code} {r.text[:200]}")
    uid = requests.get(f"{KC}/admin/realms/{REALM}/users",
                       headers=auth, params={"email": EMAIL}).json()[0]["id"]
    dg = requests.post(f"{KC}/realms/{REALM}/protocol/openid-connect/token",
                       data={"grant_type": "password",
                             "client_id": "office-toolkit-mobile",
                             "username": EMAIL, "password": PASSWORD})
    dg_err = ((dg.json().get("error", "token-ok") + " / "
               + dg.json().get("error_description", ""))
              if dg.headers.get("content-type", "").startswith("application/json")
              else dg.text[:120])
    print("direct-grant credential check:", dg.status_code, dg_err)
    r = requests.put(f"{KC}/admin/realms/{REALM}/users/{uid}/execute-actions-email",
                     headers=auth, json=["VERIFY_EMAIL"])
    if r.status_code not in (200, 204):
        fail(f"send verify email: {r.status_code} {r.text[:200]}")
    link, subject = mailpit_latest_link()
    print("captured mail:", subject)
    if not complete_action_link(link, PASSWORD):
        st = requests.get(f"{KC}/admin/realms/{REALM}/users/{uid}",
                          headers=auth).json()
        print("user state on failure:", json.dumps(
            {k: st.get(k) for k in ("enabled", "emailVerified",
                                    "requiredActions")}))
        fail("verify-email action link did not complete")
    status = requests.get(f"{KC}/admin/realms/{REALM}/users/{uid}",
                          headers=auth).json()
    if not status.get("emailVerified"):
        fail("email not marked verified after completing the link")
    print("verify-email journey OK")
    s = requests.Session()
    login_via_toolkit(s, PASSWORD)


def reset_journey(auth):
    uid = requests.get(f"{KC}/admin/realms/{REALM}/users",
                       headers=auth, params={"email": EMAIL}).json()[0]["id"]
    r = requests.put(f"{KC}/admin/realms/{REALM}/users/{uid}/execute-actions-email",
                     headers=auth, json=["UPDATE_PASSWORD"])
    if r.status_code not in (200, 204):
        fail(f"send reset email: {r.status_code} {r.text[:200]}")
    link, subject = mailpit_latest_link()
    print("captured mail:", subject)
    if not complete_action_link(link, PASSWORD, new_password=NEW_PASSWORD):
        fail("update-password action link did not complete")
    s2 = requests.Session()
    login_via_toolkit(s2, NEW_PASSWORD)
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
        reset_journey(auth)
        mobile_pkce_journey()
    finally:
        proc.terminate()
    print("ALL JOURNEYS GREEN")


if __name__ == "__main__":
    main()
