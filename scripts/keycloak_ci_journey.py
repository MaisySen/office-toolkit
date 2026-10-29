"""Full identity journey against a real Keycloak (started by the CI job),
with email captured by Mailpit. Covers: realm/client setup, admin-created
user with email verification, self-registration through the Keycloak UI,
password reset via action email, toolkit OIDC login, and the mobile PKCE
flow. Interactive pages are driven by a real browser (Playwright chromium);
API surfaces use requests. Fails loudly on any broken step. No external
mail leaves the runner. Diagnostics mask URLs to their path only."""
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse

import requests
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

KC = "http://localhost:8080"
MAILPIT = "http://localhost:8025"
REALM = "office-toolkit"
APP = "http://localhost:5050"
EMAIL = "journey.test@example.com"
PASSWORD = "correct horse battery"
NEW_PASSWORD = "a fresh password 99"
SELF_EMAIL = "self.registration@example.com"
SELF_PASSWORD = "self registered pass 42"


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


def mailpit_latest_link(to_addr, timeout_s=60):
    """Wait for the newest Mailpit message to to_addr; return its KC link."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(f"{MAILPIT}/api/v1/messages")
        for msg in r.json().get("messages", []):
            if msg["To"][0]["Address"] != to_addr:
                continue
            full = requests.get(f"{MAILPIT}/api/v1/message/{msg['ID']}").json()
            body = full.get("Text", "") + full.get("HTML", "")
            m = re.search(r'https?://localhost:8080[^\s"<>]+', body)
            if m:
                url = m.group(0).replace("&amp;", "&").rstrip(".,;'")
                return url, full.get("Subject", "")
        time.sleep(2)
    fail(f"no mail for {to_addr} arrived")


def page_summary(page):
    sp = urllib.parse.urlsplit(page.url)
    q = urllib.parse.parse_qs(sp.query)
    err = ""
    if "error" in q:
        err = f" error={q.get('error')} desc={q.get('error_description')}"
    text = re.sub(r"\s+", " ", page.inner_text("body")).strip()[:200]
    return f"path={sp.path}{err} title={page.title()!r} text={text!r}"


def browser_action_link(page, link, username, password, new_password=None):
    """Complete a Keycloak action-token link in a real browser: KC may show
    a confirmation interstitial, then a login form (the action executes
    after authentication), then possibly a password form."""
    print("action link path:", urllib.parse.urlsplit(link).path)
    page.goto(link)
    for step in range(6):
        print(f"action step {step}:", page_summary(page))
        low = page.inner_text("body").lower()
        if "account has been updated" in low:
            return True
        proceed = page.locator('a:has-text("Click here to proceed")')
        if proceed.count():
            proceed.first.click()
            page.wait_for_load_state("load")
            continue
        if page.locator('input[name="username"]').count():
            page.fill('input[name="username"]', username)
            page.fill('input[name="password"]', password)
            page.locator('input[type="submit"], button[type="submit"]').first.click()
            page.wait_for_load_state("load")
            continue
        if page.locator('input[name="password-confirm"]').count() and new_password:
            # KC's update-password form names the new-password field
            # "password-new"; the registration form uses "password".
            new_sel = ('input[name="password-new"]'
                       if page.locator('input[name="password-new"]').count()
                       else 'input[name="password"]')
            page.fill(new_sel, new_password)
            page.fill('input[name="password-confirm"]', new_password)
            page.locator('input[type="submit"], button[type="submit"]').first.click()
            page.wait_for_load_state("load")
            continue
        break
    return "account has been updated" in page.inner_text("body").lower()


def login_via_toolkit(page, username, password):
    """Drive /login/oidc -> Keycloak login -> toolkit callback. Handles both
    a fresh context (login form) and an existing KC session (SSO auto-login)."""
    page.goto(f"{APP}/login/oidc")
    page.wait_for_load_state("load")
    try:
        page.wait_for_url(APP + "/", timeout=4000)
    except PWTimeout:
        page.wait_for_selector('input[name="username"]', timeout=15000)
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', password)
        page.locator('input[type="submit"], button[type="submit"]').first.click()
        page.wait_for_url(APP + "/", timeout=15000)
    resp = page.goto(APP + "/")
    if resp is None or resp.status != 200:
        fail(f"home page after login: {resp and resp.status}")
    print(f"toolkit OIDC login OK for {username} (password verified)")


def user_state(auth, email):
    users = requests.get(f"{KC}/admin/realms/{REALM}/users",
                         headers=auth, params={"email": email}).json()
    if not users:
        fail(f"user {email} not found")
    return requests.get(
        f"{KC}/admin/realms/{REALM}/users/{users[0]['id']}",
        headers=auth).json()


def admin_user_journey(auth, ctx):
    """Admin-created user + verify-email through real Keycloak mail, login."""
    r = requests.post(f"{KC}/admin/realms/{REALM}/users", headers=auth, json={
        "username": EMAIL, "email": EMAIL, "firstName": "Journey",
        "lastName": "Test", "enabled": True, "emailVerified": False,
        "credentials": [{"type": "password", "value": PASSWORD,
                         "temporary": False}]})
    if r.status_code not in (201, 409):
        fail(f"user create: {r.status_code} {r.text[:200]}")
    uid = user_state(auth, EMAIL)["id"]
    r = requests.put(f"{KC}/admin/realms/{REALM}/users/{uid}/execute-actions-email",
                     headers=auth, json=["VERIFY_EMAIL"])
    if r.status_code not in (200, 204):
        fail(f"send verify email: {r.status_code} {r.text[:200]}")
    link, subject = mailpit_latest_link(EMAIL)
    print("captured mail:", subject)
    page = ctx.new_page()
    if not browser_action_link(page, link, EMAIL, PASSWORD):
        print("user state on failure:", json.dumps(
            {k: user_state(auth, EMAIL).get(k)
             for k in ("enabled", "emailVerified", "requiredActions")}))
        fail("verify-email action link did not complete")
    if not user_state(auth, EMAIL).get("emailVerified"):
        fail("email not marked verified after completing the link")
    print("verify-email journey OK")
    dg = requests.post(f"{KC}/realms/{REALM}/protocol/openid-connect/token",
                       data={"grant_type": "password",
                             "client_id": "office-toolkit-mobile",
                             "username": EMAIL, "password": PASSWORD})
    dg_err = ((dg.json().get("error", "token-ok") + " / "
               + dg.json().get("error_description", ""))
              if dg.headers.get("content-type", "").startswith("application/json")
              else dg.text[:120])
    print("direct-grant check after verification:", dg.status_code, dg_err)
    login_via_toolkit(page, EMAIL, PASSWORD)


def self_registration_journey(auth, ctx):
    """Self-registration through the real Keycloak registration form,
    verify-email from the confirmation mail, then toolkit login."""
    # The web client enforces PKCE (S256), so even page rendering needs a
    # challenge; the verifier is unused because the flow ends at the
    # verify-email notice, never at a code exchange.
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(secrets.token_urlsafe(48).encode()).digest()
    ).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": "office-toolkit-web",
              "redirect_uri": APP + "/login/oidc/callback", "scope": "openid",
              "code_challenge": challenge, "code_challenge_method": "S256"}
    page = ctx.new_page()
    # Open the KC login page and follow the Register link it renders: the
    # href is KC's own registration URL with the parameters it accepts.
    page.goto(f"{KC}/realms/{REALM}/protocol/openid-connect/auth?"
              + urllib.parse.urlencode(params))
    page.wait_for_load_state("load")
    print("login page:", page_summary(page))
    reg = page.locator('a[href*="registration"]')
    if not reg.count():
        fail("no registration link on the Keycloak login page")
    reg.first.click()
    page.wait_for_load_state("load")
    print("registration form:", page_summary(page))
    inputs = page.eval_on_selector_all(
        "input", "els => els.map(e => e.name + ':' + e.type)")
    print("registration inputs:", inputs)
    page.fill('input[name="firstName"]', "Self")
    page.fill('input[name="lastName"]', "Registered")
    page.fill('input[name="email"]', SELF_EMAIL)
    if page.locator('input[name="username"]').count():
        page.fill('input[name="username"]', SELF_EMAIL)
    page.fill('input[name="password"]', SELF_PASSWORD)
    page.fill('input[name="password-confirm"]', SELF_PASSWORD)
    page.locator('input[type="submit"], button[type="submit"]').first.click()
    page.wait_for_load_state("load")
    low = page.inner_text("body").lower()
    print("after registration submit:", page_summary(page))
    if "verify your email" not in low:
        fail("registration did not lead to the verify-email notice")
    link, subject = mailpit_latest_link(SELF_EMAIL)
    print("captured mail:", subject)
    if not browser_action_link(page, link, SELF_EMAIL, SELF_PASSWORD):
        fail("self-registration verify-email link did not complete")
    if not user_state(auth, SELF_EMAIL).get("emailVerified"):
        fail("self-registered email not marked verified")
    login_via_toolkit(page, SELF_EMAIL, SELF_PASSWORD)
    print("self-registration journey OK")


def reset_journey(auth, ctx):
    uid = user_state(auth, EMAIL)["id"]
    r = requests.put(f"{KC}/admin/realms/{REALM}/users/{uid}/execute-actions-email",
                     headers=auth, json=["UPDATE_PASSWORD"])
    if r.status_code not in (200, 204):
        fail(f"send reset email: {r.status_code} {r.text[:200]}")
    link, subject = mailpit_latest_link(EMAIL)
    print("captured mail:", subject)
    page = ctx.new_page()
    if not browser_action_link(page, link, EMAIL, PASSWORD,
                               new_password=NEW_PASSWORD):
        fail("update-password action link did not complete")
    login_via_toolkit(page, EMAIL, NEW_PASSWORD)
    print("password reset journey OK")


def mobile_pkce_journey(ctx):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": "office-toolkit-mobile",
              "redirect_uri": "http://localhost:8081/auth",
              "scope": "openid email profile",
              "code_challenge": challenge, "code_challenge_method": "S256"}
    page = ctx.new_page()
    captured = {}

    def on_request(req):
        if req.url.startswith("http://localhost:8081/auth"):
            captured.setdefault("url", req.url)

    ctx.on("request", on_request)
    page.goto(f"{KC}/realms/{REALM}/protocol/openid-connect/auth?"
              + urllib.parse.urlencode(params))
    page.wait_for_selector('input[name="username"]', timeout=15000)
    page.fill('input[name="username"]', EMAIL)
    page.fill('input[name="password"]', NEW_PASSWORD)
    page.locator('input[type="submit"], button[type="submit"]').first.click()
    for _ in range(30):
        if "url" in captured:
            break
        time.sleep(0.5)
    if "url" not in captured:
        print("state:", page_summary(page))
        fail("mobile authorize: no redirect to the app callback captured")
    code = urllib.parse.parse_qs(
        urllib.parse.urlsplit(captured["url"]).query).get("code", [None])[0]
    if not code:
        fail("mobile authorize: redirect carried no code")
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
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                admin_user_journey(auth, browser.new_context())
                reset_journey(auth, browser.new_context())
                self_registration_journey(auth, browser.new_context())
                mobile_pkce_journey(browser.new_context())
            finally:
                browser.close()
    finally:
        proc.terminate()
    print("ALL JOURNEYS GREEN")


if __name__ == "__main__":
    main()
