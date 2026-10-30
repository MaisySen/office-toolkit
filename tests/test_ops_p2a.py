"""Engine P2 ops, helper A: thumbnails, organize, form-fields, form-fill,
compare, protect, stamp, sign, scan-clean and the widened OCR languages.
Each op has a happy path through the job API and at least one hostile input."""
import hashlib
import io
import json
import os
import shutil
import zipfile
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pypdf import PdfReader
from reportlab.lib.colors import black
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from werkzeug.datastructures import FileStorage, MultiDict
from werkzeug.test import encode_multipart

from office_toolkit import ops_p2a
from office_toolkit.pdfops import ToolkitError
from tests.test_engine import eng, get, signed, submit  # noqa: F401


def run(c, op, paths, fields=None, extra=()):
    md = MultiDict()
    for p in paths:
        md.add("files", FileStorage(io.BytesIO(Path(p).read_bytes()), filename=os.path.basename(p)))
    for p in extra:
        md.add("images", FileStorage(io.BytesIO(Path(p).read_bytes()), filename=os.path.basename(p)))
    for k, v in (fields or {}).items():
        md.add(k, v)
    boundary, body = encode_multipart(md)
    path = f"/internal/v1/jobs?op={op}"
    rv = c.post(path, data=body, content_type=f"multipart/form-data; boundary={boundary}",
                headers=signed("POST", path, body))
    if rv.status_code != 202:
        return rv.status_code, rv.get_json()
    return 202, get(c, f"/internal/v1/jobs/{rv.get_json()['id']}").get_json()


def output(c, st):
    rv = get(c, f"/internal/v1/jobs/{st['id']}/output")
    assert rv.status_code == 200
    return rv


def text_of(data: bytes) -> list[str]:
    r = PdfReader(io.BytesIO(data))
    return [p.extract_text() or "" for p in r.pages]


def make_pdf(path, pages, size=A4, rotate=None):
    c = canvas.Canvas(str(path), pagesize=size)
    for lines in pages:
        y = size[1] - 72
        c.setFont("Helvetica", 12)
        for ln in lines:
            c.drawString(72, y, ln)
            y -= 20
        c.showPage()
    c.save()
    if rotate:
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            for i, r in rotate.items():
                pdf.pages[i].Rotate = r
            pdf.save(path)
    return str(path)


@pytest.fixture
def three_pages(tmp_path):
    return make_pdf(tmp_path / "three.pdf", [["Alpha page"], ["Bravo page"], ["Charlie page"]])


@pytest.fixture
def form_pdf(tmp_path):
    p = tmp_path / "form.pdf"
    c = canvas.Canvas(str(p), pagesize=A4)
    f = c.acroForm
    c.drawString(72, 760, "Application")
    f.textfield(name="full_name", x=72, y=700, width=250, height=20, fieldFlags="required")
    f.checkbox(name="agree", x=72, y=660, size=14, buttonStyle="check")
    f.radio(name="plan", value="basic", x=72, y=620, size=14, selected=False)
    f.radio(name="plan", value="pro", x=120, y=620, size=14, selected=False)
    f.choice(name="state", x=72, y=580, width=120, height=20, options=["NSW", "VIC", "QLD"],
             value="NSW")
    c.showPage()
    c.save()
    return str(p)


