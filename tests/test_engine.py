"""Engine only mode, signed internal API and async jobs."""
import io
import json
import os
import time
from pathlib import Path

import pytest
from pypdf import PdfReader
from werkzeug.datastructures import FileStorage, MultiDict
from werkzeug.test import encode_multipart

from office_toolkit.web import engine, service_auth
from office_toolkit.web.app import app

KEY_ID = "stg-1"
SECRET = "s" * 40
VECTOR = json.loads((Path(__file__).parent / "fixtures" / "signing-vector.json").read_text())


@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLKIT_SERVICE_KEYS", f"{KEY_ID}:{SECRET},other-1:{'o' * 40}")
    monkeypatch.setenv("TOOLKIT_ENGINE_ONLY", "1")
    monkeypatch.setattr(engine, "ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(engine, "_jobs", {})
    monkeypatch.setattr(engine, "run_inline", True)
    monkeypatch.setattr(service_auth, "REPLAY", service_auth.ReplayCache())
    app.config["TESTING"] = True
    return app.test_client()


def signed(method, path, body=b"", ws="ws_a", user="u1", max_bytes=20 * 1024 * 1024,
           ts=None, key_id=KEY_ID, secret=SECRET, nonce=None):
    ts = str(int(time.time()) if ts is None else ts)
    nonce = nonce or os.urandom(12).hex()
    sig = service_auth.sign(secret, method, path, ts, nonce, ws, user, str(max_bytes),
                            str(len(body)))
    return {"X-Multies-Key": key_id, "X-Multies-Ts": ts, "X-Multies-Nonce": nonce, "X-Multies-Ws": ws,
            "X-Multies-User": user, "X-Multies-Max-Bytes": str(max_bytes),
            "X-Multies-Sig": sig}


def multipart(paths):
    md = MultiDict()
    for p in paths:
        md.add("files", FileStorage(io.BytesIO(Path(p).read_bytes()),
                                    filename=os.path.basename(p)))
    boundary, data = encode_multipart(md)
    return data, f"multipart/form-data; boundary={boundary}"


def submit(c, op, paths, **kw):
    body, ctype = multipart(paths)
    path = f"/internal/v1/jobs?op={op}"
    return c.post(path, data=body, content_type=ctype, headers=signed("POST", path, body, **kw))


def get(c, path, **kw):
    return c.get(path, headers=signed("GET", path, **kw))


def test_shared_signing_vector_matches():
    v = VECTOR
    assert service_auth.canonical(v["method"], v["path_qs"], v["ts"], v["nonce"], v["ws"],
                                  v["user"], v["max_bytes"], v["content_length"]) == v["canonical"]
    assert service_auth.sign(v["secret"], v["method"], v["path_qs"], v["ts"], v["nonce"],
                             v["ws"], v["user"], v["max_bytes"], v["content_length"]) == v["signature"]


def test_parse_keys_formats_and_short_keys_dropped():
    assert service_auth.parse_keys('{"a": "%s"}' % ("x" * 32)) == {"a": "x" * 32}
    assert service_auth.parse_keys(f"a:{'x' * 32}, b:short") == {"a": "x" * 32}
    assert service_auth.parse_keys("") == {}


def test_good_signature_lists_operations(eng):
    rv = get(eng, "/internal/v1/operations")
    assert rv.status_code == 200
    slugs = [o["slug"] for o in rv.get_json()["operations"]]
    assert slugs == ["compress", "merge", "convert"]


def test_unsigned_call_refused(eng):
    assert eng.get("/internal/v1/operations").status_code == 401


def test_bad_signature_refused(eng):
    h = signed("GET", "/internal/v1/operations")
    h["X-Multies-Sig"] = "0" * 64
    assert eng.get("/internal/v1/operations", headers=h).status_code == 401


def test_signature_for_other_path_refused(eng):
    h = signed("GET", "/internal/v1/jobs/abc")
    assert eng.get("/internal/v1/operations", headers=h).status_code == 401


def test_stale_signature_refused(eng):
    rv = get(eng, "/internal/v1/operations", ts=int(time.time()) - 301)
    assert rv.status_code == 401
    rv = get(eng, "/internal/v1/operations", ts=int(time.time()) + 301)
    assert rv.status_code == 401


def test_replayed_signature_refused(eng):
    h = signed("GET", "/internal/v1/operations")
    assert eng.get("/internal/v1/operations", headers=h).status_code == 200
    assert eng.get("/internal/v1/operations", headers=h).status_code == 401


def test_file_replay_cache_shared_across_instances(tmp_path):
    a = service_auth.FileReplayCache(str(tmp_path / "r"))
    b = service_auth.FileReplayCache(str(tmp_path / "r"))
    assert a.check_and_add("sig1", 1000.0) is True
    assert b.check_and_add("sig1", 1001.0) is False
    assert b.check_and_add("sig2", 1001.0) is True


def test_job_state_is_shared_through_disk(eng, sample_pdf):
    jid = submit(eng, "compress", [sample_pdf]).get_json()["id"]
    engine._jobs.clear()  # another process has no memory of the job
    st = get(eng, f"/internal/v1/jobs/{jid}").get_json()
    assert st["status"] == "done"
    assert get(eng, f"/internal/v1/jobs/{jid}/output").status_code == 200


def test_stale_running_job_is_failed(eng, sample_pdf, monkeypatch):
    t0 = time.time()
    monkeypatch.setattr(engine, "now", lambda: t0)
    monkeypatch.setattr(engine, "run_inline", False)
    monkeypatch.setattr(engine._pool, "submit", lambda *a, **k: None)
    jid = submit(eng, "compress", [sample_pdf]).get_json()["id"]
    assert get(eng, f"/internal/v1/jobs/{jid}").get_json()["status"] == "queued"
    monkeypatch.setattr(engine, "now", lambda: t0 + engine.STALE_RUNNING_SECONDS + 1)
    rv = get(eng, f"/internal/v1/jobs/{jid}")
    assert rv.status_code == 200, rv.data[:300]
    assert rv.get_json()["status"] == "failed"


def test_bad_or_missing_nonce_refused(eng):
    h = signed("GET", "/internal/v1/operations", nonce="short")
    assert eng.get("/internal/v1/operations", headers=h).status_code == 401
    h = signed("GET", "/internal/v1/operations")
    del h["X-Multies-Nonce"]
    assert eng.get("/internal/v1/operations", headers=h).status_code == 401


def test_two_calls_in_one_second_are_not_replays(eng):
    ts = int(time.time())
    assert get(eng, "/internal/v1/operations", ts=ts).status_code == 200
    assert get(eng, "/internal/v1/operations", ts=ts).status_code == 200


def test_unknown_key_refused(eng):
    rv = get(eng, "/internal/v1/operations", key_id="nope-1")
    assert rv.status_code == 401


def test_no_keys_configured_hides_internal_api(eng, monkeypatch):
    monkeypatch.delenv("TOOLKIT_SERVICE_KEYS")
    assert get(eng, "/internal/v1/operations").status_code == 404


@pytest.mark.parametrize("path,method", [
    ("/", "GET"), ("/login", "GET"), ("/register", "GET"), ("/logout", "POST"),
    ("/oauth/token", "POST"), ("/oauth/authorize", "GET"), ("/api/run/compress", "POST"),
    ("/api/me", "GET"), ("/run/compress", "POST"),
])
def test_engine_only_hides_user_routes(eng, path, method):
    assert eng.open(path, method=method).status_code == 404


def test_healthz_open(eng):
    rv = eng.get("/healthz")
    assert rv.status_code == 200 and rv.get_json()["engine_only"] is True


def test_compress_job_happy_path_and_inputs_deleted(eng, sample_pdf):
    rv = submit(eng, "compress", [sample_pdf])
    assert rv.status_code == 202, rv.get_json()
    jid = rv.get_json()["id"]
    st = get(eng, f"/internal/v1/jobs/{jid}").get_json()
    assert st["status"] == "done" and st["output"]["name"] == "compressed.pdf"
    assert not os.path.exists(os.path.join(engine.ROOT, jid, "in"))
    out = get(eng, f"/internal/v1/jobs/{jid}/output")
    assert out.status_code == 200 and out.data.startswith(b"%PDF")
    assert out.headers["Content-Type"] == "application/pdf"


def test_merge_job_pages_add_up(eng, sample_pdf, secret_pdf):
    rv = submit(eng, "merge", [sample_pdf, secret_pdf])
    jid = rv.get_json()["id"]
    out = get(eng, f"/internal/v1/jobs/{jid}/output")
    n = len(PdfReader(io.BytesIO(out.data)).pages)
    assert n == len(PdfReader(sample_pdf).pages) + len(PdfReader(secret_pdf).pages)


def test_convert_job_docx_to_pdf(eng, sample_docx):
    rv = submit(eng, "convert", [sample_docx])
    jid = rv.get_json()["id"]
    st = get(eng, f"/internal/v1/jobs/{jid}").get_json()
    if st["status"] == "failed" and "LibreOffice" in st.get("error", ""):
        pytest.skip("LibreOffice not installed")
    assert st["status"] == "done", st
    out = get(eng, f"/internal/v1/jobs/{jid}/output")
    assert out.data.startswith(b"%PDF") and len(PdfReader(io.BytesIO(out.data)).pages) >= 1


def test_wrong_file_type_refused(eng, sample_docx):
    rv = submit(eng, "compress", [sample_docx])
    assert rv.status_code == 400 and rv.get_json()["error"] == "bad_file"
    assert not os.path.exists(engine.ROOT) or os.listdir(engine.ROOT) == []


def test_file_count_rules(eng, sample_pdf):
    rv = submit(eng, "merge", [sample_pdf])
    assert rv.status_code == 400 and rv.get_json()["error"] == "file_count"


def test_unknown_op(eng, sample_pdf):
    rv = submit(eng, "ocr", [sample_pdf])
    assert rv.status_code == 400 and rv.get_json()["error"] == "unknown_op"


def test_over_caller_cap_is_413(eng, sample_pdf):
    rv = submit(eng, "compress", [sample_pdf], max_bytes=100)
    assert rv.status_code == 413


def test_other_workspace_cannot_see_job(eng, sample_pdf):
    jid = submit(eng, "compress", [sample_pdf], ws="ws_a").get_json()["id"]
    assert get(eng, f"/internal/v1/jobs/{jid}", ws="ws_b").status_code == 404
    assert get(eng, f"/internal/v1/jobs/{jid}/output", ws="ws_b").status_code == 404
    h = signed("DELETE", f"/internal/v1/jobs/{jid}", ws="ws_b")
    assert eng.delete(f"/internal/v1/jobs/{jid}", headers=h).status_code == 404
    assert get(eng, f"/internal/v1/jobs/{jid}", ws="ws_a").status_code == 200


def test_delete_removes_output(eng, sample_pdf):
    jid = submit(eng, "compress", [sample_pdf]).get_json()["id"]
    h = signed("DELETE", f"/internal/v1/jobs/{jid}")
    assert eng.delete(f"/internal/v1/jobs/{jid}", headers=h).status_code == 204
    assert not os.path.exists(os.path.join(engine.ROOT, jid))
    assert get(eng, f"/internal/v1/jobs/{jid}").status_code == 404


def test_output_expires_after_ttl(eng, sample_pdf, monkeypatch):
    t0 = time.time()
    monkeypatch.setattr(engine, "now", lambda: t0)
    jid = submit(eng, "compress", [sample_pdf]).get_json()["id"]
    assert get(eng, f"/internal/v1/jobs/{jid}").status_code == 200
    monkeypatch.setattr(engine, "now", lambda: t0 + engine.OUTPUT_TTL_SECONDS + 1)
    assert get(eng, f"/internal/v1/jobs/{jid}/output").status_code == 404
    assert not os.path.exists(os.path.join(engine.ROOT, jid))


def test_queue_full_is_429(eng, sample_pdf, monkeypatch):
    for i in range(engine.QUEUE_CAP):
        engine._jobs[f"fake{i}"] = engine.Job(id=f"fake{i}", op="compress",
                                              workspace=f"w{i}", user="u", dir="/nonexistent")
    rv = submit(eng, "compress", [sample_pdf])
    assert rv.status_code == 429 and rv.get_json()["error"] == "busy"


def test_workspace_concurrency_is_429(eng, sample_pdf):
    for i in range(engine.PER_WORKSPACE):
        engine._jobs[f"fake{i}"] = engine.Job(id=f"fake{i}", op="compress",
                                              workspace="ws_a", user="u", dir="/nonexistent")
    rv = submit(eng, "compress", [sample_pdf], ws="ws_a")
    assert rv.status_code == 429 and rv.get_json()["error"] == "workspace_busy"
    assert submit(eng, "compress", [sample_pdf], ws="ws_b").status_code == 202


def test_failed_job_reports_plain_error(eng, tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really a pdf")
    rv = submit(eng, "compress", [str(broken)])
    assert rv.status_code == 400  # page count guard rejects it before queueing
