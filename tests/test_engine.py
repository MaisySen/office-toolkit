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


def multipart(paths, fields=None):
    md = MultiDict()
    for p in paths:
        md.add("files", FileStorage(io.BytesIO(Path(p).read_bytes()),
                                    filename=os.path.basename(p)))
    for k, v in (fields or {}).items():
        md.add(k, v)
    boundary, data = encode_multipart(md)
    return data, f"multipart/form-data; boundary={boundary}"


def submit(c, op, paths, fields=None, **kw):
    body, ctype = multipart(paths, fields)
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
    # P1 ops first and in order; P2 ops (ops_p2a, ops_p2b) follow them.
    assert slugs[:13] == ["compress", "merge", "convert", "to-jpeg", "images-to-pdf", "split",
                     "rotate", "scrub", "ocr", "redact", "watermark", "unlock", "pdf-to-docx"]
    ops = {o["slug"]: o for o in rv.get_json()["operations"]}
    assert ops["ocr"]["max_pages"] == 30 and ops["ocr"]["slow"] is True
    assert [o["name"] for o in ops["watermark"]["options"]] == ["text", "opacity"]
    assert ops["split"]["options"][0]["required"] is True
    assert ops["split"]["output"]["content_type"] == "application/zip"


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
    rv = submit(eng, "replace", [sample_pdf])
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


# ---------------------------------------------------------------- P1 all tools

def run(c, op, paths, fields=None):
    rv = submit(c, op, paths, fields)
    assert rv.status_code == 202, rv.get_json()
    st = get(c, f"/internal/v1/jobs/{rv.get_json()['id']}").get_json()
    return st


def output(c, st):
    rv = get(c, f"/internal/v1/jobs/{st['id']}/output")
    assert rv.status_code == 200
    return rv.data


def test_to_jpeg_zips_one_image_per_page(eng, sample_pdf):
    import zipfile
    st = run(eng, "to-jpeg", [sample_pdf], {"dpi": "72"})
    assert st["status"] == "done" and st["result"] == {"pages": 2}
    z = zipfile.ZipFile(io.BytesIO(output(eng, st)))
    assert sorted(z.namelist()) == ["page-p1.jpg", "page-p2.jpg"]
    assert z.read("page-p1.jpg")[:3] == b"\xff\xd8\xff"


def test_images_to_pdf(eng, sample_image, tmp_path):
    from PIL import Image
    jpg = tmp_path / "b.jpg"
    Image.new("RGB", (300, 200), "red").save(jpg)
    st = run(eng, "images-to-pdf", [sample_image, str(jpg)])
    assert st["status"] == "done" and st["result"] == {"pages": 2}
    assert len(PdfReader(io.BytesIO(output(eng, st))).pages) == 2


def test_images_to_pdf_refuses_other_files(eng, sample_pdf):
    assert submit(eng, "images-to-pdf", [sample_pdf]).status_code == 400


def test_split_by_ranges(eng, sample_pdf):
    import zipfile
    st = run(eng, "split", [sample_pdf], {"ranges": "1, 2"})
    assert st["status"] == "done" and st["result"] == {"parts": 2}
    z = zipfile.ZipFile(io.BytesIO(output(eng, st)))
    assert sorted(z.namelist()) == ["part-1.pdf", "part-2.pdf"]


def test_split_needs_good_ranges(eng, sample_pdf):
    assert submit(eng, "split", [sample_pdf]).status_code == 400
    rv = submit(eng, "split", [sample_pdf], {"ranges": "one to two"})
    assert rv.status_code == 400 and "1-3" in rv.get_json()["message"]
    st = run(eng, "split", [sample_pdf], {"ranges": "1-9"})
    assert st["status"] == "failed" and "outside" in st["error"]


def test_rotate_selected_pages(eng, sample_pdf):
    st = run(eng, "rotate", [sample_pdf], {"angle": "180", "ranges": "2"})
    r = PdfReader(io.BytesIO(output(eng, st)))
    assert [p.rotation for p in r.pages] == [0, 180]
    assert submit(eng, "rotate", [sample_pdf], {"angle": "45"}).status_code == 400