@pytest.fixture
def sig_png(tmp_path):
    img = Image.new("RGBA", (400, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.line([(10, 100), (100, 20), (200, 90), (300, 30), (390, 80)], fill=(10, 20, 120, 255), width=6)
    p = tmp_path / "sig.png"
    img.save(p)
    return str(p)


def photo_of_page(path, angle=4.0, lines=None):
    """A phone photo: a white page with dark text, turned a little, lying on
    a dark desk, with a shadow across it."""
    page = Image.new("RGB", (1240, 1754), (250, 250, 246))
    d = ImageDraw.Draw(page)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    for i, ln in enumerate(lines or [f"Line {i} of the scanned invoice total 42.50" for i in range(28)]):
        d.text((110, 120 + i * 54), ln, fill=(20, 20, 20), font=font)
    page = page.rotate(angle, expand=True, fillcolor=(60, 55, 50), resample=Image.BICUBIC)
    desk = Image.new("RGB", (page.width + 300, page.height + 300), (60, 55, 50))
    desk.paste(page, (150, 150))
    shade = Image.linear_gradient("L").resize(desk.size).point(lambda v: 255 - v // 3)
    desk = Image.composite(desk, Image.new("RGB", desk.size, (0, 0, 0)), shade)
    desk.save(path, "JPEG", quality=88)
    return str(path)


# ------------------------------------------------------------------ listing

def test_ops_listed_with_caps(eng):
    ops = {o["slug"]: o for o in get(eng, "/internal/v1/operations").get_json()["operations"]}
    for slug in ("thumbnails", "organize", "form-fields", "form-fill", "compare", "protect",
                 "stamp", "sign", "scan-clean"):
        assert slug in ops
    assert ops["thumbnails"]["max_pages"] == 200
    assert ops["compare"]["min_files"] == 2 and ops["compare"]["max_files"] == 2
    assert ops["compare"]["outputs"][1] == {"name": "changes.json", "content_type": "application/json"}
    assert ops["sign"]["extra_files"]["field"] == "images"
    assert ops["form-fields"]["output"]["content_type"] == "application/json"
    assert ops["scan-clean"]["accepts_ext"] == [".jpg", ".jpeg", ".png", ".webp"]
    assert "nep" in ops["scan-clean"]["options"][1]["choices"]


# ------------------------------------------------------------------ thumbnails

def test_thumbnails_zip(eng, three_pages):
    code, st = run(eng, "thumbnails", [three_pages])
    assert st["status"] == "done", st
    assert st["result"] == {"pages": 3}
    z = zipfile.ZipFile(io.BytesIO(output(eng, st).data))
    assert z.namelist() == ["page-001.png", "page-002.png", "page-003.png"]
    im = Image.open(io.BytesIO(z.read("page-001.png")))
    assert max(im.size) == 200


def test_thumbnails_refuses_encrypted(eng, three_pages, tmp_path):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(three_pages) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(owner="o", user="u", R=6))
    code, st = run(eng, "thumbnails", [str(locked)])
    assert st["status"] == "failed" and "password" in st["error"]


def test_thumbnails_page_cap(eng, tmp_path):
    big = make_pdf(tmp_path / "big.pdf", [["p"]] * 201)
    code, body = run(eng, "thumbnails", [big])
    assert code == 400 and "200 pages" in body["message"]


# ------------------------------------------------------------------ organize

def test_organize_reorder_rotate_delete_repeat(eng, three_pages):
    plan = [{"page": 3}, {"page": 1, "rotate": 90}, {"page": 1, "rotate": 180}]
    code, st = run(eng, "organize", [three_pages], {"plan": json.dumps(plan)})
    assert st["status"] == "done", st
    data = output(eng, st).data
    r = PdfReader(io.BytesIO(data))
    assert len(r.pages) == 3
    assert "Charlie" in r.pages[0].extract_text() and "Alpha" in r.pages[1].extract_text()
    assert [p.get("/Rotate", 0) for p in r.pages] == [0, 90, 180]
    assert not any("Bravo" in t for t in text_of(data))


@pytest.mark.parametrize("plan,msg", [
    ("[]", "at least one page"),
    ('[{"page": 9}]', "not in this PDF"),
    ('[{"page": 1, "rotate": 45}]', "Rotate must be"),
    ('[{"page": "1"}]', "page number"),
    ('{"page": 1}', "at least one page"),
    ('[{"page": true}]', "page number"),
])
def test_organize_hostile_plans(eng, three_pages, plan, msg):
    code, st = run(eng, "organize", [three_pages], {"plan": plan})
    assert st["status"] == "failed" and msg in st["error"]


def test_organize_bad_json_is_400(eng, three_pages):
    code, body = run(eng, "organize", [three_pages], {"plan": "[{page:1}"})
    assert code == 400 and body["message"] == "Page plan is not valid."


# ------------------------------------------------------------------ forms

def test_form_fields_listed(eng, form_pdf):
    code, st = run(eng, "form-fields", [form_pdf])
    assert st["status"] == "done", st
    out = output(eng, st)
    assert out.mimetype == "application/json"
    fields = {f["name"]: f for f in out.get_json()["fields"]}
    assert fields["full_name"]["type"] == "text" and fields["full_name"]["required"] is True
    assert fields["agree"]["type"] == "checkbox"
    assert fields["plan"]["type"] == "radio" and set(fields["plan"]["options"]) == {"basic", "pro"}
    assert fields["state"]["type"] == "choice" and fields["state"]["options"] == ["NSW", "VIC", "QLD"]
    assert fields["full_name"]["page"] == 1 and len(fields["full_name"]["rect"]) == 4
    assert st["result"] == {"fields": 4}


def test_form_fields_none(eng, three_pages):
    code, st = run(eng, "form-fields", [three_pages])
    assert st["status"] == "done" and st["result"] == {"fields": 0}


def test_form_fill_and_flatten(eng, form_pdf):
    vals = {"full_name": "Sita Sharma", "agree": True, "plan": "pro", "state": "VIC", "nope": "x"}
    code, st = run(eng, "form-fill", [form_pdf], {"values": json.dumps(vals)})
    assert st["status"] == "done", st
    assert st["result"] == {"fields_filled": 4}
    r = PdfReader(io.BytesIO(output(eng, st).data))
    got = {k: v.get("/V") for k, v in r.get_fields().items()}
    assert got["full_name"] == "Sita Sharma" and got["agree"] == "/Yes"
    assert got["plan"] == "/pro" and got["state"] == "VIC"
    code, st = run(eng, "form-fill", [form_pdf], {"values": json.dumps(vals), "flatten": "yes"})
    assert st["status"] == "done", st
    data = output(eng, st).data
    r = PdfReader(io.BytesIO(data))
    assert not r.get_fields()
    assert "Sita Sharma" in r.pages[0].extract_text()


@pytest.mark.parametrize("vals,msg", [
    ({"plan": "gold"}, "must be one of"),
    ({"state": "WA"}, "must be one of"),
    ({"full_name": {"x": 1}}, "must be text"),
    ({"full_name": "a" * 2001}, "too long"),
    ({"unknown": "x"}, "None of those fields"),
    ([], "at least one field"),
])
def test_form_fill_hostile(eng, form_pdf, vals, msg):
    code, st = run(eng, "form-fill", [form_pdf], {"values": json.dumps(vals)})
    assert st["status"] == "failed" and msg in st["error"]


def test_form_fill_nested_names_only_fill_their_own_field(eng, tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import (ArrayObject, DictionaryObject, FloatObject, NameObject,
                               NumberObject, TextStringObject)
    w = PdfWriter()
    page = w.add_blank_page(400, 400)
    kids_all, annots = [], ArrayObject()
    for i, parent_name in enumerate(("buyer", "seller")):
        parent = DictionaryObject({NameObject("/T"): TextStringObject(parent_name),
                                   NameObject("/Kids"): ArrayObject()})
        pref = w._add_object(parent)
        kid = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): TextStringObject("name"),
            NameObject("/Rect"): ArrayObject([FloatObject(50), FloatObject(300 - i * 60),
                                              FloatObject(250), FloatObject(320 - i * 60)]),
            NameObject("/Parent"): pref, NameObject("/F"): NumberObject(4)})
        kref = w._add_object(kid)
        parent[NameObject("/Kids")].append(kref)
        annots.append(kref)
        kids_all.append(pref)
    page[NameObject("/Annots")] = annots
    w._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject(kids_all)})
    src = tmp_path / "nested.pdf"
    with open(src, "wb") as fh:
        w.write(fh)
    code, st = run(eng, "form-fields", [str(src)])
    names = sorted(f["name"] for f in output(eng, st).get_json()["fields"])
    assert names == ["buyer.name", "seller.name"]
    code, st = run(eng, "form-fill", [str(src)], {"values": json.dumps({"buyer.name": "Asha"})})
    assert st["status"] == "done", st
    fields = PdfReader(io.BytesIO(output(eng, st).data)).get_fields()
    assert fields["buyer.name"].get("/V") == "Asha"
    assert fields["seller.name"].get("/V") in (None, "")


