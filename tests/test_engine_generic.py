"""Generic engine pieces P2 ops build on: json options, extra files, file
kinds by extension, output types picked by an option, the module hook, and
the P1 listing staying exactly as it was."""
import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage, MultiDict
from werkzeug.test import encode_multipart

from office_toolkit import opspec
from office_toolkit.opspec import ExtraFiles, Operation, Option
from office_toolkit.web import engine
from tests.test_engine import eng, get, signed, submit  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"
P1 = ("compress", "merge", "convert", "to-jpeg", "images-to-pdf", "split", "rotate", "scrub",
      "ocr", "redact", "watermark", "unlock", "pdf-to-docx")

seen = {}


def _handler(ctx, inputs, opts, out):
    seen.clear()
    seen.update(inputs=inputs, opts=dict(opts), extra=[Path(p).read_bytes()[:8] for p in ctx.extra],
                extra_names=[os.path.basename(p) for p in ctx.extra])
    if opts.get("format") == "json":
        Path(ctx.output("result.json")).write_text(json.dumps({"ok": True}))
        return {"kind": "json"}
    if opts.get("format") == "bogus":
        Path(ctx.output("other.bin")).write_bytes(b"x")
        return {}
    Path(out).write_bytes(b"%PDF-1.4\n%%EOF\n")
    return {"kind": "pdf"}


@pytest.fixture
def fake(monkeypatch):
    op = Operation("fake-p2", "Fake tool", "file", 1, 2, "result.pdf",
                   options=(Option("plan", "json", "Plan", max_len=500),
                            Option("format", "choice", "Format", default="pdf",
                                   choices=("pdf", "json", "bogus"))),
                   exts=(".pdf", ".csv", ".png"),
                   extra=ExtraFiles("images", "Signature images", 0, 3, (".png",),
                                    max_bytes_each=200 * 1024),
                   outputs=("result.json",), limits=(("max_rows", 500),))
    monkeypatch.setitem(engine.OPERATIONS, "fake-p2", op)
    monkeypatch.setitem(engine.P2_HANDLERS, "fake-p2", _handler)
    return op


def send(c, op, files, extra=(), fields=None):
    md = MultiDict()
    for name, data in files:
        md.add("files", FileStorage(io.BytesIO(data), filename=name))
    for name, data in extra:
        md.add("images", FileStorage(io.BytesIO(data), filename=name))
    for k, v in (fields or {}).items():
        md.add(k, v)
    boundary, body = encode_multipart(md)
    path = f"/internal/v1/jobs?op={op}"
    return c.post(path, data=body, content_type=f"multipart/form-data; boundary={boundary}",
                  headers=signed("POST", path, body))


def _png(color=(10, 20, 30)):
    b = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(b, "PNG")
    return b.getvalue()


CSV = b"a,b\n1,2\n"


def test_p1_listing_unchanged(eng):
    listed = {o["slug"]: o for o in get(eng, "/internal/v1/operations").get_json()["operations"]}
    want = json.loads((FIXTURES / "p1-operations.json").read_text())
    assert [w["slug"] for w in want] == list(P1)
    for w in want:
        got = json.loads(json.dumps(listed[w["slug"]]))
        if w["slug"] == "ocr":  # P2 widened the languages; eng stays first and default
            lang = got["options"][0]
            assert lang["choices"][0] == "eng" and lang["default"] == "eng"
            assert {"nep", "hin", "eng+nep", "eng+hin"} <= set(lang["choices"])
            lang["choices"] = ["eng"]
        if w["slug"] == "compress":  # R68b added one optional level; old callers send none
            assert got["options"] == [{"name": "level", "kind": "choice", "label": "Level",
                                       "required": False, "default": "medium",
                                       "choices": ["light", "medium", "strong"]}]
            got["options"] = []
        assert got == w


def test_new_keys_listed_only_for_ops_that_use_them(eng, fake):
    listed = {o["slug"]: o for o in get(eng, "/internal/v1/operations").get_json()["operations"]}
    f = listed["fake-p2"]
    assert f["accepts"] == "file" and f["accepts_ext"] == [".pdf", ".csv", ".png"]
    assert f["extra_files"] == {"field": "images", "label": "Signature images", "min": 0,
                                "max": 3, "accepts_ext": [".png"], "max_bytes_each": 204800}
    assert f["outputs"] == [{"name": "result.pdf", "content_type": "application/pdf"},
                            {"name": "result.json", "content_type": "application/json"}]
    assert f["limits"] == {"max_rows": 500}
    assert {"name": "plan", "kind": "json", "label": "Plan", "required": False,
            "max_len": 500} in f["options"]


def test_json_option_parsed(eng, fake):
    rv = send(eng, "fake-p2", [("a.csv", CSV)], fields={"plan": '[{"page": 2, "rotate": 90}]'})
    assert rv.status_code == 202, rv.get_json()
    assert seen["opts"]["plan"] == [{"page": 2, "rotate": 90}]
    assert "_extra" not in seen["opts"]


@pytest.mark.parametrize("bad", ["[1, 2", "{'a': 1}", "NaNx", "[" * 400 + "]" * 400])
def test_json_option_invalid_is_plain_400(eng, fake, bad):
    rv = send(eng, "fake-p2", [("a.csv", CSV)], fields={"plan": bad})
    assert rv.status_code == 400
    assert rv.get_json()["message"] in ("Plan is not valid.", "Plan is too long.")


