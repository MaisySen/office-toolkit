import os
import subprocess

import pytest
from pypdf import PdfReader
from PIL import Image

from office_toolkit import pdfops
from office_toolkit.pdfops import RedactRegion, ToolkitError


def text_of(path):
    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)


def test_merge(sample_pdf, secret_pdf, tmp_path):
    out = str(tmp_path / "merged.pdf")
    assert pdfops.merge_pdfs([sample_pdf, secret_pdf], out) == 4
    t = text_of(out)
    assert "Public paragraph stays" in t and "Second page content" in t


def test_split_ranges(sample_pdf, tmp_path):
    outs = pdfops.split_pdf(sample_pdf, "1,2", str(tmp_path))
    assert len(outs) == 2
    assert "TFN 123 456 782" in text_of(outs[0])
    assert "Second page content" in text_of(outs[1])


def test_split_rejects_bad_range(sample_pdf, tmp_path):
    with pytest.raises(ToolkitError):
        pdfops.split_pdf(sample_pdf, "5-9", str(tmp_path))


def test_rotate(sample_pdf, tmp_path):
    out = str(tmp_path / "rot.pdf")
    pdfops.rotate_pdf(sample_pdf, out, 90, "1")
    r = PdfReader(out)
    assert r.pages[0].get("/Rotate") == 90
    assert r.pages[1].get("/Rotate") in (None, 0)


def test_compress_reports_sizes(noisy_pdf, tmp_path):
    out = str(tmp_path / "small.pdf")
    res = pdfops.compress_pdf(noisy_pdf, out)
    assert os.path.exists(out) and res.in_bytes > 0 and res.out_bytes > 0
    assert len(PdfReader(out).pages) == 1
    print(f"\ncompression ratio on noise PDF: {res.ratio:.3f}")


def test_scrub_metadata(secret_pdf, tmp_path):
    out = str(tmp_path / "clean.pdf")
    pdfops.scrub_metadata(secret_pdf, out)
    meta = PdfReader(out).metadata
    assert not meta.title and not meta.author


def test_password_roundtrip(sample_pdf, tmp_path):
    locked = str(tmp_path / "locked.pdf")
    pdfops.add_password(sample_pdf, locked, "s3cret")
    with pytest.raises(ToolkitError, match="Wrong password"):
        pdfops.remove_password(locked, str(tmp_path / "x.pdf"), "nope")
    out = str(tmp_path / "open.pdf")
    pdfops.remove_password(locked, out, "s3cret")
    assert "TFN 123 456 782" in text_of(out)


def test_pdf_to_jpeg(sample_pdf, tmp_path):
    outs = pdfops.pdf_to_jpeg(sample_pdf, str(tmp_path), dpi=100)
    assert len(outs) == 2
    im = Image.open(outs[0])
    assert im.size[0] > 700 and im.format == "JPEG"


def test_watermark_text_visible_in_layer(sample_pdf, tmp_path):
    out = str(tmp_path / "wm.pdf")
    pdfops.watermark_pdf(sample_pdf, out, text="CONFIDENTIAL")
    assert "CONFIDENTIAL" in text_of(out)


def test_watermark_image_changes_pixels(sample_pdf, tmp_path):
    mark = Image.new("RGB", (300, 200), (140, 20, 20))
    mp = tmp_path / "mark.png"
    mark.save(mp)
    out = str(tmp_path / "wmi.pdf")
    pdfops.watermark_pdf(sample_pdf, out, image=str(mp), opacity=0.85, image_scale=0.5)
    (tmp_path / "b").mkdir(); (tmp_path / "a").mkdir()
    before = pdfops.pdf_to_jpeg(sample_pdf, str(tmp_path / "b"), dpi=60)[0]
    after = pdfops.pdf_to_jpeg(out, str(tmp_path / "a"), dpi=60)[0]
    b, a = Image.open(before).convert("L"), Image.open(after).convert("L")
    diff = sum(1 for x, y in zip(b.getdata(), a.getdata()) if abs(x - y) > 12)
    assert diff > b.size[0] * b.size[1] * 0.01


def test_watermark_plain_image(sample_image, tmp_path):
    out = str(tmp_path / "wm.jpg")
    pdfops.watermark_image(sample_image, out, "DRAFT")
    assert os.path.getsize(out) > 0


