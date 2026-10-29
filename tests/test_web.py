import io
import zipfile

import pytest
from pypdf import PdfReader

from office_toolkit.web.app import app
from office_toolkit import pdfops


@pytest.fixture
def client(tmp_path, monkeypatch):
    from office_toolkit.web.auth import AuthStore
    from office_toolkit.web import app as webapp
    store = AuthStore(str(tmp_path / "auth.db"))
    monkeypatch.setattr(webapp, "auth_store", store)
    app.config["TESTING"] = True
    c = app.test_client()
    store.register("user@example.com", "correct horse")
    # client registered only so OAuth-using tests share the same store shape
    c.get("/login")
    with c.session_transaction() as s:
        s["_csrf"] = "test-csrf"
    c.post("/login", data={"email": "user@example.com", "password": "correct horse",
                           "_csrf": "test-csrf"})
    with c.session_transaction() as s:
        token = s["_csrf"]  # fresh token issued in the post-login session
    _orig_post = c.post
    def _post(url, **kw):
        if url.startswith("/run/") and isinstance(kw.get("data"), dict):
            kw["data"].setdefault("_csrf", token)
        return _orig_post(url, **kw)
    c.post = _post
    return c


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    from office_toolkit.web.auth import AuthStore
    from office_toolkit.web import app as webapp
    store = AuthStore(str(tmp_path / "auth.db"))
    monkeypatch.setattr(webapp, "auth_store", store)
    store.register("user@example.com", "correct horse")
    app.config["TESTING"] = True
    return app.test_client()


def up(field, path, name=None):
    return (open(path, "rb"), name or path.split("/")[-1])


def test_index_lists_operations(client):
    html = client.get("/").get_data(as_text=True)
    for label in ["Merge PDFs", "Redact text permanently", "Remove password",
                  "Make scanned PDF searchable"]:
        assert label in html


def test_merge_endpoint(client, sample_pdf, secret_pdf):
    rv = client.post("/run/merge", data={
        "files": [up("files", sample_pdf, "a.pdf"), up("files", secret_pdf, "b.pdf")],
    }, content_type="multipart/form-data")
    assert rv.status_code == 200
    open("/tmp/merged-web.pdf", "wb").write(rv.data)
    assert len(PdfReader("/tmp/merged-web.pdf").pages) == 4


def test_unlock_wrong_password_returns_clean_error(client, sample_pdf, tmp_path):
    locked = tmp_path / "locked.pdf"
    pdfops.add_password(sample_pdf, str(locked), "right")
    rv = client.post("/run/unlock", data={
        "file": up("file", str(locked)), "password": "wrong",
    }, content_type="multipart/form-data")
    assert rv.status_code == 400
    assert "Wrong password" in rv.get_data(as_text=True)


def test_redact_endpoint_removes_text(client, secret_pdf):
    rv = client.post("/run/redact", data={
        "file": up("file", secret_pdf), "find": "TFN 123 456 782",
    }, content_type="multipart/form-data")
    assert rv.status_code == 200
    open("/tmp/redacted-web.pdf", "wb").write(rv.data)
    text = "\n".join((p.extract_text() or "") for p in PdfReader("/tmp/redacted-web.pdf").pages)
    assert "123 456 782" not in text


def test_redact_missing_text_explains(client, secret_pdf):
    rv = client.post("/run/redact", data={
        "file": up("file", secret_pdf), "find": "NOT PRESENT",
    }, content_type="multipart/form-data")
    assert rv.status_code == 400
    assert "No occurrence" in rv.get_data(as_text=True)


def test_to_jpeg_returns_zip(client, sample_pdf):
    rv = client.post("/run/to-jpeg", data={
        "file": up("file", sample_pdf), "dpi": "80",
    }, content_type="multipart/form-data")
    assert rv.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(rv.data))
    assert sorted(z.namelist()) == ["sample-p1.jpg", "sample-p2.jpg"]


def test_convert_endpoint(client, sample_docx):
    rv = client.post("/run/convert", data={
        "file": up("file", sample_docx),
    }, content_type="multipart/form-data")
    assert rv.status_code == 200 and rv.data[:4] == b"%PDF"


def test_images_to_pdf_endpoint(client, sample_image):
    rv = client.post("/run/images-to-pdf", data={
        "files": [up("files", sample_image, "one.png"), up("files", sample_image, "two.png")],
    }, content_type="multipart/form-data")
    assert rv.status_code == 200 and rv.data[:4] == b"%PDF"
    open("/tmp/imgs-web.pdf", "wb").write(rv.data)
    assert len(PdfReader("/tmp/imgs-web.pdf").pages) == 2


def test_pdf_to_docx_endpoint(client, sample_pdf):
    rv = client.post("/run/pdf-to-docx", data={
        "file": up("file", sample_pdf),
    }, content_type="multipart/form-data")
    assert rv.status_code == 200 and rv.data[:2] == b"PK"


def test_redact_scan_gets_ocr_hint(client, scanned_pdf):
    rv = client.post("/run/redact", data={
        "file": up("file", scanned_pdf), "find": "4431",
    }, content_type="multipart/form-data")
    assert rv.status_code == 400
    assert "OCR first" in rv.get_data(as_text=True)


import os as _os
import time as _time


def test_job_sweep_removes_old_dirs(client, sample_pdf):
    import time as _time
    from office_toolkit.web import app as webapp
    stale = _os.path.join(webapp.JOBS, "stale-job")
    _os.makedirs(stale, exist_ok=True)
    old = _time.time() - 3600
    _os.utime(stale, (old, old))
    client.post("/run/to-jpeg", data={"file": up("file", sample_pdf), "dpi": "60"},
                content_type="multipart/form-data")
    assert not _os.path.exists(stale)


