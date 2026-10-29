import base64
import hashlib
import time

import pytest

from office_toolkit.web import app as webapp
from office_toolkit.web.app import app
from office_toolkit.web.auth import AuthStore
from office_toolkit.pdfops import ToolkitError


@pytest.fixture
def store(tmp_path):
    s = AuthStore(str(tmp_path / "auth.db"))
    s.register_client(webapp.MOBILE_CLIENT_ID,
                      ["officetoolkit://auth", "http://localhost:8081/auth"])
    return s


@pytest.fixture
def anon_client(tmp_path, monkeypatch, store):
    monkeypatch.setattr(webapp, "auth_store", store)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    monkeypatch.setattr(webapp, "auth_store", store)
    app.config["TESTING"] = True
    c = app.test_client()
    c.get("/login")
    with c.session_transaction() as s:
        s["_csrf"] = "test-csrf"
    _orig_post = c.post
    def _post(url, **kw):
        if url.startswith(("/run/", "/login", "/register")) and isinstance(kw.get("data"), dict):
            kw["data"].setdefault("_csrf", "test-csrf")
        return _orig_post(url, **kw)
    c.post = _post
    return c


def pkce_pair():
    verifier = "correct-verifier-1234567890abcdef"
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# --------------------------------------------------------------- account rules

def test_register_rejects_short_password(store):
    with pytest.raises(ToolkitError, match="8 characters"):
        store.register("a@b.co", "short")


def test_register_rejects_duplicate_email(store):
    store.register("dup@example.com", "password-one")
    with pytest.raises(ToolkitError, match="already registered"):
        store.register("dup@example.com", "password-two")


def test_login_wrong_password(store):
    store.register("u@example.com", "right-password")
    with pytest.raises(ToolkitError, match="Wrong email or password"):
        store.verify_login("u@example.com", "wrong-password")


def test_login_unknown_email_same_message(store):
    with pytest.raises(ToolkitError, match="Wrong email or password"):
        store.verify_login("ghost@example.com", "any-password")


def test_password_is_hashed_at_rest(store, tmp_path):
    store.register("hash@example.com", "my-secret-password")
    raw = open(tmp_path / "auth.db", "rb").read()
    assert b"my-secret-password" not in raw


def test_registration_needs_bootstrap_token_when_empty(client, store, monkeypatch):
    monkeypatch.setattr(webapp, "BOOTSTRAP_TOKEN", "boot-secret")
    rv = client.get("/register")
    assert rv.status_code == 200 and "Bootstrap token" in rv.get_data(as_text=True)
    bad = client.post("/register", data={"email": "n@example.com", "password": "password-one",
                                         "bootstrap_token": "wrong"})
    assert bad.status_code == 403
    good = client.post("/register", data={"email": "n@example.com", "password": "password-one",
                                          "bootstrap_token": "boot-secret"})
    assert good.status_code == 302


def test_registration_blocked_without_bootstrap_env(client, store, monkeypatch):
    monkeypatch.setattr(webapp, "BOOTSTRAP_TOKEN", "")
    rv = client.get("/register")
    assert rv.status_code == 403
    assert "no accounts yet" in rv.get_data(as_text=True)


def test_registration_closes_after_first_user(client, store):
    store.register("first@example.com", "password-one")
    rv = client.get("/register")
    assert rv.status_code == 403
    assert "closed" in rv.get_data(as_text=True)


# -------------------------------------------------------------- PKCE flow

def _authorize(client, store, challenge):
    store.register("m@example.com", "password-one")
    client.post("/login", data={"email": "m@example.com", "password": "password-one"})  # _csrf injected by fixture
    rv = client.get("/oauth/authorize", query_string={
        "client_id": webapp.MOBILE_CLIENT_ID,
        "redirect_uri": "officetoolkit://auth",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
    })
    assert rv.status_code == 302
    location = rv.headers["Location"]
    assert location.startswith("officetoolkit://auth?code=")
    assert "state=xyz" in location
    return location.split("code=")[1].split("&")[0]


def test_pkce_happy_path(client, store):
    verifier, challenge = pkce_pair()
    code = _authorize(client, store, challenge)
    rv = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": webapp.MOBILE_CLIENT_ID,
        "redirect_uri": "officetoolkit://auth",
        "code_verifier": verifier,
    })
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["token_type"] == "Bearer" and body["access_token"] and body["refresh_token"]


def test_pkce_wrong_verifier_rejected(client, store):
    _v, challenge = pkce_pair()
    code = _authorize(client, store, challenge)
    rv = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": webapp.MOBILE_CLIENT_ID,
        "redirect_uri": "officetoolkit://auth",
        "code_verifier": "wrong-verifier",
    })
    assert rv.status_code == 400
    assert "PKCE" in rv.get_json()["error_description"]