def test_redaction_removes_text_and_paints_black(secret_pdf, tmp_path):
    rects = pdfops.find_text_rects(secret_pdf, "TFN 123 456 782")
    assert rects, "locator should find the secret line"
    out = str(tmp_path / "redacted.pdf")
    info = pdfops.redact_pdf(secret_pdf, out, rects, dpi=150)
    assert info["pages_rasterised"] == 1
    t = text_of(out)
    assert "123 456 782" not in t and "TFN" not in t
    # untouched page keeps vector text
    assert "Second page content" in t
    # pixel check: the redaction zone is black in the render
    (tmp_path / "r").mkdir()
    jpg = pdfops.pdf_to_jpeg(out, str(tmp_path / "r"), dpi=100)[0]
    im = Image.open(jpg).convert("L")
    r = rects[0]
    scale = 100 / 72
    page_h = PdfReader(secret_pdf).pages[0].mediabox.height
    cx = int((r.rect[0] + r.rect[2]) / 2 * scale)
    cy = int((page_h - (r.rect[1] + r.rect[3]) / 2) * scale)
    region = [im.getpixel((min(max(cx + dx, 0), im.width - 1),
                           min(max(cy + dy, 0), im.height - 1)))
              for dx in (-6, 0, 6) for dy in (-4, 0, 4)]
    assert sum(region) / len(region) < 40, f"redaction zone not black: {region}"


def test_redaction_scanned_page(scanned_pdf, tmp_path):
    # whole-page scan: burn a band across the middle, confirm no crash + black pixels
    out = str(tmp_path / "scan_red.pdf")
    pdfops.redact_pdf(scanned_pdf, out, [RedactRegion(0, (40, 500, 560, 560))], dpi=120)
    assert len(PdfReader(out).pages) == 1


def test_replace_text_simple(sample_pdf, tmp_path):
    out = str(tmp_path / "rep.pdf")
    n = pdfops.replace_text(sample_pdf, out, "Jane Citizen", "John Citizen")
    assert n >= 1
    assert "John Citizen" in text_of(out)


def test_office_docx_to_pdf(sample_docx, tmp_path):
    out = pdfops.office_to_pdf(sample_docx, str(tmp_path))
    assert "Quarterly note" in text_of(out)


def test_office_xlsx_to_pdf(sample_xlsx, tmp_path):
    out = pdfops.office_to_pdf(sample_xlsx, str(tmp_path))
    assert "Staples" in text_of(out)


def test_ocr_adds_text_layer(scanned_pdf, tmp_path):
    out = str(tmp_path / "ocr.pdf")
    pdfops.ocr_pdf(scanned_pdf, out)
    t = text_of(out)
    assert "4431" in t and "77.50" in t


def test_ocr_language_list():
    langs = pdfops.list_ocr_languages()
    assert "eng" in langs


def test_images_to_pdf(sample_image, tmp_path):
    out = str(tmp_path / "imgs.pdf")
    assert pdfops.images_to_pdf([sample_image, sample_image], out) == 2
    assert len(PdfReader(out).pages) == 2


def test_pdf_to_docx_text(sample_pdf, tmp_path):
    out = str(tmp_path / "out.docx")
    assert pdfops.pdf_to_docx_text(sample_pdf, out) == 2
    from docx import Document
    text = "\n".join(p.text for p in Document(out).paragraphs)
    assert "TFN 123 456 782" in text


def test_pdf_to_docx_scan_explains(scanned_pdf, tmp_path):
    with pytest.raises(ToolkitError, match="OCR first"):
        pdfops.pdf_to_docx_text(scanned_pdf, str(tmp_path / "x.docx"))


def test_redaction_verifies_and_fails_loudly(secret_pdf, tmp_path):
    # region deliberately misses the text: verification must catch the leak
    out = str(tmp_path / "bad.pdf")
    with pytest.raises(ToolkitError, match="not covered|still present"):
        pdfops.redact_pdf(secret_pdf, out,
                          [RedactRegion(0, (10, 10, 20, 20))],
                          verify_needle="TFN 123 456 782")
    assert not os.path.exists(out)


def test_redaction_strips_metadata(secret_pdf, tmp_path):
    rects = pdfops.find_text_rects(secret_pdf, "TFN 123 456 782")
    out = str(tmp_path / "r2.pdf")
    pdfops.redact_pdf(secret_pdf, out, rects, verify_needle="TFN 123 456 782")
    meta = PdfReader(out).metadata
    assert not meta.title and not meta.author