def test_form_fill_without_fields(eng, three_pages):
    code, st = run(eng, "form-fill", [three_pages], {"values": '{"a": "b"}'})
    assert st["status"] == "failed" and st["error"] == "This PDF has no form fields to fill."


# ------------------------------------------------------------------ compare

@pytest.fixture
def versions(tmp_path):
    old = make_pdf(tmp_path / "v1.pdf", [["The rent is 500 dollars per week.", "Notice period 30 days."],
                                         ["Same second page."]])
    new = make_pdf(tmp_path / "v2.pdf", [["The rent is 650 dollars per week.", "Notice period 30 days."],
                                         ["Same second page."], ["A new third page."]])
    return old, new


def test_compare_report_pdf(eng, versions):
    code, st = run(eng, "compare", list(versions))
    assert st["status"] == "done", st
    assert st["result"]["pages_changed"] == 2
    assert st["result"]["words_added"] == 5 and st["result"]["words_removed"] == 1
    data = output(eng, st).data
    pages = text_of(data)
    assert "Comparison of two versions" in pages[0] and "Pages with changes: 2" in pages[0]
    assert len(pages) == 3  # summary plus two side by side sheets
    r = PdfReader(io.BytesIO(data))
    assert float(r.pages[1].mediabox.width) > 2 * A4[0]