def test_scrub_removes_metadata(eng, secret_pdf):
    st = run(eng, "scrub", [secret_pdf])
    meta = PdfReader(io.BytesIO(output(eng, st))).metadata or {}
    assert "SecretDoc" not in str(dict(meta))


def test_ocr_makes_scan_searchable_with_progress_cleared(eng, scanned_pdf):
    st = run(eng, "ocr", [scanned_pdf], {"language": "eng"})
    assert st["status"] == "done" and st["result"] == {"pages": 1} and "progress" not in st
    text = PdfReader(io.BytesIO(output(eng, st))).pages[0].extract_text()
    assert "4431" in text


def test_ocr_page_cap(eng, big_pdf):
    rv = submit(eng, "ocr", [big_pdf])
    assert rv.status_code == 400 and "30 pages" in rv.get_json()["message"]


def test_ocr_size_cap_413(eng, noisy_pdf, monkeypatch):
    monkeypatch.setitem(engine.OPERATIONS, "ocr", engine.OPERATIONS["ocr"].__class__(
        **{**engine.OPERATIONS["ocr"].__dict__, "max_bytes": 1024 * 1024}))
    assert os.path.getsize(noisy_pdf) > 1024 * 1024 + 64 * 1024
    rv = submit(eng, "ocr", [noisy_pdf])
    assert rv.status_code == 413


def test_redact_phrases_verified_count(eng, sample_pdf):
    st = run(eng, "redact", [sample_pdf], {"phrases": "Jane Citizen\n123 456 782"})
    assert st["status"] == "done"
    assert st["result"]["matches"] == 2 and st["result"]["verified"] is True
    text = "".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(output(eng, st))).pages)
    assert "Jane Citizen" not in text and "123 456 782" not in text


def test_redact_refuses_scan_and_missing_text(eng, scanned_pdf, sample_pdf):
    st = run(eng, "redact", [scanned_pdf], {"phrases": "4431"})
    assert st["status"] == "failed" and "OCR" in st["error"]
    st = run(eng, "redact", [sample_pdf], {"phrases": "not in here"})
    assert st["status"] == "failed" and "were found" in st["error"]


def test_redact_refuses_text_in_annotations(eng, annotated_pdf):
    st = run(eng, "redact", [annotated_pdf], {"phrases": "123 456 782"})
    assert st["status"] == "failed" and "Nothing was changed" in st["error"]


def test_watermark_text_and_opacity_bounds(eng, sample_pdf):
    st = run(eng, "watermark", [sample_pdf], {"text": "DRAFT", "opacity": "30"})
    assert st["status"] == "done"
    assert "DRAFT" in PdfReader(io.BytesIO(output(eng, st))).pages[0].extract_text()
    assert submit(eng, "watermark", [sample_pdf], {"text": "DRAFT", "opacity": "90"}).status_code == 400
    assert submit(eng, "watermark", [sample_pdf]).status_code == 400


def test_unlock_with_the_password_and_password_not_kept(eng, sample_pdf, tmp_path):
    from office_toolkit import pdfops
    locked = tmp_path / "locked.pdf"
    pdfops.add_password(sample_pdf, str(locked), "hunter22")
    st = run(eng, "unlock", [str(locked)], {"password": "wrong"})
    assert st["status"] == "failed"
    st = run(eng, "unlock", [str(locked)], {"password": "hunter22"})
    assert st["status"] == "done"
    assert not PdfReader(io.BytesIO(output(eng, st))).is_encrypted
    for root, _dirs, files in os.walk(engine.ROOT):
        for f in files:
            if f == "job.json":
                assert "hunter22" not in Path(root, f).read_text()


def test_pdf_to_docx(eng, sample_pdf):
    from docx import Document
    st = run(eng, "pdf-to-docx", [sample_pdf])
    assert st["status"] == "done" and st["result"] == {"pages_with_text": 2}
    doc = Document(io.BytesIO(output(eng, st)))
    assert any("Invoice total" in p.text for p in doc.paragraphs)


def test_option_too_long_refused(eng, sample_pdf):
    rv = submit(eng, "watermark", [sample_pdf], {"text": "x" * 81})
    assert rv.status_code == 400 and "too long" in rv.get_json()["message"]