def test_json_option_too_long(eng, fake):
    rv = send(eng, "fake-p2", [("a.csv", CSV)], fields={"plan": json.dumps(list(range(400)))})
    assert rv.status_code == 400 and rv.get_json()["message"] == "Plan is too long."


def test_output_type_picked_by_option(eng, fake):
    rv = send(eng, "fake-p2", [("a.csv", CSV)], fields={"format": "json"})
    st = get(eng, f"/internal/v1/jobs/{rv.get_json()['id']}").get_json()
    assert st["status"] == "done"
    assert st["output"]["name"] == "result.json"
    assert st["output"]["content_type"] == "application/json"
    out = get(eng, f"/internal/v1/jobs/{st['id']}/output")
    assert out.status_code == 200 and out.mimetype == "application/json"
    assert out.get_json() == {"ok": True}


def test_default_output_type(eng, fake):
    rv = send(eng, "fake-p2", [("a.csv", CSV)])
    st = get(eng, f"/internal/v1/jobs/{rv.get_json()['id']}").get_json()
    assert st["output"] == {"name": "result.pdf", "bytes": 15, "content_type": "application/pdf"}


def test_undeclared_output_name_fails_plainly(eng, fake):
    rv = send(eng, "fake-p2", [("a.csv", CSV)], fields={"format": "bogus"})
    st = get(eng, f"/internal/v1/jobs/{rv.get_json()['id']}").get_json()
    assert st["status"] == "failed" and st["error"] == "The file could not be processed."


def test_extra_files_in_order(eng, fake):
    rv = send(eng, "fake-p2", [("a.csv", CSV)], extra=[("s1.png", _png()), ("s2.png", _png())])
    assert rv.status_code == 202, rv.get_json()
    assert seen["extra_names"] == ["x00.png", "x01.png"]
    assert all(b.startswith(b"\x89PNG") for b in seen["extra"])


def test_extra_files_rules(eng, fake):
    too_many = [(f"s{i}.png", _png()) for i in range(4)]
    assert send(eng, "fake-p2", [("a.csv", CSV)], extra=too_many).status_code == 400
    rv = send(eng, "fake-p2", [("a.csv", CSV)], extra=[("s.jpg", _png())])
    assert rv.status_code == 400 and "PNG files only" in rv.get_json()["message"]
    rv = send(eng, "fake-p2", [("a.csv", CSV)], extra=[("s.png", b"<svg>not a png</svg>")])
    assert rv.status_code == 400
    big = [("s.png", _png() + os.urandom(300 * 1024))]
    rv = send(eng, "fake-p2", [("a.csv", CSV)], extra=big)
    assert rv.status_code == 400 and "under 200 KB" in rv.get_json()["message"]


def test_file_kind_by_extension(eng, fake, sample_pdf):
    assert send(eng, "fake-p2", [("a.docx", b"PK\x03\x04")]).status_code == 400
    rv = send(eng, "fake-p2", [("a.csv", b"a,b\x00\x00\x01")])
    assert rv.status_code == 400 and "text" in rv.get_json()["message"]
    assert send(eng, "fake-p2", [("a.csv", b"\xff\xfe\xfa\xfb")]).status_code == 202  # utf-16 bom
    assert send(eng, "fake-p2", [("a.csv", b"caf\xe9 latin1")]).status_code == 400
    assert send(eng, "fake-p2", [("a.pdf", b"not a pdf")]).status_code == 400
    assert send(eng, "fake-p2", [("a.pdf", b"%PDF-1.4 broken")]).status_code == 400  # page guard
    rv = send(eng, "fake-p2", [("a.pdf", Path(sample_pdf).read_bytes()), ("b.png", _png())])
    assert rv.status_code == 202
    assert [Path(p).suffix for p in seen["inputs"]] == [".pdf", ".png"]


def test_sniff_ext_types(tmp_path):
    from office_toolkit.pdfops import ToolkitError
    from office_toolkit.web.security import sniff_ext
    p = tmp_path / "x"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    sniff_ext(str(p), ".webp")
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    with pytest.raises(ToolkitError):
        sniff_ext(str(p), ".webp")
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")
    sniff_ext(str(p), ".xls")
    with pytest.raises(ToolkitError):
        sniff_ext(str(p), ".exe")
    p.write_bytes("é".encode() * 40000)  # 80 KB, the 64 KB read cuts nothing here
    sniff_ext(str(p), ".txt")
    p.write_bytes(b"a" * (64 * 1024 - 1) + "é".encode())  # cut mid character at 64 KB
    sniff_ext(str(p), ".txt")


def test_module_hook_skips_missing_and_refuses_clash(monkeypatch):
    engine._load_p2("office_toolkit.no_such_ops_module")  # skipped quietly
    import types, sys
    mod = types.ModuleType("office_toolkit.fake_clash_ops")
    mod.OPERATIONS = {"compress": engine.OPERATIONS["compress"]}
    monkeypatch.setitem(sys.modules, "office_toolkit.fake_clash_ops", mod)
    with pytest.raises(RuntimeError):
        engine._load_p2("office_toolkit.fake_clash_ops")


def test_content_types():
    assert opspec.content_type_for("a.xlsx") == opspec.XLSX
    assert opspec.content_type_for("a.csv") == "text/csv"
    assert opspec.content_type_for("a.md") == "text/markdown"
    assert opspec.content_type_for("a.txt") == "text/plain"
    assert opspec.content_type_for("a.png") == "image/png"
    assert opspec.content_type_for("a.json") == "application/json"