def test_compare_json(eng, versions):
    code, st = run(eng, "compare", list(versions), {"output": "json"})
    assert st["status"] == "done", st
    out = output(eng, st)
    assert st["output"]["name"] == "changes.json" and out.mimetype == "application/json"
    pages = out.get_json()["pages"]
    assert pages[0] == {"page": 1, "added": ["650"], "removed": ["500"]}
    assert pages[1]["page"] == 3 and pages[1]["added"] == ["A new third page."]


def test_compare_same_file(eng, versions):
    code, st = run(eng, "compare", [versions[0], versions[0]])
    assert st["status"] == "done" and st["result"]["pages_changed"] == 0
    assert "No differences" in text_of(output(eng, st).data)[0]


def test_compare_hostile(eng, versions, tmp_path):
    assert run(eng, "compare", [versions[0]])[0] == 400          # one file
    scan = tmp_path / "scan.pdf"
    Image.new("RGB", (600, 800), "white").save(scan, "PDF")
    code, st = run(eng, "compare", [str(scan), str(scan)])
    assert st["status"] == "failed" and "no text" in st["error"]


# ------------------------------------------------------------------ protect

def test_protect_aes256(eng, three_pages):
    code, st = run(eng, "protect", [three_pages],
                   {"password": "s3cret!", "allow_print": "yes", "allow_copy": "no"})
    assert st["status"] == "done", st
    assert st["result"] == {"pages": 3, "encryption": "AES-256"}
    data = output(eng, st).data
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(io.BytesIO(data))
    with pikepdf.open(io.BytesIO(data), password="s3cret!") as pdf:
        assert pdf.encryption.R == 6 and "aes" in str(pdf.encryption.stream_method).lower()
        assert pdf.allow.print_highres is True and pdf.allow.extract is False
    assert b"s3cret!" not in json.dumps(st).encode()


def test_protect_password_not_kept(eng, three_pages, tmp_path):
    from office_toolkit.web import engine
    code, st = run(eng, "protect", [three_pages], {"password": "VeryUnique8812"})
    root = Path(engine.ROOT)
    for f in root.rglob("*"):
        if f.is_file() and f.suffix != ".pdf":
            assert b"VeryUnique8812" not in f.read_bytes()


def test_protect_hostile(eng, three_pages, tmp_path):
    code, st = run(eng, "protect", [three_pages], {"password": "abc"})
    assert st["status"] == "failed" and "4 to 128" in st["error"]
    assert run(eng, "protect", [three_pages], {"password": "x" * 129})[0] == 400
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(three_pages) as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(owner="o", user="u", R=6))
    code, st = run(eng, "protect", [str(locked)], {"password": "abcd"})
    assert st["status"] == "failed" and "already has a password" in st["error"]


