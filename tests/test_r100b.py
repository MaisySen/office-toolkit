import io
import os
import zipfile

import pytest
from PIL import Image

from office_toolkit import ops_r100b as R
from office_toolkit.opspec import Ctx
from office_toolkit.pdfops import ToolkitError
from office_toolkit.web import engine
from office_toolkit.web.security import sniff_ext

MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
 <manifest:file-entry manifest:full-path="/" manifest:media-type="{mime}"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>"""
NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
      'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" office:version="1.2"')


def odf(path, kind):
    mime = R._ODF_MIME[".odp" if kind == "presentation" else ".odg"].decode()
    tag = "presentation" if kind == "presentation" else "drawing"
    pages = "".join(
        f'<draw:page draw:name="p{i}"><draw:frame svg:x="2cm" svg:y="2cm" svg:width="12cm" svg:height="3cm">'
        f'<draw:text-box><text:p>Slide {i} hello</text:p></draw:text-box></draw:frame></draw:page>' for i in (1, 2))
    content = f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {NS}><office:body><office:{tag}>{pages}</office:{tag}></office:body></office:document-content>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), mime)
        z.writestr("META-INF/manifest.xml", MANIFEST.format(mime=mime), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("content.xml", content, compress_type=zipfile.ZIP_DEFLATED)
    return path


def run(op, src, tmp_path, **opts):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    ctx = Ctx(work=str(work), out_dir=str(out))
    facts = R.HANDLERS[op](ctx, [str(src)], opts, str(out))
    return os.path.join(str(out), ctx.output_name), facts


def test_registered_and_listed():
    assert "slides-convert" in engine.OPERATIONS and "image-transform" in engine.OPERATIONS
    assert len(R.OPERATIONS["slides-convert"].extra_public()["outputs"]) == 5
    assert engine.P2_HANDLERS.get("image-transform") is R.image_transform


def test_plan_matrix():
    assert R.plan(".pptx", "pdf") == "pdf:impress_pdf_Export"
    assert R.plan(".odp", "pptx").startswith("pptx:")
    assert R.plan(".odg", "svg") == "svg:draw_svg_Export"
    assert R.plan(".pdf", "odg") == "odg:draw8"
    with pytest.raises(ToolkitError, match="This ODG file can become PDF or SVG here."):
        R.plan(".odg", "pptx")
    for ext, to in ((".pptx", "pptx"), (".odg", "pptx"), (".pdf", "pdf"), (".docx", "pdf"), (".odp", "svg")):
        with pytest.raises(ToolkitError):
            R.plan(ext, to)


def test_sniffing(tmp_path):
    good = odf(str(tmp_path / "a.odg"), "drawing")
    sniff_ext(good, ".odg")
    with pytest.raises(ToolkitError):
        R.sniff_slides(good, ".odp")  # a drawing named as slides
    fake = tmp_path / "b.pptx"
    fake.write_bytes(b"not a zip at all")
    with pytest.raises(ToolkitError):
        sniff_ext(str(fake), ".pptx")
    z = tmp_path / "c.pptx"
    with zipfile.ZipFile(z, "w") as zz:
        zz.writestr("hello.txt", "x")
    with pytest.raises(ToolkitError, match="not a PowerPoint"):
        R.sniff_slides(str(z), ".pptx")
    ppt = tmp_path / "d.ppt"
    ppt.write_bytes(b"PK\x03\x04junk")
    with pytest.raises(ToolkitError):
        sniff_ext(str(ppt), ".ppt")


@pytest.mark.skipif(not __import__("shutil").which("soffice"), reason="LibreOffice missing")
def test_slides_round_trip(tmp_path):
    odp = odf(str(tmp_path / "deck.odp"), "presentation")
    pdf, facts = run("slides-convert", odp, tmp_path, to="pdf")
    assert open(pdf, "rb").read(5) == b"%PDF-" and facts["pages"] == 2
    pptx, _ = run("slides-convert", odp, tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, to="pptx")
    with zipfile.ZipFile(pptx) as z:
        assert "ppt/presentation.xml" in z.namelist()
    back, _ = run("slides-convert", pptx, tmp_path / "y" if (tmp_path / "y").mkdir() is None else tmp_path, to="odp")
    with zipfile.ZipFile(back) as z:
        assert z.read("mimetype") == R._ODF_MIME[".odp"]


@pytest.mark.skipif(not __import__("shutil").which("soffice"), reason="LibreOffice missing")
def test_drawing_and_pdf_import(tmp_path):
    odg = odf(str(tmp_path / "art.odg"), "drawing")
    pdf, facts = run("slides-convert", odg, tmp_path, to="pdf")
    assert open(pdf, "rb").read(5) == b"%PDF-"
    (tmp_path / "s").mkdir()
    svg, _ = run("slides-convert", odg, tmp_path / "s", to="svg")
    assert b"<svg" in open(svg, "rb").read(4000)
    (tmp_path / "d").mkdir()
    drawn, _ = run("slides-convert", pdf, tmp_path / "d", to="odg")
    with zipfile.ZipFile(drawn) as z:
        assert z.read("mimetype") == R._ODF_MIME[".odg"]


def test_pdf_page_cap(tmp_path, monkeypatch):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(3):
        w.add_blank_page(100, 100)
    p = tmp_path / "many.pdf"
    with open(p, "wb") as fh:
        w.write(fh)
    monkeypatch.setattr(R, "PDF_TO_DRAW_MAX_PAGES", 2)
    with pytest.raises(ToolkitError, match="up to 2 pages"):
        run("slides-convert", p, tmp_path, to="odg")


def img(tmp_path, name="i.png", size=(400, 200), mode="RGB", color=(255, 255, 255)):
    im = Image.new(mode, size, color)
    p = tmp_path / name
    im.save(p)
    return p


def test_sizes_and_fits():
    assert R.target_size(400, 200, 100, 0, "inside") == (100, 50, 100, 50)
    assert R.target_size(400, 200, 800, 0, "inside") == (400, 200, 400, 200)  # never enlarged
    assert R.target_size(400, 200, 100, 100, "cover") == (100, 100, 100, 100)
    assert R.target_size(400, 200, 100, 100, "contain") == (100, 100, 100, 50)
    assert R.target_size(400, 200, 0, 0, "cover") == (400, 200, 400, 200)
    assert R.target_size(400, 200, 9999, 9999, "fill")[:2] == (4096, 4096)


def test_area_parse():
    assert R.parse_area("1, 2,3,4") == (1, 2, 3, 4)
    assert R.parse_area("") is None
    for bad in ("1,2,3", "a,b,c,d", "-1,0,5,5", "0,0,0,5"):
        with pytest.raises(ToolkitError):
            R.parse_area(bad)


def test_smart_crop_finds_the_busy_part(tmp_path):
    im = Image.new("RGB", (600, 200), (250, 250, 250))
    for x in range(480, 580, 6):  # stripes on the right
        for y in range(40, 160):
            im.putpixel((x, y), (200, 20, 20))
    box = R.crop_box(600, 200, 200, 200, "smart", im)
    assert box[0] >= 330 and box[2] - box[0] == 200
    assert R.crop_box(600, 200, 200, 200, "center")[0] == 200
    assert R.crop_box(600, 200, 200, 200, "left")[0] == 0
    assert R.crop_box(200, 600, 200, 200, "bottom")[1] == 400


def test_transform_formats(tmp_path):
    p = img(tmp_path)
    out, facts = run("image-transform", p, tmp_path, w=100, h=100, fit="cover", gravity="smart", format="webp", quality=80)
    assert facts == {"width": 100, "height": 100, "format": "webp"} and out.endswith(".webp")
    (tmp_path / "2").mkdir()
    out, facts = run("image-transform", p, tmp_path / "2", w=100, format="auto")
    assert facts["format"] == "png" and Image.open(out).size == (100, 50)
    (tmp_path / "3").mkdir()
    rgba = img(tmp_path, "a.png", mode="RGBA", color=(0, 0, 0, 0))
    out, facts = run("image-transform", rgba, tmp_path / "3", w=50, format="jpeg")
    assert Image.open(out).mode == "RGB"
    (tmp_path / "4").mkdir()
    out, facts = run("image-transform", p, tmp_path / "4", area="10,10,50,40")
    assert Image.open(out).size == (50, 40)


def test_transform_refuses(tmp_path, monkeypatch):
    junk = tmp_path / "j.png"
    junk.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    with pytest.raises(ToolkitError, match="damaged"):
        run("image-transform", junk, tmp_path)
    text = tmp_path / "t.jpg"
    text.write_bytes(b"hello")
    with pytest.raises(ToolkitError):
        run("image-transform", text, tmp_path)
    with pytest.raises(ToolkitError, match="inside the image"):
        run("image-transform", img(tmp_path, "c.png"), tmp_path, area="900,0,5,5")
    monkeypatch.setattr(R, "IMAGE_MAX_PIXELS", 1000)
    with pytest.raises(ToolkitError, match="megapixels"):
        run("image-transform", img(tmp_path, "big.png"), tmp_path)