def test_code_single_use(client, store):
    verifier, challenge = pkce_pair()
    code = _authorize(client, store, challenge)
    data = {"grant_type": "authorization_code", "code": code,
            "client_id": webapp.MOBILE_CLIENT_ID,
            "redirect_uri": "officetoolkit://auth", "code_verifier": verifier}
    assert client.post("/oauth/token", data=data).status_code == 200
    assert client.post("/oauth/token", data=data).status_code == 400


def test_wrong_redirect_uri_rejected(client, store):
    verifier, challenge = pkce_pair()
    code = _authorize(client, store, challenge)
    rv = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": webapp.MOBILE_CLIENT_ID,
        "redirect_uri": "officetoolkit://evil",
        "code_verifier": verifier,
    })
    assert rv.status_code == 400


def test_unregistered_redirect_blocked_at_authorize(client, store):
    store.register("m@example.com", "password-one")
    client.post("/login", data={"email": "m@example.com", "password": "password-one"})
    _v, challenge = pkce_pair()
    rv = client.get("/oauth/authorize", query_string={
        "client_id": webapp.MOBILE_CLIENT_ID,
        "redirect_uri": "https://attacker.example/callback",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    assert rv.status_code == 400


def test_plain_pkce_method_refused(store):
    uid = store.register("p@example.com", "password-one")
    with pytest.raises(ToolkitError, match="S256"):
        store.issue_code(uid, webapp.MOBILE_CLIENT_ID, "officetoolkit://auth",
                         "abc", method="plain")


def test_refresh_rotation_invalidates_old(client, store):
    verifier, challenge = pkce_pair()
    code = _authorize(client, store, challenge)
    first = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": webapp.MOBILE_CLIENT_ID,
        "redirect_uri": "officetoolkit://auth", "code_verifier": verifier}).get_json()
    second = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
        "client_id": webapp.MOBILE_CLIENT_ID}).get_json()
    assert second["access_token"]
    rv = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
        "client_id": webapp.MOBILE_CLIENT_ID})
    assert rv.status_code == 400


def test_expired_access_token_rejected(store):
    uid = store.register("e@example.com", "password-one")
    pair = store._issue_token_pair(uid, webapp.MOBILE_CLIENT_ID)
    store.db.execute("UPDATE tokens SET expires_at=? WHERE token_hash=?",
                     (time.time() - 10, hashlib.sha256(pair["access_token"].encode()).hexdigest()))
    store.db.commit()
    assert store.authenticate_bearer(pair["access_token"]) is None


# ------------------------------------------------------------- route protection

def test_api_requires_auth(anon_client):
    rv = anon_client.get("/api/me")
    assert rv.status_code == 401


def test_web_run_redirects_to_login(anon_client):
    rv = anon_client.post("/run/compress", data={}, content_type="multipart/form-data")
    assert rv.status_code == 302 and "/login" in rv.headers["Location"]


def test_bearer_token_unlocks_api(client, store, sample_pdf):
    verifier, challenge = pkce_pair()
    code = _authorize(client, store, challenge)
    tokens = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": webapp.MOBILE_CLIENT_ID,
        "redirect_uri": "officetoolkit://auth", "code_verifier": verifier}).get_json()
    rv = client.get("/api/me", headers={"Authorization": "Bearer " + tokens["access_token"]})
    assert rv.status_code == 200
    assert rv.get_json()["email"] == "m@example.com"


def test_bruteforce_lockout(store):
    store.register("lock@example.com", "right-password")
    for _ in range(5):
        with pytest.raises(ToolkitError, match="Wrong email or password"):
            store.verify_login("lock@example.com", "nope")
    with pytest.raises(ToolkitError, match="Too many failed attempts"):
        store.verify_login("lock@example.com", "right-password")
    # lockout clears after the window
    store.db.execute("UPDATE login_attempts SET locked_until=0 WHERE email=?",
                     ("lock@example.com",))
    store.db.commit()
    assert store.verify_login("lock@example.com", "right-password")


def test_successful_login_clears_failures(store):
    store.register("clear@example.com", "right-password")
    for _ in range(3):
        with pytest.raises(ToolkitError):
            store.verify_login("clear@example.com", "nope")
    store.verify_login("clear@example.com", "right-password")
    # only two failures are now counted, so no lockout after three more
    for _ in range(3):
        with pytest.raises(ToolkitError):
            store.verify_login("clear@example.com", "nope")
    assert store.verify_login("clear@example.com", "right-password")
