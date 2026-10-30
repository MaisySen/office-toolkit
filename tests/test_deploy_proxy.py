"""The proxied deploy entry refuses direct traffic and names the public host."""
import importlib
import os

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLKIT_AUTH_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("DOCTOOL_PROXY_SECRET", "test-proxy-secret")
    monkeypatch.setenv("PUBLIC_HOSTNAME", "doctool.example.test")
    from werkzeug.test import Client
    import deploy.render.wsgi as wsgi
    importlib.reload(wsgi)
    return Client(wsgi.app)


def test_healthz_needs_no_header(client):
    assert client.get("/healthz").status_code == 200


def test_direct_request_is_refused(client):
    assert client.get("/login").status_code == 403
    assert client.get("/login", headers={"X-Doctool-Proxy": "wrong"}).status_code == 403


def test_proxied_request_reaches_the_app(client):
    r = client.get("/login", headers={"X-Doctool-Proxy": "test-proxy-secret"})
    assert r.status_code == 200


def test_missing_secret_refuses_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLKIT_AUTH_DB", str(tmp_path / "auth.db"))
    monkeypatch.delenv("DOCTOOL_PROXY_SECRET", raising=False)
    from werkzeug.test import Client
    import deploy.render.wsgi as wsgi
    importlib.reload(wsgi)
    r = Client(wsgi.app).get("/login", headers={"X-Doctool-Proxy": ""})
    assert r.status_code == 403
