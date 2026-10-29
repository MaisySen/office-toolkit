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