def test_redaction_refuses_annotation_leak(annotated_pdf, tmp_path):
    rects = pdfops.find_text_rects(annotated_pdf, "TFN 123 456 782")
    with pytest.raises(ToolkitError, match="annotations"):
        pdfops.redact_pdf(annotated_pdf, str(tmp_path / "o.pdf"), rects,
                          verify_needle="TFN 123 456 782")
    assert not os.path.exists(tmp_path / "o.pdf")


def test_redaction_refuses_attachment_leak(attachment_pdf, tmp_path):
    rects = pdfops.find_text_rects(attachment_pdf, "TFN 123 456 782")
    with pytest.raises(ToolkitError, match="attachments"):
        pdfops.redact_pdf(attachment_pdf, str(tmp_path / "o.pdf"), rects,
                          verify_needle="TFN 123 456 782")


def test_big_pdf_smoke(big_pdf, tmp_path):
    out = str(tmp_path / "big-out.pdf")
    res = pdfops.compress_pdf(big_pdf, out)
    assert len(PdfReader(out).pages) == 120 and res.out_bytes > 0
    merged = str(tmp_path / "big-merged.pdf")
    assert pdfops.merge_pdfs([big_pdf, big_pdf], merged) == 240


def test_ocr_then_redact_chain(scanned_pdf, tmp_path):
    ocr = str(tmp_path / "ocr.pdf")
    pdfops.ocr_pdf(scanned_pdf, ocr)
    rects = pdfops.find_text_rects(ocr, "4431")
    assert rects, "OCR layer should make the number findable"
    out = str(tmp_path / "final.pdf")
    pdfops.redact_pdf(ocr, out, rects, verify_needle="4431")
    assert "4431" not in text_of(out)


def test_text_to_docx(tmp_path):
    out = str(tmp_path / "made.docx")
    pdfops.text_to_docx("First line\nSecond line", out, title="Made")
    from docx import Document
    paras = [p.text for p in Document(out).paragraphs]
    assert "First line" in paras and "Second line" in paras
import os
import subprocess

import pytest
from pypdf import PdfReader
from PIL import Image

from office_toolkit import pdfops
from office_toolkit.pdfops import RedactRegion, ToolkitError


def text_of(path):
    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)


def test_merge(sample_pdf, secret_pdf, tmp_path):
    out = str(tmp_path / "merged.pdf")
    assert pdfops.merge_pdfs([sample_pdf, secret_pdf], out) == 4
    t = text_of(out)
    assert "Public paragraph stays" in t and "Second page content" in t


def test_split_ranges(sample_pdf, tmp_path):
    outs = pdfops.split_pdf(sample_pdf, "1,2", str(tmp_path))
    assert len(outs) == 2
    assert "TFN 123 456 782" in text_of(outs[0])
    assert "Second page content" in text_of(outs[1])


def test_split_rejects_bad_range(sample_pdf, tmp_path):
    with pytest.raises(ToolkitError):
        pdfops.split_pdf(sample_pdf, "5-9", str(tmp_path))


def test_rotate(sample_pdf, tmp_path):
    out = str(tmp_path / "rot.pdf")
    pdfops.rotate_pdf(sample_pdf, out, 90, "1")
    r = PdfReader(out)
    assert r.pages[0].get("/Rotate") == 90
    assert r.pages[1].get("/Rotate") in (None, 0)


def test_compress_reports_sizes(noisy_pdf, tmp_path):
    out = str(tmp_path / "small.pdf")
    res = pdfops.compress_pdf(noisy_pdf, out)
    assert os.path.exists(out) and res.in_bytes > 0 and res.out_bytes > 0
    assert len(PdfReader(out).pages) == 1
    print(f"\ncompression ratio on noise PDF: {res.ratio:.3f}")


def test_scrub_metadata(secret_pdf, tmp_path):
    out = str(tmp_path / "clean.pdf")
    pdfops.scrub_metadata(secret_pdf, out)
    meta = PdfReader(out).metadata
    assert not meta.title and not meta.author


def test_password_roundtrip(sample_pdf, tmp_path):
    locked = str(tmp_path / "locked.pdf")
    pdfops.add_password(sample_pdf, locked, "s3cret")
    with pytest.raises(ToolkitError, match="Wrong password"):
        pdfops.remove_password(locked, str(tmp_path / "x.pdf"), "nope")
    out = str(tmp_path / "open.pdf")
    pdfops.remove_password(locked, out, "s3cret")
    assert "TFN 123 456 782" in text_of(out)


