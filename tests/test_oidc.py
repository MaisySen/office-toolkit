"""End-to-end OIDC sign-in tests against a fake but real-HTTP identity
provider: discovery, authorize redirect, token exchange, JWKS fetch and
ID-token signature verification all run for real. Only the provider itself
is a test double."""
import base64
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _int_b64(n: int) -> str:
    return _b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class FakeIdP:
    """Minimal OIDC provider over real HTTP with switchable misbehaviour."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # misbehaviour switches, all default to correct behaviour
        self.sign_with_wrong_key = False
        self.audience = "test-client"
        self.issuer_override = None
        self.expired = False
        self.nonce_override = None
        self.sub = "idp-subject-123"
        self.include_sub = True
        self.email_in_id_token = True
        self.userinfo_sub = None  # None means "same as sub"
        self.email = "sso-user@example.com"
        self.email_verified = True
        self.metadata_issuer_override = None
        self.jwks_down = False
        self.rotated = False
        self.rotate_key = rsa.generate_private_key(public_exponent=65537,
                                                   key_size=2048)
        self.token_calls = 0
        self._seen_nonce = None
        self._seen_verifier = None

        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/.well-known/openid-configuration":
                    self._json({
                        "issuer": idp.metadata_issuer_override or idp.issuer,
                        "authorization_endpoint": idp.issuer + "/authorize",
                        "token_endpoint": idp.issuer + "/token",
                        "jwks_uri": idp.issuer + "/jwks",
                        "userinfo_endpoint": idp.issuer + "/userinfo",
                    })
                elif u.path == "/jwks":
                    if idp.jwks_down:
                        self._json({"error": "down"}, code=500)
                        return
                    key = idp.rotate_key if idp.rotated else idp.key
                    kid = "test2" if idp.rotated else "test"
                    nums = key.public_key().public_numbers()
                    self._json({"keys": [{
                        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
                        "n": _int_b64(nums.n), "e": _int_b64(nums.e)}]})
                elif u.path == "/userinfo":
                    self._json({
                        "sub": idp.userinfo_sub if idp.userinfo_sub is not None
                               else idp.sub,
                        "email": idp.email,
                        "email_verified": idp.email_verified})
                elif u.path == "/authorize":
                    idp._seen_nonce = q["nonce"][0]
                    if "code_challenge" in q:
                        idp._seen_challenge = q["code_challenge"][0]
                    target = q["redirect_uri"][0] + "?" + urllib.parse.urlencode(
                        {"code": "fake-code", "state": q["state"][0]})
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                if u.path != "/token":
                    self.send_response(404)
                    self.end_headers()
                    return
                idp.token_calls += 1
                length = int(self.headers["Content-Length"])
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                idp._seen_verifier = form.get("code_verifier", [None])[0]
                now = int(time.time())
                claims = {
                    "iss": idp.issuer_override or idp.issuer,
                    "aud": idp.audience,
                    "nonce": idp.nonce_override if idp.nonce_override is not None
                             else idp._seen_nonce,
                    "iat": now,
                    "exp": now - 3600 if idp.expired else now + 3600,
                }
                if idp.include_sub:
                    claims["sub"] = idp.sub
                if idp.email_in_id_token:
                    claims["email"] = idp.email
                    claims["email_verified"] = idp.email_verified
                header = {"alg": "RS256", "kid": "test", "typ": "JWT"}
                signing_input = (_b64url(json.dumps(header).encode()) + "."
                                 + _b64url(json.dumps(claims).encode())).encode()
                key = idp.other_key if idp.sign_with_wrong_key else idp.key
                sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
                id_token = signing_input.decode() + "." + _b64url(sig)
                self._json({"access_token": "at", "token_type": "Bearer",
                            "expires_in": 3600, "id_token": id_token})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.issuer = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture
def idp():
    server = FakeIdP()
    yield server
    server.stop()


@pytest.fixture
def oidc_app(tmp_path, monkeypatch, idp):
    from authlib.integrations.flask_client import OAuth
    from office_toolkit.web import app as webapp
    from office_toolkit.web.auth import AuthStore
    store = AuthStore(str(tmp_path / "auth.db"))
    monkeypatch.setattr(webapp, "auth_store", store)
    oauth = OAuth(webapp.app)
    client = oauth.register(
        "oidc", client_id="test-client",
        server_metadata_url=idp.issuer + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile",
                       "code_challenge_method": "S256"})
    monkeypatch.setattr(webapp, "oidc_client", client)
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client(), store


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _fetch_no_redirect(url):
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        opener.open(url)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302):
            return e
        raise
    raise AssertionError("expected a redirect from the authorize endpoint")


def run_login(client):
    rv = client.get("/login/oidc")
    assert rv.status_code == 302
    resp = _fetch_no_redirect(rv.headers["Location"])
    callback_url = resp.headers["Location"]
    path = urllib.parse.urlparse(callback_url)
    return client.get(path.path + "?" + path.query)


def test_full_oidc_login_creates_sso_account(oidc_app, idp):
    client, store = oidc_app
    rv = run_login(client)
    assert rv.status_code == 302 and rv.headers["Location"] == "/"
    assert client.get("/").status_code == 200  # anonymous would redirect to /login
    row = store.db.execute("SELECT provider FROM users WHERE email=?",
                           ("sso-user@example.com",)).fetchone()
    assert row["provider"].startswith("oidc:")
    # SSO mode disables the local password route outright: no fallback
    client.get("/logout")
    rv = client.post("/login", data={"email": "sso-user@example.com",
                                     "password": "whatever123",
                                     "_csrf": "pw-csrf"})
    assert rv.status_code == 404
    # PKCE verifier actually reached the token endpoint
    assert idp._seen_verifier


def test_wrong_signature_rejected(oidc_app, idp):
    idp.sign_with_wrong_key = True
    client, _ = oidc_app
    rv = run_login(client)
    assert rv.status_code == 400 and b"could not be verified" in rv.data


def test_wrong_audience_rejected(oidc_app, idp):
    idp.audience = "someone-else"
    client, _ = oidc_app
    rv = run_login(client)
    assert rv.status_code == 400


def test_wrong_issuer_rejected(oidc_app, idp):
    idp.issuer_override = "http://evil.example.com"
    client, _ = oidc_app
    rv = run_login(client)
    assert rv.status_code == 400


def test_expired_token_rejected(oidc_app, idp):
    idp.expired = True
    client, _ = oidc_app
    rv = run_login(client)
    assert rv.status_code == 400


def test_nonce_mismatch_rejected(oidc_app, idp):
    idp.nonce_override = "attacker-nonce"
    client, _ = oidc_app
    rv = run_login(client)
    assert rv.status_code == 400


def test_bad_state_rejected(oidc_app, idp):
    client, _ = oidc_app
    client.get("/login/oidc")  # establishes a real state server side
    rv = client.get("/login/oidc/callback?code=fake-code&state=forged-state")
    assert rv.status_code == 400
    assert idp.token_calls == 0  # never reached the token endpoint


def test_state_replay_rejected(oidc_app, idp):
    client, _ = oidc_app
    rv1 = client.get("/login/oidc")
    resp = _fetch_no_redirect(rv1.headers["Location"])
    path = urllib.parse.urlparse(resp.headers["Location"])
    target = path.path + "?" + path.query
    assert client.get(target).status_code == 302
    assert client.get(target).status_code == 400  # replay of same code+state


def test_unverified_email_rejected(oidc_app, idp):
    idp.email_verified = False
    client, _ = oidc_app
    assert run_login(client).status_code == 400


def test_no_silent_linking_of_local_account(oidc_app, idp):
    client, store = oidc_app
    store.register("sso-user@example.com", "correct horse")
    rv = run_login(client)
    assert rv.status_code == 400 and b"already exists" in rv.data


def test_same_subject_new_email_signs_into_same_account(oidc_app, idp):
    client, store = oidc_app
    assert run_login(client).status_code == 302
    uid1 = store.db.execute("SELECT id FROM users WHERE email=?",
                            ("sso-user@example.com",)).fetchone()["id"]
    client.get("/logout")
    idp.email = "renamed@example.com"
    assert run_login(client).status_code == 302
    row = store.db.execute("SELECT id, email FROM users WHERE provider LIKE 'oidc:%' "
                           "AND sub='idp-subject-123'").fetchone()
    assert row["id"] == uid1 and row["email"] == "renamed@example.com"
    assert store.db.execute("SELECT count(*) c FROM users").fetchone()["c"] == 1


def test_email_reassigned_to_new_subject_refused(oidc_app, idp):
    client, store = oidc_app
    assert run_login(client).status_code == 302
    client.get("/logout")
    idp.sub = "a-different-person"
    rv = run_login(client)
    assert rv.status_code == 400 and b"different sign-in identity" in rv.data
    # original account untouched
    row = store.db.execute("SELECT email FROM users WHERE sub='idp-subject-123'").fetchone()
    assert row["email"] == "sso-user@example.com"


def test_missing_subject_rejected(oidc_app, idp):
    idp.include_sub = False
    client, _ = oidc_app
    assert run_login(client).status_code == 400


def test_email_from_userinfo_when_absent_in_id_token(oidc_app, idp):
    idp.email_in_id_token = False
    client, store = oidc_app
    rv = run_login(client)
    assert rv.status_code == 302
    assert client.get("/").status_code == 200
    row = store.db.execute("SELECT email FROM users WHERE sub='idp-subject-123'").fetchone()
    assert row["email"] == "sso-user@example.com"


def test_userinfo_sub_mismatch_rejected(oidc_app, idp):
    idp.email_in_id_token = False
    idp.userinfo_sub = "someone-else-entirely"
    client, _ = oidc_app
    rv = run_login(client)
    assert rv.status_code == 400 and b"subject does not match" in rv.data


class TestSsoModeGating:
    """With a maintained IdP configured, the prototype local password
    routes (register, login POST, bootstrap) must not survive as fallback;
    the sign-in page offers SSO only. Dev mode (no IdP) keeps local auth."""

    def test_register_get_disabled(self, oidc_app):
        client, _ = oidc_app
        assert client.get("/register").status_code == 404

    def test_register_post_disabled_even_with_bootstrap(self, oidc_app):
        client, _ = oidc_app
        r = client.post("/register", data={
            "email": "sneak@example.com", "password": "x" * 12,
            "bootstrap_token": "anything"})
        assert r.status_code == 404

    def test_login_post_disabled(self, oidc_app):
        client, _ = oidc_app
        assert client.post("/login", data={
            "email": "a@b.c", "password": "x" * 12}).status_code == 404

    def test_login_page_is_sso_only(self, oidc_app):
        client, _ = oidc_app
        r = client.get("/login")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Sign in with SSO" in body
        assert 'name="password"' not in body
        assert "Create one" not in body

    def test_callback_error_page_shows_no_local_form(self, oidc_app):
        client, _ = oidc_app
        r = client.get("/login/oidc/callback?error=access_denied")
        body = r.get_data(as_text=True)
        assert "Sign in with SSO" in body
        assert 'name="password"' not in body


def _mint_access_token(idp, **over):
    now = int(time.time())
    claims = {"iss": over.get("iss", idp.issuer),
              "sub": over.get("sub", idp.sub),
              "azp": over.get("azp", "office-toolkit-mobile"),
              "aud": over.get("aud", ["account", "office-toolkit-api"]),
              "iat": now}
    if over.get("exp", "default") != "omit":
        claims["exp"] = over.get("exp", now + 3600)
    if over.get("email", True):
        claims["email"] = idp.email
        ev = over.get("email_verified", True)
        if ev != "omit":
            claims["email_verified"] = ev
    header = {"alg": "RS256", "kid": over.get("kid", "test"), "typ": "JWT"}
    si = (_b64url(json.dumps(header).encode()) + "."
          + _b64url(json.dumps(claims).encode())).encode()
    key = over.get("key")
    if key is None:
        key = idp.other_key if over.get("wrong_key") else idp.key
    sig = key.sign(si, padding.PKCS1v15(), hashes.SHA256())
    return si.decode() + "." + _b64url(sig)


class TestIdpBearerTokens:
    """Mobile clients carry IdP-minted access tokens; the API must validate
    them (signature, issuer, expiry, authorized party) and map identity,
    and must reject anything unverifiable."""

    def _call(self, client, token):
        # /api/run/<slug> requires a user; with no file attached an
        # authenticated call lands on the 400 no-file path, anything
        # unauthenticated is a 401. That split is the assertion surface.
        return client.post("/api/run/to-jpeg",
                           headers={"Authorization": f"Bearer {token}"})

    def test_valid_idp_token_authenticates(self, oidc_app, idp):
        client, store = oidc_app
        r = self._call(client, _mint_access_token(idp))
        assert r.status_code == 400  # authenticated; no file attached
        row = store.db.execute("SELECT provider FROM users WHERE email=?",
                               (idp.email,)).fetchone()
        assert row["provider"].startswith("oidc:")

    def test_email_falls_back_to_userinfo(self, oidc_app, idp):
        client, _ = oidc_app
        r = self._call(client, _mint_access_token(idp, email=False))
        assert r.status_code == 400

    def test_userinfo_sub_mismatch_rejected(self, oidc_app, idp):
        client, _ = oidc_app
        idp.userinfo_sub = "someone-else"
        r = self._call(client, _mint_access_token(idp, email=False))
        assert r.status_code == 401

    def test_wrong_azp_rejected(self, oidc_app, idp):
        client, _ = oidc_app
        assert self._call(client, _mint_access_token(
            idp, azp="other-client", aud="other-client")).status_code == 401

    def test_wrong_signature_rejected(self, oidc_app, idp):
        client, _ = oidc_app
        assert self._call(client, _mint_access_token(
            idp, wrong_key=True)).status_code == 401

    def test_expired_rejected(self, oidc_app, idp):
        client, _ = oidc_app
        assert self._call(client, _mint_access_token(
            idp, exp=int(time.time()) - 60)).status_code == 401

    def test_wrong_issuer_rejected(self, oidc_app, idp):
        client, _ = oidc_app
        assert self._call(client, _mint_access_token(
            idp, iss="http://evil.example")).status_code == 401

    def test_garbage_token_rejected(self, oidc_app):
        client, _ = oidc_app
        assert self._call(client, "not-a-jwt").status_code == 401

    def test_wrong_resource_same_client_rejected(self, oidc_app, idp):
        # The registered mobile client, but a token minted for a different
        # resource: client identity alone must not grant API access.
        client, _ = oidc_app
        assert self._call(client, _mint_access_token(
            idp, aud=["account"])).status_code == 401

    def test_missing_exp_rejected(self, oidc_app, idp):
        client, _ = oidc_app
        assert self._call(client, _mint_access_token(
            idp, exp="omit")).status_code == 401

    def test_unverified_email_rejected(self, oidc_app, idp):
        client, _ = oidc_app
        idp.email_verified = False
        assert self._call(client, _mint_access_token(
            idp, email_verified=False)).status_code == 401

    def test_discovery_issuer_mismatch_rejected(self, oidc_app, idp):
        # Discovery claims an issuer the deployment never configured; a
        # token from that issuer must fail even though it matches the
        # fetched metadata.
        client, _ = oidc_app
        idp.metadata_issuer_override = "http://evil.example"
        assert self._call(client, _mint_access_token(
            idp, iss="http://evil.example")).status_code == 401

    def test_subject_binding_blocks_email_takeover(self, oidc_app, idp):
        # A different subject presenting an email already held by an SSO
        # account must never authenticate as that account.
        client, _ = oidc_app
        assert self._call(client, _mint_access_token(
            idp, sub="alice-sub")).status_code == 400
        assert self._call(client, _mint_access_token(
            idp, sub="bob-sub")).status_code == 401
        # the original subject still authenticates afterwards
        assert self._call(client, _mint_access_token(
            idp, sub="alice-sub")).status_code == 400

    def test_jwks_rotation_triggers_refetch(self, oidc_app, idp):
        import office_toolkit.web.app as webapp
        client, _ = oidc_app
        webapp._JWKS_CACHE.clear()
        assert self._call(client, _mint_access_token(idp)).status_code == 400
        idp.rotated = True  # provider now serves only the rotated key
        assert self._call(client, _mint_access_token(
            idp, kid="test2", key=idp.rotate_key)).status_code == 400

    def test_jwks_fetch_failure_rejected(self, oidc_app, idp):
        import office_toolkit.web.app as webapp
        client, _ = oidc_app
        webapp._JWKS_CACHE.clear()
        idp.jwks_down = True
        assert self._call(client, _mint_access_token(idp)).status_code == 401
