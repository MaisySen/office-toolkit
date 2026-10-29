"""Tests for the hardening items from the auth and deployment review:
absolute session age and purge, POST only logout, bounded JWKS fetching,
no development redirect URIs in production, and the CI password grant
seeding kept away from production realms."""
import base64
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

import pytest

from office_toolkit.web import auth as authmod
from office_toolkit.web.auth import AuthStore

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_script(name):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "scripts" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(ROOT / "scripts"))


# ---------------------------------------------- 1. session age and purge

def test_session_stops_working_after_absolute_age(tmp_path, monkeypatch):
    store = AuthStore(str(tmp_path / "auth.db"))
    uid = store.register("age@example.com", "correct horse")
    sid = store.create_web_session(uid)
    assert store.web_session_user(sid) == uid
    later = time.time() + authmod.WEB_SESSION_MAX_AGE + 1
    monkeypatch.setattr(authmod.time, "time", lambda: later)
    assert store.web_session_user(sid) is None
    # the expired row is gone, not just ignored
    assert store.db.execute("SELECT count(*) c FROM web_sessions").fetchone()["c"] == 0


def test_new_sign_in_purges_expired_rows(tmp_path):
    store = AuthStore(str(tmp_path / "auth.db"))
    uid = store.register("purge@example.com", "correct horse")
    old = time.time() - authmod.WEB_SESSION_MAX_AGE - 10
    for i in range(3):
        store.db.execute("INSERT INTO web_sessions (sid, user_id, created_at) "
                         "VALUES (?,?,?)", (f"old-{i}", uid, old))
    store.db.execute("INSERT INTO tokens (token_hash, user_id, client_id, kind, "
                     "expires_at) VALUES ('dead', ?, 'c', 'access', ?)",
                     (uid, time.time() - 1))
    store.db.commit()
    fresh = store.create_web_session(uid)
    sids = [r["sid"] for r in store.db.execute("SELECT sid FROM web_sessions")]
    assert sids == [fresh]
    assert store.db.execute("SELECT count(*) c FROM tokens").fetchone()["c"] == 0


def test_live_session_survives_purge(tmp_path):
    store = AuthStore(str(tmp_path / "auth.db"))
    uid = store.register("live@example.com", "correct horse")
    first = store.create_web_session(uid)
    store.create_web_session(uid)
    assert store.web_session_user(first) == uid


# --------------------------------------------------- 2. POST-only logout

@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    from office_toolkit.web import app as webapp
    store = AuthStore(str(tmp_path / "auth.db"))
    monkeypatch.setattr(webapp, "auth_store", store)
    webapp.app.config["TESTING"] = True
    store.register("out@example.com", "correct horse")
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "pre-login"
    c.post("/login", data={"email": "out@example.com",
                           "password": "correct horse", "_csrf": "pre-login"})
    with c.session_transaction() as s:
        token = s["_csrf"]
    assert c.get("/api/me").status_code == 200
    return c, token


def test_logout_get_does_not_sign_out(signed_in):
    c, _ = signed_in
    rv = c.get("/logout")
    assert rv.status_code == 200
    html = rv.get_data(as_text=True)
    assert 'method="post"' in html and 'action="/logout"' in html
    assert c.get("/api/me").status_code == 200  # still signed in


def test_logout_post_without_csrf_is_refused(signed_in):
    c, _ = signed_in
    assert c.post("/logout", data={}).status_code == 400
    assert c.post("/logout", data={"_csrf": "forged"}).status_code == 400
    assert c.get("/api/me").status_code == 200  # still signed in


def test_logout_post_with_csrf_signs_out(signed_in):
    c, token = signed_in
    rv = c.post("/logout", data={"_csrf": token})
    assert rv.status_code == 302 and rv.headers["Location"] == "/login"
    assert c.get("/api/me").status_code == 401  # signed out


def test_index_signs_out_with_a_form(signed_in):
    c, _ = signed_in
    html = c.get("/").get_data(as_text=True)
    assert 'href="/logout"' not in html
    assert 'action="/logout"' in html


# ----------------------------------------------- 3. bounded JWKS fetching

class _FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def jwks_counter(monkeypatch):
    import requests
    from office_toolkit.web import app as webapp
    webapp._JWKS_CACHE.clear()
    calls = []

    def fake_get(url, timeout=None, **kw):
        calls.append(url)
        return _FakeResp({"keys": [{"kty": "RSA", "kid": "known",
                                    "n": "AQAB", "e": "AQAB"}]})
    monkeypatch.setattr(requests, "get", fake_get)
    yield webapp, calls
    webapp._JWKS_CACHE.clear()


def _b64(obj):
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.mark.parametrize("garbage", [
    "", "x", "a.b", "a.b.c.d", "....", "not a token at all",
    "%%%.%%%.%%%", "a" * 20000,
    _b64({"alg": "none"}) + ".e30.sig",
    _b64({"alg": "HS256", "kid": "known"}) + ".e30.sig",
    _b64({"alg": "RS256", "kid": 7}) + ".e30.sig",
    _b64({"alg": "RS256", "kid": "k" * 300}) + ".e30.sig",
    _b64(["RS256"]) + ".e30.sig",
])
def test_garbage_tokens_never_fetch_the_jwks(jwks_counter, garbage):
    webapp, calls = jwks_counter
    assert webapp._decode_idp_token(garbage, "https://idp.test/jwks") is None
    assert calls == []