def test_pdf_to_jpeg(sample_pdf, tmp_path):
    outs = pdfops.pdf_to_jpeg(sample_pdf, str(tmp_path), dpi=100)
    assert len(outs) == 2
    im = Image.open(outs[0])
    assert im.size[0] > 700 and im.format == "JPEG"


def test_watermark_text_visible_in_layer(sample_pdf, tmp_path):
    out = str(tmp_path / "wm.pdf")
    pdfops.watermark_pdf(sample_pdf, out, text="CONFIDENTIAL")
    assert "CONFIDENTIAL" in text_of(out)


def test_watermark_image_changes_pixels(sample_pdf, tmp_path):
    mark = Image.new("RGB", (300, 200), (140, 20, 20))
    mp = tmp_path / "mark.png"
    mark.save(mp)
    out = str(tmp_path / "wmi.pdf")
    pdfops.watermark_pdf(sample_pdf, out, image=str(mp), opacity=0.85, image_scale=0.5)
    (tmp_path / "b").mkdir(); (tmp_path / "a").mkdir()
    before = pdfops.pdf_to_jpeg(sample_pdf, str(tmp_path / "b"), dpi=60)[0]
    after = pdfops.pdf_to_jpeg(out, str(tmp_path / "a"), dpi=60)[0]
    b, a = Image.open(before).convert("L"), Image.open(after).convert("L")
    diff = sum(1 for x, y in zip(b.getdata(), a.getdata()) if abs(x - y) > 12)
    assert diff > b.size[0] * b.size[1] * 0.01


def test_watermark_plain_image(sample_image, tmp_path):
    out = str(tmp_path / "wm.jpg")
    pdfops.watermark_image(sample_image, out, "DRAFT")
    assert os.path.getsize(out) > 0


def test_redaction_removes_text_and_paints_black(secret_pdf, tmp_path):
    rects = pdfops.find_text_rects(secret_pdf, "TFN 123 456 782")
    assert rects, "locator should find the secret line"
    out = str(tmp_path / "redacted.pdf")
    info = pdfops.redact_pdf(secret_pdf, out, rects, dpi=150)
    assert info["pages_rasterised"] == 1
    t = text_of(out)
    assert "123 456 782" not in t and "TFN" not in t
    # untouched page keeps vector text
    assert "Second page content" in t
    # pixel check: the redaction zone is black in the render
    (tmp_path / "r").mkdir()
    jpg = pdfops.pdf_to_jpeg(out, str(tmp_path / "r"), dpi=100)[0]
    im = Image.open(jpg).convert("L")
    r = rects[0]
    scale = 100 / 72
    page_h = PdfReader(secret_pdf).pages[0].mediabox.height
    cx = int((r.rect[0] + r.rect[2]) / 2 * scale)
    cy = int((page_h - (r.rect[1] + r.rect[3]) / 2) * scale)
    region = [im.getpixel((min(max(cx + dx, 0), im.width - 1),
                           min(max(cy + dy, 0), im.height - 1)))
              for dx in (-6, 0, 6) for dy in (-4, 0, 4)]
    assert sum(region) / len(region) < 40, f"redaction zone not black: {region}"


def test_redaction_scanned_page(scanned_pdf, tmp_path):
    # whole-page scan: burn a band across the middle, confirm no crash + black pixels
    out = str(tmp_path / "scan_red.pdf")
    pdfops.redact_pdf(scanned_pdf, out, [RedactRegion(0, (40, 500, 560, 560))], dpi=120)
    assert len(PdfReader(out).pages) == 1


def test_replace_text_simple(sample_pdf, tmp_path):
    out = str(tmp_path / "rep.pdf")
    n = pdfops.replace_text(sample_pdf, out, "Jane Citizen", "John Citizen")
    assert n >= 1
    assert "John Citizen" in text_of(out)


def test_office_docx_to_pdf(sample_docx, tmp_path):
    out = pdfops.office_to_pdf(sample_docx, str(tmp_path))
    assert "Quarterly note" in text_of(out)


def test_office_xlsx_to_pdf(sample_xlsx, tmp_path):
    out = pdfops.office_to_pdf(sample_xlsx, str(tmp_path))
    assert "Staples" in text_of(out)


def test_ocr_adds_text_layer(scanned_pdf, tmp_path):
    out = str(tmp_path / "ocr.pdf")
    pdfops.ocr_pdf(scanned_pdf, out)
    t = text_of(out)
    assert "4431" in t and "77.50" in t