# ------------------------------------------------------------------ stamp

def test_stamp_numbers_header_footer(eng, three_pages):
    code, st = run(eng, "stamp", [three_pages], {"stamp": "paid", "page_numbers": "page n of N",
                                                  "header": "Acme Pty Ltd", "footer": "Internal",
                                                  "ranges": "1-2"})
    assert st["status"] == "done", st
    assert st["result"] == {"pages_changed": 2}
    pages = text_of(output(eng, st).data)
    assert "PAID" in pages[0] and "Page 1 of 3" in pages[0] and "Acme Pty Ltd" in pages[0]
    assert "Page 2 of 3" in pages[1] and "Internal" in pages[1]
    assert "PAID" not in pages[2] and "Page 3" not in pages[2]


def test_stamp_custom_center_on_rotated_page(eng, tmp_path):
    src = make_pdf(tmp_path / "rot.pdf", [["Turned page"]], rotate={0: 90})
    code, st = run(eng, "stamp", [src], {"stamp": "custom", "stamp_text": "Checked", "position": "center",
                                        "page_numbers": "n", "number_position": "bottom-right"})
    assert st["status"] == "done", st
    data = output(eng, st).data
    assert "CHECKED" in text_of(data)[0]
    # The number sits at the bottom right of the page as shown (rotation applied).
    pdf = pdfium.PdfDocument(data)
    page = pdf[0]
    img = page.render(scale=1, rotation=0, may_draw_forms=False).to_pil().convert("L")
    w, h = img.size
    assert w > h  # displayed landscape
    corner = img.crop((int(w * 0.85), int(h * 0.9), w, h))
    assert min(corner.get_flattened_data()) < 128


@pytest.mark.parametrize("fields,msg", [
    ({}, "Choose a stamp"),
    ({"stamp": "custom"}, "Type the stamp text"),
    ({"stamp": "paid", "ranges": "2-9"}, "between 1 and 3"),
])
def test_stamp_hostile(eng, three_pages, fields, msg):
    code, st = run(eng, "stamp", [three_pages], fields)
    assert st["status"] == "failed" and msg in st["error"]


def test_stamp_refuses_bad_choice(eng, three_pages):
    code, body = run(eng, "stamp", [three_pages], {"stamp": "<script>"})
    assert code == 400


# ------------------------------------------------------------------ sign

AUDIT = {"title": "Lease 12 King St",
         "events": [{"at": "2026-10-30T04:05:00Z", "who": "Sita Sharma", "email": "sita@example.com",
                     "what": "signed", "ip_country": "Australia"},
                    {"at": "2026-10-30T05:10:30+00:00", "who": "Ram Thapa", "email": "ram@example.com",
                     "what": "signed", "ip_country": "Nepal"}]}


def test_sign_with_record(eng, three_pages, sig_png):
    sigs = [{"page": 1, "x": 72, "y": 600, "w": 200, "h": 60, "image": 0, "name": "Sita Sharma",
             "date": "30 Oct 2026"},
            {"page": 3, "x": 300, "y": 700, "w": 150, "h": 50, "image": 0}]
    code, st = run(eng, "sign", [three_pages], {"signatures": json.dumps(sigs),
                                                "audit": json.dumps(AUDIT)}, extra=[sig_png])
    assert st["status"] == "done", st
    data = output(eng, st).data
    assert st["result"]["pages"] == 4 and st["result"]["signatures"] == 2
    assert st["result"]["sha256"] == hashlib.sha256(data).hexdigest()
    pages = text_of(data)
    assert "Signed by Sita Sharma, 30 Oct 2026" in pages[0]
    rec = " ".join(pages[3].split())
    want_sha = hashlib.sha256(Path(three_pages).read_bytes()).hexdigest()
    assert "Signing record" in rec and "Lease 12 King St" in rec
    assert want_sha in pages[3].replace("\n", "")
    assert "30 Oct 2026 04:05:00 UTC" in rec and "30 Oct 2026 15:05 AEDT" in rec
    assert "ram@example.com" in rec and "Nepal" in rec
    assert "Signed electronically in Multies" in rec
    # the signature ink landed inside its box on page 1 (top left origin)
    img = pdfium.PdfDocument(data)[0].render(scale=1).to_pil().convert("RGB")
    box = img.crop((72, 600, 272, 660))
    assert any(b > r + 40 for r, g, b in box.get_flattened_data())


