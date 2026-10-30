"""Engine R68b: Compress really shrinks scanned (picture) PDFs and never
returns a file bigger than the one it was given."""
import io
import os

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from office_toolkit import pdfops
from office_toolkit.pdfops import ToolkitError
from tests.test_engine import eng, get, submit  # noqa: F401


def _scan_image(w=2480, h=3508):
    """A 300 dpi A4 'scan': text lines on tinted, noisy paper."""
    im = Image.new("RGB", (w, h), (247, 244, 236))
    d = ImageDraw.Draw(im)
    for i in range(60):
        d.text((200, 250 + i * 52), f"Line {i + 1}: services rendered, amount AUD {1000 + i * 37}.00",
               fill=(20, 20, 20))
    d.rectangle((150, 200, w - 150, h - 200), outline=(30, 30, 30), width=4)
    noise = Image.effect_noise((w, h), 20).convert("RGB")
    return Image.blend(im, noise, 0.07).filter(ImageFilter.GaussianBlur(0.5))


@pytest.fixture
def scan300_pdf(tmp_path):
    p = tmp_path / "scan300.pdf"
    c = canvas.Canvas(str(p), pagesize=A4)
    b = io.BytesIO()
    _scan_image().save(b, "JPEG", quality=90)
    b.seek(0)
    c.drawImage(ImageReader(b), 0, 0, A4[0], A4[1])
    c.showPage()
    c.save()
    return str(p)


@pytest.fixture
def text_pdf(tmp_path):
    p = tmp_path / "text.pdf"
    c = canvas.Canvas(str(p), pagesize=A4)
    for i in range(40):
        c.drawString(72, 780 - i * 18, f"Invoice line {i + 1}, amount AUD {i * 13}.00")
    c.showPage()
    c.save()
    return str(p)


def _image_widths(path):
    with pikepdf.open(path) as pdf:
        return [int(im.Width) for im in pdf.pages[0].images.values()]


def test_light_keeps_pictures_untouched(scan300_pdf, tmp_path):
    out = str(tmp_path / "light.pdf")
    r = pdfops.compress_pdf(scan300_pdf, out, "light")
    assert _image_widths(out) == [2480] and r.images_changed == 0
    assert r.out_bytes <= r.in_bytes


def test_medium_and_strong_downsample_scans(scan300_pdf, tmp_path):
    med, strong = str(tmp_path / "m.pdf"), str(tmp_path / "s.pdf")
    rm = pdfops.compress_pdf(scan300_pdf, med, "medium")
    rs = pdfops.compress_pdf(scan300_pdf, strong, "strong")
    assert rm.images_changed == 1 and rs.images_changed == 1
    assert abs(_image_widths(med)[0] - 1240) <= 2      # about 150 dpi across A4
    assert abs(_image_widths(strong)[0] - 827) <= 2    # about 100 dpi
    assert rm.out_bytes < rm.in_bytes * 0.5
    assert rs.out_bytes < rm.out_bytes
    # still a readable page of the same size
    page = pdfium.PdfDocument(strong)[0]
    assert round(page.get_width()) == round(A4[0]) and round(page.get_height()) == round(A4[1])
    px = page.render(scale=1).to_pil().convert("L")
    assert min(px.getdata()) < 80   # dark text survived


def test_default_level_is_light_in_the_library(scan300_pdf, tmp_path):
    r = pdfops.compress_pdf(scan300_pdf, str(tmp_path / "d.pdf"))
    assert r.level == "light" and r.images_changed == 0


def test_text_pdf_stays_vector(text_pdf, tmp_path):
    for level in ("light", "medium", "strong"):
        out = str(tmp_path / f"{level}.pdf")
        r = pdfops.compress_pdf(text_pdf, out, level)
        assert r.out_bytes <= r.in_bytes
        text = pdfium.PdfDocument(out)[0].get_textpage().get_text_range()
        assert "Invoice line 40" in text