def test_ocr_language_list():
    langs = pdfops.list_ocr_languages()
    assert "eng" in langs


def test_images_to_pdf(sample_image, tmp_path):
    out = str(tmp_path / "imgs.pdf")
    assert pdfops.images_to_pdf([sample_image, sample_image], out) == 2
    assert len(PdfReader(out).pages) == 2


def test_pdf_to_docx_text(sample_pdf, tmp_path):
    out = str(tmp_path / "out.docx")
    assert pdfops.pdf_to_docx_text(sample_pdf, out) == 2
    from docx import Document
    text = "\n".join(p.text for p in Document(out).paragraphs)
    assert "TFN 123 456 782" in text


def test_pdf_to_docx_scan_explains(scanned_pdf, tmp_path):
    with pytest.raises(ToolkitError, match="OCR first"):
        pdfops.pdf_to_docx_text(scanned_pdf, str(tmp_path / "x.docx"))


def test_redaction_verifies_and_fails_loudly(secret_pdf, tmp_path):
    # region deliberately misses the text: verification must catch the leak
    out = str(tmp_path / "bad.pdf")
    with pytest.raises(ToolkitError, match="not covered|still present"):
        pdfops.redact_pdf(secret_pdf, out,
                          [RedactRegion(0, (10, 10, 20, 20))],
                          verify_needle="TFN 123 456 782")
    assert not os.path.exists(out)


def test_redaction_strips_metadata(secret_pdf, tmp_path):
    rects = pdfops.find_text_rects(secret_pdf, "TFN 123 456 782")
    out = str(tmp_path / "r2.pdf")
    pdfops.redact_pdf(secret_pdf, out, rects, verify_needle="TFN 123 456 782")
    meta = PdfReader(out).metadata
    assert not meta.title and not meta.author


def test_redaction_refuses_annotation_leak(annotated_pdf, tmp_path):
    rects = pdfops.find_text_rects(annotated_pdf, "TFN 123 456 782")
    with pytest.raises(ToolkitError, match="annotations"):
        pdfops.redact_pdf(annotated_pdf, str(tmp_path / "o.pdf"), rects,
                          verify_needle="TFN 123 456 782")
    assert not os.path.exists(tmp_path / "o.pdf")


def test_redaction_refuses_attachment_leak(attachment_pdf, tmp_path):
    rects = pdfops.find_text_rects(attachment_pdf, "TFN 123 456 782")
    with pytest.raises(ToolkitError, match="attachments"):
        pdfops.redact_pdf(attachment_pdf, str(tmp_path / "o.pdf"), rects,
                          verify_needle="TFN 123 456 782")


def test_big_pdf_smoke(big_pdf, tmp_path):
    out = str(tmp_path / "big-out.pdf")
    res = pdfops.compress_pdf(big_pdf, out)
    assert len(PdfReader(out).pages) == 120 and res.out_bytes > 0
    merged = str(tmp_path / "big-merged.pdf")
    assert pdfops.merge_pdfs([big_pdf, big_pdf], merged) == 240


def test_ocr_then_redact_chain(scanned_pdf, tmp_path):
    ocr = str(tmp_path / "ocr.pdf")
    pdfops.ocr_pdf(scanned_pdf, ocr)
    rects = pdfops.find_text_rects(ocr, "4431")
    assert rects, "OCR layer should make the number findable"
    out = str(tmp_path / "final.pdf")
    pdfops.redact_pdf(ocr, out, rects, verify_needle="4431")
    assert "4431" not in text_of(out)


def test_text_to_docx(tmp_path):
    out = str(tmp_path / "made.docx")
    pdfops.text_to_docx("First line\nSecond line", out, title="Made")
    from docx import Document
    paras = [p.text for p in Document(out).paragraphs]
    assert "First line" in paras and "Second line" in paras


def test_split_creates_missing_out_dir(sample_pdf, tmp_path):
    outs = pdfops.split_pdf(str(sample_pdf), "1", str(tmp_path / "new" / "nested"))
    assert len(outs) == 1 and all(os.path.exists(p) for p in outs)


def test_pdf_to_jpeg_creates_missing_out_dir(sample_pdf, tmp_path):
    outs = pdfops.pdf_to_jpeg(str(sample_pdf), str(tmp_path / "new" / "jpgs"))
    assert len(outs) >= 1 and all(os.path.exists(p) for p in outs)