def test_sign_without_record(eng, three_pages, sig_png):
    sigs = [{"page": 2, "x": 10, "y": 10, "w": 100, "h": 40, "image": 0}]
    code, st = run(eng, "sign", [three_pages], {"signatures": json.dumps(sigs)}, extra=[sig_png])
    assert st["status"] == "done", st
    assert st["result"]["pages"] == 3 and st["result"]["signing_record"] is False


@pytest.mark.parametrize("sigs,msg", [
    ([], "at least one signature"),
    ([{"page": 5, "x": 1, "y": 1, "w": 50, "h": 20, "image": 0}], "page this PDF does not have"),
    ([{"page": 1, "x": 1, "y": 1, "w": 50, "h": 20, "image": 3}], "image that was not sent"),
    ([{"page": 1, "x": 580, "y": 1, "w": 50, "h": 20, "image": 0}], "outside page 1"),
    ([{"page": 1, "x": -5, "y": 1, "w": 50, "h": 20, "image": 0}], "outside page 1"),
    ([{"page": 1, "x": "a", "y": 1, "w": 50, "h": 20, "image": 0}], "position and size"),
    ([{"page": 1, "x": 1, "y": 1, "w": 50, "h": 20, "image": 0, "name": "n" * 101}], "too long"),
])
def test_sign_hostile_placements(eng, three_pages, sig_png, sigs, msg):
    code, st = run(eng, "sign", [three_pages], {"signatures": json.dumps(sigs)}, extra=[sig_png])
    assert st["status"] == "failed" and msg in st["error"], st


def test_sign_hostile_record_and_images(eng, three_pages, sig_png, tmp_path):
    sigs = json.dumps([{"page": 1, "x": 1, "y": 1, "w": 50, "h": 20, "image": 0}])
    bad = {"events": [{"at": "yesterday", "who": "x"}]}
    code, st = run(eng, "sign", [three_pages], {"signatures": sigs, "audit": json.dumps(bad)},
                   extra=[sig_png])
    assert st["status"] == "failed" and "not a valid date" in st["error"]
    bad = {"document_sha256": "zz", "events": []}
    code, st = run(eng, "sign", [three_pages], {"signatures": sigs, "audit": json.dumps(bad)},
                   extra=[sig_png])
    assert st["status"] == "failed" and "SHA-256" in st["error"]
    assert run(eng, "sign", [three_pages], {"signatures": sigs})[0] == 400       # no image
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"GIF89a not a png really")
    assert run(eng, "sign", [three_pages], {"signatures": sigs}, extra=[str(fake)])[0] == 400


def test_sign_unicode_names_do_not_crash(eng, three_pages, sig_png):
    audit = {"events": [{"at": "2026-10-30T04:05:00Z", "who": "सीता शर्मा", "email": "s@example.com",
                         "what": "signed", "ip_country": "Nepal"}]}
    sigs = [{"page": 1, "x": 72, "y": 600, "w": 200, "h": 60, "image": 0, "name": "सीता"}]
    code, st = run(eng, "sign", [three_pages], {"signatures": json.dumps(sigs),
                                                "audit": json.dumps(audit)}, extra=[sig_png])
    assert st["status"] == "done", st


# ------------------------------------------------------------------ scan-clean