def test_unknown_kid_flood_refetches_at_most_once(jwks_counter):
    webapp, calls = jwks_counter
    for i in range(50):
        token = _b64({"alg": "RS256", "kid": f"random-{i}"}) + ".e30.c2ln"
        assert webapp._decode_idp_token(token, "https://idp.test/jwks") is None
    # one normal fetch plus one rate limited forced refetch
    assert len(calls) == 2


def test_bad_signature_on_known_kid_does_not_refetch(jwks_counter):
    webapp, calls = jwks_counter
    for _ in range(20):
        token = _b64({"alg": "RS256", "kid": "known"}) + ".e30.c2ln"
        assert webapp._decode_idp_token(token, "https://idp.test/jwks") is None
    assert len(calls) == 1


# ------------------------------- 4. no development redirects in production

def test_local_redirect_problems():
    from office_toolkit.web.app import local_redirect_problems
    assert local_redirect_problems(["officetoolkit://auth"]) == []
    bad = ["http://localhost:8081/auth", "http://10.0.2.2:8081/auth",
           "https://127.0.0.1/auth", "http://app.example.com/auth",
           "https://dev.localhost/auth"]
    assert local_redirect_problems(bad) == bad


def _boot(env_extra, tmp_path):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT),
           "TOOLKIT_AUTH_DB": str(tmp_path / "boot.db"),
           "TOOLKIT_SECRET_KEY": "test-only-secret"}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", "import office_toolkit.web.app as a; "
         "print(a.auth_store.db.execute('SELECT redirect_uris FROM clients')"
         ".fetchone()[0])"],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=60)


def test_production_refuses_dev_redirects(tmp_path):
    r = _boot({"TOOLKIT_SECURE_COOKIES": "1",
               "TOOLKIT_MOBILE_REDIRECTS":
                   "officetoolkit://auth,http://localhost:8081/auth"}, tmp_path)
    assert r.returncode != 0
    assert "not allowed in production" in r.stderr


def test_production_default_drops_the_dev_redirect(tmp_path):
    r = _boot({"TOOLKIT_SECURE_COOKIES": "1"}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "officetoolkit://auth"


def test_development_keeps_the_metro_redirect(tmp_path):
    r = _boot({}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "http://localhost:8081/auth" in r.stdout


def test_shipped_production_env_has_no_dev_redirect():
    from office_toolkit.web.app import local_redirect_problems
    env = (ROOT / "deploy" / "env.example").read_text()
    line = [ln for ln in env.splitlines()
            if ln.startswith("TOOLKIT_MOBILE_REDIRECTS=")][0]
    assert local_redirect_problems(line.split("=", 1)[1].split(",")) == []


# ------------------- 5. CI password-grant seeding kept off production realms

def test_ci_seed_refused_outside_ci():
    guard = _load_script("ci_guard")
    assert guard.ci_seed_problems(["http://localhost:8080"], env={})
    assert not guard.ci_seed_problems(["http://localhost:8080"],
                                      env={"GITHUB_ACTIONS": "true"})


@pytest.mark.parametrize("url", [
    "https://doctool.multies.si/auth",
    "http://doctool.multies.si:8080",
    "https://localhost:8080",
    "http://192.168.1.10:8080",
])
def test_ci_seed_refused_against_real_hosts(url):
    guard = _load_script("ci_guard")
    assert guard.ci_seed_problems([url], env={"GITHUB_ACTIONS": "true"})


def test_ci_seed_allows_the_ci_loopbacks():
    guard = _load_script("ci_guard")
    urls = ["http://localhost:8080", "http://127.0.0.1:8080/realms/x",
            "http://10.0.2.2:8080/realms/office-toolkit"]
    assert guard.ci_seed_problems(urls, env={"GITHUB_ACTIONS": "true"}) == []


def test_ci_scripts_call_the_guard():
    for name in ("ci_runtime_stack.py", "keycloak_ci_journey.py"):
        src = (ROOT / "scripts" / name).read_text()
        main = src[src.index("def main"):]
        # the guard is the first thing main does, before any Keycloak call
        assert "assert_ci_only(" in main[:400], name


def test_production_clients_have_the_password_grant_off():
    prov = _load_script("provision_keycloak")
    for cid, public, redirects in prov.production_clients("https://doctool.multies.si"):
        body = prov.client_body(cid, public, redirects, "placeholder")
        assert body["directAccessGrantsEnabled"] is False
        assert body["implicitFlowEnabled"] is False
        assert not any(m in u for u in body["redirectUris"]
                       for m in prov.DEV_REDIRECT_MARKERS)
    assert prov.realm_client_problems([
        {"clientId": "office-toolkit-web", "redirectUris": ["https://x/cb"]},
        {"clientId": "admin-cli", "directAccessGrantsEnabled": True},
    ]) == []


def test_production_check_catches_a_ci_shaped_client():
    prov = _load_script("provision_keycloak")
    problems = prov.realm_client_problems([
        {"clientId": "office-toolkit-mobile", "directAccessGrantsEnabled": True,
         "redirectUris": ["officetoolkit://auth", "http://localhost:8081/auth"]},
    ])
    assert any("password grant" in p for p in problems)
    assert any("localhost" in p for p in problems)