def test_privacy_wording_is_accurate(client):
    html = client.get("/").get_data(as_text=True)
    assert "uploaded to this server" in html
    assert "30 minutes" in html
    assert "never sent anywhere else" not in html


def test_fake_pdf_rejected(client, tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"<html>not a pdf</html>")
    rv = client.post("/run/compress", data={"file": up("file", str(fake))},
                     content_type="multipart/form-data")
    assert rv.status_code == 400
    assert "not a PDF" in rv.get_data(as_text=True)


def test_security_headers(client):
    rv = client.get("/")
    assert rv.headers["X-Content-Type-Options"] == "nosniff"
    assert rv.headers["X-Frame-Options"] == "DENY"


def test_api_operations_list(client):
    data = client.get("/api/operations").get_json()
    slugs = [o["slug"] for o in data["operations"]]
    assert "merge" in slugs and "redact" in slugs


def test_api_error_is_json(client, tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"nope")
    rv = client.post("/api/run/compress", data={"file": up("file", str(fake))},
                     content_type="multipart/form-data")
    assert rv.status_code == 400
    assert "not a PDF" in rv.get_json()["error"]


def test_api_merge_returns_pdf(client, sample_pdf, secret_pdf):
    rv = client.post("/api/run/merge", data={
        "files": [up("files", sample_pdf, "a.pdf"), up("files", secret_pdf, "b.pdf")],
    }, content_type="multipart/form-data")
    assert rv.status_code == 200 and rv.data[:4] == b"%PDF"


def test_job_dir_expires_on_timer(client, sample_pdf):
    import time as _time
    from office_toolkit.web import app as webapp
    old_ttl = webapp.JOB_TTL_SECONDS
    webapp.JOB_TTL_SECONDS = 0.3
    try:
        before = set(_os.listdir(webapp.JOBS))
        rv = client.post("/run/to-jpeg", data={"file": up("file", sample_pdf), "dpi": "60"},
                         content_type="multipart/form-data")
        assert rv.status_code == 200
        new = set(_os.listdir(webapp.JOBS)) - before
        assert new, "job dir should exist right after the request"
        _time.sleep(0.8)
        assert not (set(_os.listdir(webapp.JOBS)) & new), "job dir should be gone after TTL"
    finally:
        webapp.JOB_TTL_SECONDS = old_ttl


def test_boot_sweep_recovers_orphans():
    from office_toolkit.web import app as webapp
    stale = _os.path.join(webapp.JOBS, "orphan-from-crash")
    _os.makedirs(stale, exist_ok=True)
    old = _time.time() - 4000
    _os.utime(stale, (old, old))
    webapp.sweep_old_jobs()
    assert not _os.path.exists(stale)


def test_retention_wording_is_a_target_not_a_guarantee(client):
    html = client.get("/").get_data(as_text=True)
    assert "targeted for deletion" in html
    assert "deleted automatically within" not in html


def test_rate_limit_trips(client, sample_pdf):
    from office_toolkit.web import app as webapp
    webapp._RATE.clear()
    old = webapp.RATE_LIMIT
    webapp.RATE_LIMIT = 3
    try:
        codes = [client.post("/run/compress", data={"file": up("file", sample_pdf)},
                             content_type="multipart/form-data").status_code
                 for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 400 and codes[4] == 400
    finally:
        webapp.RATE_LIMIT = old
        webapp._RATE.clear()


def test_run_rejects_missing_csrf_token(anon_client, sample_pdf):
    c = anon_client
    c.get("/login")
    with c.session_transaction() as s:
        s["_csrf"] = "test-csrf"
    c.post("/login", data={"email": "user@example.com", "password": "correct horse",
                           "_csrf": "test-csrf"})
    rv = c.post("/run/merge", data={
        "files": [up("files", sample_pdf), up("files", sample_pdf)]},
        content_type="multipart/form-data")
    assert rv.status_code == 400
    assert "Form expired" in rv.get_data(as_text=True)


def test_login_rejects_missing_csrf_token(anon_client):
    anon_client.get("/login")  # establishes a session token
    rv = anon_client.post("/login", data={"email": "user@example.com",
                                          "password": "correct horse"})
    assert rv.status_code == 400


def _fresh_local_client(tmp_path, monkeypatch, name):
    from office_toolkit.web.auth import AuthStore
    from office_toolkit.web import app as webapp
    store = AuthStore(str(tmp_path / name))
    monkeypatch.setattr(webapp, "auth_store", store)
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    store.register("user@example.com", "correct horse")
    c.get("/login")
    with c.session_transaction() as s:
        s["_csrf"] = "test-csrf"
    return c


@pytest.mark.parametrize("evil", ["//evil.example/x", "https://evil.example/x",
                                  "/\\evil.example", "//foo@evil.example"])
def test_login_next_rejects_external_redirect(tmp_path, monkeypatch, evil):
    """The post-login next parameter must never bounce the browser to an
    external origin; protocol-relative URLs start with a slash too."""
    c = _fresh_local_client(tmp_path, monkeypatch, "auth-evil.db")
    r = c.post("/login", query_string={"next": evil},
               data={"email": "user@example.com",
                     "password": "correct horse", "_csrf": "test-csrf"})
    assert r.status_code == 302
    assert r.headers["Location"] == "/"


def test_login_next_keeps_local_path(tmp_path, monkeypatch):
    c = _fresh_local_client(tmp_path, monkeypatch, "auth-local.db")
    r = c.post("/login", query_string={"next": "/run/to-jpeg"},
               data={"email": "user@example.com",
                     "password": "correct horse", "_csrf": "test-csrf"})
    assert r.status_code == 302
    assert r.headers["Location"] == "/run/to-jpeg"