def test_scan_clean_crops_straightens_and_whitens(eng, tmp_path):
    photo = photo_of_page(tmp_path / "photo.jpg", angle=4.0)
    code, st = run(eng, "scan-clean", [photo], {"mode": "gray"})
    assert st["status"] == "done", st
    assert st["result"] == {"pages": 1, "pages_with_text": 0}
    data = output(eng, st).data
    page = pdfium.PdfDocument(data)[0]
    w, h = page.get_size()
    assert 0.6 < w / h < 0.8                       # portrait page, desk cropped away
    img = page.render(scale=1).to_pil().convert("L")
    # paper corners come out light (shadow and desk gone)
    for box in ((5, 5, 40, 40), (img.width - 40, img.height - 40, img.width - 5, img.height - 5)):
        assert sum(img.crop(box).get_flattened_data()) / ((35) * (35)) > 200


def test_find_skew_recovers_angle():
    page = Image.new("L", (1000, 1300), 255)
    d = ImageDraw.Draw(page)
    for i in range(25):
        d.rectangle([80, 80 + i * 45, 900, 96 + i * 45], fill=0)
    turned = page.rotate(-3.0, expand=True, fillcolor=255)
    assert abs(ops_p2a.find_skew(turned) - 3.0) <= 0.5


def test_scan_clean_ocr_and_bw(eng, tmp_path):
    photo = photo_of_page(tmp_path / "photo.jpg", angle=2.0,
                          lines=["INVOICE 4431", "Amount due 77.50"] + ["Thank you"] * 6)
    code, st = run(eng, "scan-clean", [photo, photo], {"mode": "bw", "ocr": "yes"})
    assert st["status"] == "done", st
    assert st["result"] == {"pages": 2, "pages_with_text": 2}
    txt = text_of(output(eng, st).data)[0]
    assert "INVOICE" in txt or "4431" in txt


def test_scan_clean_hostile(eng, tmp_path, sample_pdf):
    fake = tmp_path / "fake.jpg"
    fake.write_bytes(b"%PDF-1.4 pretending")
    assert run(eng, "scan-clean", [str(fake)])[0] == 400
    assert run(eng, "scan-clean", [sample_pdf])[0] == 400       # pdf not taken
    trunc = tmp_path / "trunc.jpg"
    good = photo_of_page(tmp_path / "ok.jpg")
    trunc.write_bytes(Path(good).read_bytes()[:2000])
    code, st = run(eng, "scan-clean", [str(trunc)])
    assert st["status"] == "failed" and "could not be read" in st["error"]
    many = [good] * 21
    assert run(eng, "scan-clean", many)[0] == 400


def test_scan_clean_pixel_bomb(eng, tmp_path):
    bomb = tmp_path / "bomb.png"
    Image.new("1", (9000, 9000), 1).save(bomb, optimize=True)   # 81 MP, tiny file
    code, st = run(eng, "scan-clean", [str(bomb)])
    assert st["status"] == "failed" and ("too large" in st["error"] or "could not be read" in st["error"])


# ------------------------------------------------------------------ ocr languages

needs_nep = pytest.mark.skipif(shutil.which("tesseract") is None or
                               "nep" not in ops_p2a._tesseract_langs(),
                               reason="Nepali traineddata not installed")


@needs_nep
def test_ocr_nepali_and_combined(eng, tmp_path):
    font = None
    for p in ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
              "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
              "/usr/share/fonts/truetype/freefont/FreeSans.ttf"):
        if os.path.exists(p):
            font = ImageFont.truetype(p, 48)
            break
    if font is None:
        pytest.skip("no Devanagari font to draw the test page")
    img = Image.new("RGB", (1240, 900), "white")
    d = ImageDraw.Draw(img)
    d.text((100, 150), "नमस्ते नेपाल", fill="black", font=font)
    d.text((100, 300), "Invoice 4431", fill="black", font=ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48))
    scan = tmp_path / "nep.pdf"
    img.save(scan, "PDF", resolution=150.0)
    code, st = run(eng, "ocr", [str(scan)], {"language": "eng+nep"})
    assert st["status"] == "done", st
    txt = text_of(output(eng, st).data)[0]
    assert "नेपाल" in txt or "नमस्ते" in txt
    assert "4431" in txt


def test_ocr_language_must_be_listed(eng, sample_pdf):
    code, body = run(eng, "ocr", [sample_pdf], {"language": "eng+xxx"})
    assert code == 400