def test_never_bigger_returns_the_original(scan300_pdf, tmp_path):
    once = str(tmp_path / "once.pdf")
    pdfops.compress_pdf(scan300_pdf, once, "medium")
    twice = str(tmp_path / "twice.pdf")
    r = pdfops.compress_pdf(once, twice, "medium")
    assert r.out_bytes <= r.in_bytes
    if r.already_optimised:
        assert open(once, "rb").read() == open(twice, "rb").read()


def test_already_optimised_when_nothing_to_gain(tmp_path):
    # a tiny, already tight file: any rewrite would only add bytes
    src = str(tmp_path / "tiny.pdf")
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        pdf.save(src, object_stream_mode=pikepdf.ObjectStreamMode.generate, compress_streams=True)
    out = str(tmp_path / "o.pdf")
    r = pdfops.compress_pdf(src, out, "strong")
    assert r.out_bytes <= r.in_bytes
    if r.out_bytes == r.in_bytes:
        assert r.already_optimised and open(src, "rb").read() == open(out, "rb").read()


def test_transparent_picture_keeps_its_mask(tmp_path):
    im = _scan_image(1600, 1600).convert("RGBA")
    a = Image.new("L", im.size, 0)
    ImageDraw.Draw(a).ellipse((100, 100, 1500, 1500), fill=255)
    im.putalpha(a)
    ip = tmp_path / "t.png"
    im.save(ip)
    src = str(tmp_path / "t.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    c.drawImage(ImageReader(str(ip)), 72, 300, 200, 200, mask="auto")  # 1600 px over 2.8 in
    c.showPage()
    c.save()
    out = str(tmp_path / "o.pdf")
    r = pdfops.compress_pdf(src, out, "strong")
    assert r.out_bytes < r.in_bytes
    with pikepdf.open(out) as pdf:
        img = list(pdf.pages[0].images.values())[0]
        assert "/SMask" in img
        assert (int(img.SMask.Width), int(img.SMask.Height)) == (int(img.Width), int(img.Height))
    pdfium.PdfDocument(out)[0].render(scale=1)  # renders


def test_one_bit_scan_is_left_alone(tmp_path):
    im = _scan_image().convert("1")
    ip = tmp_path / "bw.png"
    im.save(ip)
    src = str(tmp_path / "bw.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    c.drawImage(ImageReader(str(ip)), 0, 0, A4[0], A4[1])
    c.showPage()
    c.save()
    r = pdfops.compress_pdf(src, str(tmp_path / "o.pdf"), "strong")
    assert r.out_bytes <= r.in_bytes


def test_unknown_level_refused(text_pdf, tmp_path):
    with pytest.raises(ToolkitError):
        pdfops.compress_pdf(text_pdf, str(tmp_path / "o.pdf"), "extreme")


def _run_job(eng, path, fields=None):
    rv = submit(eng, "compress", [path], fields)
    assert rv.status_code == 202, rv.get_json()
    jid = rv.get_json()["id"]
    st = get(eng, f"/internal/v1/jobs/{jid}").get_json()
    assert st["status"] == "done", st
    out = get(eng, f"/internal/v1/jobs/{jid}/output").data
    return st, out


def test_engine_compress_levels_and_listing(eng, scan300_pdf):  # noqa: F811
    ops = {o["slug"]: o for o in get(eng, "/internal/v1/operations").get_json()["operations"]}
    opt = ops["compress"]["options"]
    assert [o["name"] for o in opt] == ["level"] and opt[0]["default"] == "medium"
    assert opt[0]["choices"] == ["light", "medium", "strong"] and opt[0]["required"] is False
    size = os.path.getsize(scan300_pdf)
    st, out = _run_job(eng, scan300_pdf)                     # old callers: no level sent
    assert st["result"]["level"] == "medium" and len(out) < size * 0.5
    st2, out2 = _run_job(eng, scan300_pdf, {"level": "strong"})
    assert len(out2) < len(out) and st2["result"]["already_optimised"] is False
    assert st2["output"]["name"] == "compressed.pdf" and out2.startswith(b"%PDF")


def test_engine_compress_bad_level_is_a_plain_400(eng, scan300_pdf):  # noqa: F811
    rv = submit(eng, "compress", [scan300_pdf], {"level": "max"})
    assert rv.status_code == 400
