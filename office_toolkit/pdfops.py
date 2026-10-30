"""Core PDF operations built on permissively licensed libraries only.

Stack (no AGPL dependencies):
  pikepdf  (MPL-2.0)   structure, encryption, metadata, compression
  pypdf    (BSD-3)     merge/split/rotate, watermark stamping
  pypdfium2(BSD-3/Apache-2.0)  rendering to images
  reportlab(BSD)       overlay and sample generation
  Pillow   (MIT-CMU)   image handling
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

import pikepdf
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas


class ToolkitError(Exception):
    """Clean, user-presentable failure."""


# ---------------------------------------------------------------- merge/split

def merge_pdfs(inputs: list[str], out: str) -> int:
    writer = PdfWriter()
    for path in inputs:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ToolkitError(f"Encrypted input cannot be merged without its password: {os.path.basename(path)}")
        for page in reader.pages:
            writer.add_page(page)
    with open(out, "wb") as fh:
        writer.write(fh)
    return len(writer.pages)


def parse_ranges(spec: str, page_count: int) -> list[list[int]]:
    """'1-3,5' -> [[0,1,2],[4]]; 1-based input, 0-based output groups."""
    groups = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(part)
        if lo < 1 or hi > page_count or lo > hi:
            raise ToolkitError(f"Page range '{part}' is outside 1-{page_count}.")
        groups.append(list(range(lo - 1, hi)))
    if not groups:
        raise ToolkitError("No valid page ranges given.")
    return groups


def split_pdf(src: str, spec: str, out_dir: str, base: str = "part") -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    reader = PdfReader(src)
    groups = parse_ranges(spec, len(reader.pages))
    outs = []
    for i, group in enumerate(groups, 1):
        writer = PdfWriter()
        for idx in group:
            writer.add_page(reader.pages[idx])
        out = os.path.join(out_dir, f"{base}-{i}.pdf")
        with open(out, "wb") as fh:
            writer.write(fh)
        outs.append(out)
    return outs


def rotate_pdf(src: str, out: str, angle: int, spec: str | None = None) -> None:
    if angle % 90 != 0:
        raise ToolkitError("Rotation must be a multiple of 90 degrees.")
    reader = PdfReader(src)
    writer = PdfWriter()
    targets = set()
    if spec:
        for g in parse_ranges(spec, len(reader.pages)):
            targets.update(g)
    else:
        targets = set(range(len(reader.pages)))
    for i, page in enumerate(reader.pages):
        if i in targets:
            page.rotate(angle)
        writer.add_page(page)
    with open(out, "wb") as fh:
        writer.write(fh)


# ------------------------------------------------------- compression/metadata

@dataclass
class CompressResult:
    in_bytes: int
    out_bytes: int

    @property
    def ratio(self) -> float:
        return self.out_bytes / self.in_bytes if self.in_bytes else 1.0


def compress_pdf(src: str, out: str) -> CompressResult:
    with pikepdf.open(src) as pdf:
        pdf.save(out, recompress_flate=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return CompressResult(os.path.getsize(src), os.path.getsize(out))


def scrub_metadata(src: str, out: str) -> None:
    with pikepdf.open(src) as pdf:
        try:
            del pdf.docinfo[:]
        except Exception:
            for key in list(pdf.docinfo.keys()):
                del pdf.docinfo[key]
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                for key in list(meta.keys()):
                    del meta[key]
        except Exception:
            pass
        pdf.save(out)


# --------------------------------------------------------------- encryption

def remove_password(src: str, out: str, password: str) -> None:
    """Removes encryption only when the correct password is supplied.

    Never attempts to crack; a wrong password raises ToolkitError.
    """
    password_error = getattr(pikepdf, "PasswordError", None)
    try:
        pdf = pikepdf.open(src, password=password)
    except Exception as e:
        is_password = (password_error is not None and isinstance(e, password_error)) \
            or "password" in str(e).lower()
        if is_password:
            raise ToolkitError("Wrong password. The file was not changed.")
        raise
    with pdf:
        pdf.save(out)


def add_password(src: str, out: str, password: str) -> None:
    with pikepdf.open(src) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(owner=password, user=password, R=6))


# ------------------------------------------------------------------ rendering

def pdf_to_jpeg(src: str, out_dir: str, dpi: int = 150, quality: int = 90,
                base: str | None = None, on_page=None) -> list[str]:
    if dpi < 50 or dpi > 600:
        raise ToolkitError("DPI must be between 50 and 600.")
    os.makedirs(out_dir, exist_ok=True)
    pdf = pdfium.PdfDocument(src)
    stem = base or os.path.splitext(os.path.basename(src))[0]
    outs = []
    for i in range(len(pdf)):
        pil = pdf[i].render(scale=dpi / 72).to_pil().convert("RGB")
        out = os.path.join(out_dir, f"{stem}-p{i+1}.jpg")
        pil.save(out, "JPEG", quality=quality)
        outs.append(out)
        if on_page:
            on_page(i + 1)
    pdf.close()
    return outs


# ------------------------------------------------------------------ watermark

def _text_overlay(page_w: float, page_h: float, text: str, opacity: float,
                  font_size: int) -> io.BytesIO:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setFillAlpha(opacity)
    c.setFillColor(Color(0.45, 0.45, 0.45))
    c.setFont("Helvetica-Bold", font_size)
    c.saveState()
    c.translate(page_w / 2, page_h / 2)
    c.rotate(45)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    c.save()
    buf.seek(0)
    return buf


def _image_overlay(page_w: float, page_h: float, image_path: str, opacity: float,
                   scale: float) -> io.BytesIO:
    im = Image.open(image_path).convert("RGBA")
    alpha = im.getchannel("A").point(lambda a: int(a * opacity))
    im.putalpha(alpha)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    im.save(tmp.name)
    w = page_w * scale
    h = w * im.height / im.width
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.drawImage(tmp.name, (page_w - w) / 2, (page_h - h) / 2, w, h, mask="auto")
    c.save()
    os.unlink(tmp.name)
    buf.seek(0)
    return buf


def watermark_pdf(src: str, out: str, text: str | None = None,
                  image: str | None = None, opacity: float = 0.25,
                  font_size: int = 48, image_scale: float = 0.35) -> None:
    if not text and not image:
        raise ToolkitError("Give watermark text or an image.")
    if not 0.02 <= opacity <= 1.0:
        raise ToolkitError("Opacity must be between 0.02 and 1.")
    reader = PdfReader(src)
    writer = PdfWriter()
    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        if text:
            ov = PdfReader(_text_overlay(w, h, text, opacity, font_size)).pages[0]
            page.merge_page(ov)
        if image:
            ov = PdfReader(_image_overlay(w, h, image, opacity, image_scale)).pages[0]
            page.merge_page(ov)
        writer.add_page(page)
    with open(out, "wb") as fh:
        writer.write(fh)


def watermark_image(src: str, out: str, text: str, opacity: float = 0.35) -> None:
    im = Image.open(src).convert("RGBA")
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    size = max(16, im.width // 14)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tile = Image.new("RGBA", (tw + 40, th + 40), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((20, 20), text, font=font,
                              fill=(90, 90, 90, int(255 * opacity)))
    tile = tile.rotate(30, expand=True)
    for y in range(0, im.height, tile.height + 60):
        for x in range(0, im.width, tile.width + 60):
            layer.paste(tile, (x, y), tile)
    Image.alpha_composite(im, layer).convert("RGB").save(out)


# ------------------------------------------------------------------ redaction

@dataclass
class RedactRegion:
    page: int            # 0-based
    rect: tuple[float, float, float, float]  # PDF points: x0, y0(bottom), x1, y1


def redact_pdf(src: str, out: str, regions: list[RedactRegion], dpi: int = 200,
               verify_needle: str | None = None) -> dict:
    """Irreversible redaction by rasterising affected pages and painting the
    regions black in the pixels. The original content stream is discarded
    entirely for those pages, so nothing remains to extract underneath.

    Also drops annotations on affected pages and scrubs document metadata.
    Does NOT search embedded file attachments or images on other pages; if
    `verify_needle` is given, the output's full text layer is checked and any
    surviving occurrence raises ToolkitError instead of succeeding silently.

    Tradeoff: redacted pages become images (no selectable text, larger file).
    Untouched pages keep their original vector content.
    """
    pdf = pdfium.PdfDocument(src)
    n = len(pdf)
    by_page: dict[int, list[tuple]] = {}
    for r in regions:
        if r.page < 0 or r.page >= n:
            raise ToolkitError(f"Redaction page {r.page + 1} is outside 1-{n}.")
        by_page.setdefault(r.page, []).append(r.rect)

    reader = PdfReader(src)
    writer = PdfWriter()
    burned = 0
    for i in range(n):
        if i not in by_page:
            writer.add_page(reader.pages[i])
            continue
        scale = dpi / 72
        pil = pdf[i].render(scale=scale).to_pil().convert("RGB")
        draw = ImageDraw.Draw(pil)
        page_h_pt = float(reader.pages[i].mediabox.height)
        for (x0, y0, x1, y1) in by_page[i]:
            # PDF origin is bottom-left; image origin is top-left.
            draw.rectangle([x0 * scale, (page_h_pt - y1) * scale,
                            x1 * scale, (page_h_pt - y0) * scale], fill=(0, 0, 0))
        if "/Annots" in reader.pages[i]:
            del reader.pages[i]["/Annots"]
        buf = io.BytesIO()
        pil.save(buf, "JPEG", quality=95)
        buf.seek(0)
        page_w = float(reader.pages[i].mediabox.width)
        obuf = io.BytesIO()
        c = rl_canvas.Canvas(obuf, pagesize=(page_w, page_h_pt))
        c.drawImage(ImageReader(buf), 0, 0, page_w, page_h_pt)
        c.save()
        obuf.seek(0)
        writer.add_page(PdfReader(obuf).pages[0])
        burned += 1
    with open(out, "wb") as fh:
        writer.write(fh)
    pdf.close()
    _tmp = out + ".scrubtmp"
    scrub_metadata(out, _tmp)  # redaction must not leave the text in metadata
    os.replace(_tmp, out)
    if verify_needle:
        # 0) refuse silently-incomplete jobs: annotations / embedded attachments
        hits = _needle_in_extras(src, verify_needle)
        if hits:
            if os.path.exists(out):
                os.unlink(out)
            raise ToolkitError(
                "The text also appears in " + " and ".join(hits) +
                ", which this tool cannot redact. Nothing was changed.")
        # 1) pre-burn coverage: every located occurrence must intersect a region
        def _overlap(a, b):
            return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
        missed = [f for f in find_text_rects(src, verify_needle)
                  if not any(f.page == r.page and _overlap(f.rect, r.rect) for r in regions)]
        if missed:
            os.unlink(out)
            raise ToolkitError(
                f"Redaction failed: {len(missed)} occurrence(s) of the text are not "
                "covered by the given regions. No output file was kept.")
        # 2) post-burn: nothing extractable anywhere in the output
        remaining = "\n".join((p.extract_text() or "") for p in PdfReader(out).pages)
        if verify_needle in remaining:
            os.unlink(out)
            raise ToolkitError(
                "Redaction failed: the text is still present after processing. "
                "No output file was kept.")
    return {"pages_rasterised": burned, "pages_total": n}


def redact_phrases(src: str, out: str, phrases: list[str], dpi: int = 200) -> dict:
    """Blacks out every match of each phrase and proves it.

    Refuses scans with no text layer, phrases that also sit in annotations or
    attachments, and any result where a phrase is still extractable. Returns
    the number of matches covered and the pages rebuilt."""
    reader = PdfReader(src)
    if not any((p.extract_text() or "").strip() for p in reader.pages):
        raise ToolkitError("This PDF has no text layer (it looks like a scan). "
                           "Make it searchable with OCR first, then redact.")
    for phrase in phrases:
        hits = _needle_in_extras(src, phrase)
        if hits:
            raise ToolkitError(f"'{phrase}' also appears in " + " and ".join(hits) +
                               ", which this tool cannot redact. Nothing was changed.")
    regions: list[RedactRegion] = []
    for phrase in phrases:
        regions.extend(find_text_rects(src, phrase))
    if not regions:
        raise ToolkitError("None of those words or phrases were found in the text.")
    matches = sum((p.extract_text() or "").count(ph) for p in reader.pages for ph in phrases)
    res = redact_pdf(src, out, regions, dpi=dpi)

    def _overlap(a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
    remaining = "\n".join((p.extract_text() or "") for p in PdfReader(out).pages)
    for phrase in phrases:
        missed = [f for f in find_text_rects(src, phrase)
                  if not any(f.page == r.page and _overlap(f.rect, r.rect) for r in regions)]
        if missed or phrase in remaining:
            os.unlink(out)
            raise ToolkitError("Redaction could not be verified, so no file was kept.")
    return {"matches": max(matches, len(regions)), "pages_rasterised": res["pages_rasterised"]}


def _needle_in_extras(src: str, needle: str) -> list[str]:
    """Where else does the needle hide? Returns names of un-redactable carriers."""
    hits = []
    reader = PdfReader(src)
    for page in reader.pages:
        for annot in (page.get("/Annots") or []):
            obj = annot.get_object()
            for key in ("/Contents", "/T", "/Subj", "/NM"):
                val = obj.get(key)
                if val and needle in str(val):
                    hits.append("page annotations")
                    break
            else:
                continue
            break
        if "page annotations" in hits:
            break
    try:
        for name, contents in reader.attachments.items():
            data = contents[0] if isinstance(contents, list) else contents
            if needle in name or needle.encode("utf-8", "ignore") in data:
                hits.append("embedded file attachments")
                break
    except Exception:
        pass
    return hits


def find_text_rects(src: str, needle: str) -> list[RedactRegion]:
    """Locate occurrences of `needle` as redaction regions, using text
    positions from pypdf's extraction visitor. Approximate height: 1.2x font.
    """
    found: list[RedactRegion] = []
    reader = PdfReader(src)
    for pno, page in enumerate(reader.pages):
        hits = []

        def visitor(text, cm, tm, font_dict, font_size):
            if needle and needle in text:
                x, y = tm[4], tm[5]
                w = font_size * 0.6 * len(text)
                h = font_size * 1.2
                hits.append((x, y - h * 0.25, x + w, y + h * 0.75))

        page.extract_text(visitor_text=visitor)
        found.extend(RedactRegion(pno, r) for r in hits)
    return found


# ------------------------------------------------------------ text replacement

def replace_text(src: str, out: str, old: str, new: str) -> int:
    """Naive literal-string replacement in page content streams.

    Only works when `old` appears as a contiguous literal string in a simple
    encoding. No paragraph reflow: `new` should be similar length or shorter.
    Returns the number of replacements (0 means nothing matched).
    """
    if len(new) > len(old) + 8:
        raise ToolkitError("Replacement is much longer than the original; layout will break.")
    count = 0
    pat = re.compile(rb"\((?:\\.|[^\\()])*" + re.escape(old.encode("latin-1", "ignore")) +
                     rb"(?:\\.|[^\\()])*\)")
    pdf = pikepdf.open(src)
    with pdf:
        for page in pdf.pages:
            contents = page.Contents
            streams = contents if isinstance(contents, pikepdf.Array) else [contents]
            for stream in streams:
                data = stream.read_bytes()
                if old.encode("latin-1", "ignore") not in data:
                    continue

                def _sub(m):
                    nonlocal count
                    count += 1
                    return m.group(0).replace(old.encode("latin-1", "ignore"),
                                              new.encode("latin-1", "ignore"))

                stream.write(pat.sub(_sub, data))
        pdf.save(out)
    return count


# --------------------------------------------------------------- office / OCR

def office_to_pdf(src: str, out_dir: str, timeout: int = 120) -> str:
    os.makedirs(out_dir, exist_ok=True)
    """DOCX/XLSX/PPTX/ODT -> PDF via LibreOffice headless. Layout is good but
    not a pixel-perfect match for Microsoft Office rendering."""
    if not shutil.which("soffice"):
        raise ToolkitError("LibreOffice is not available on this machine.")
    proc = subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", out_dir, src],
        capture_output=True, text=True, timeout=timeout)
    stem = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(out_dir, stem + ".pdf")
    if proc.returncode != 0 or not os.path.exists(out):
        raise ToolkitError(f"Conversion failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return out


def ocr_pdf(src: str, out: str, language: str = "eng", dpi: int = 200,
            timeout: int = 300, on_page=None) -> None:
    """Adds a searchable text layer to scanned pages using Tesseract directly.

    Pages are rendered to images, each is OCRed, and the results are combined
    into a new PDF. No Ghostscript or OCRmyPDF in the path (licence note in
    docs/dependencies.md). Tradeoff: every page is rebuilt from its render,
    so vector content becomes an image and page dimensions follow the render
    resolution rather than the original points. Only languages installed on
    the machine are valid; check with list_ocr_languages.
    """
    if not shutil.which("tesseract"):
        raise ToolkitError("Tesseract is not available on this machine.")
    if language not in list_ocr_languages():
        raise ToolkitError(f"OCR language '{language}' is not installed. "
                           f"Available: {', '.join(list_ocr_languages())}")
    pdf = pdfium.PdfDocument(src)
    writer = PdfWriter()
    workdir = tempfile.mkdtemp(prefix="ocr-")
    try:
        for i in range(len(pdf)):
            img = pdf[i].render(scale=dpi / 72).to_pil().convert("RGB")
            png = os.path.join(workdir, f"p{i}.png")
            img.save(png)
            base = os.path.join(workdir, f"p{i}")
            proc = subprocess.run(
                ["tesseract", png, base, "-l", language, "pdf"],
                capture_output=True, text=True, timeout=timeout)
            page_pdf = base + ".pdf"
            if proc.returncode != 0 or not os.path.exists(page_pdf):
                raise ToolkitError(f"OCR failed on page {i + 1}: "
                                   f"{proc.stderr.strip()[-200:]}")
            writer.add_page(PdfReader(page_pdf).pages[0])
            if on_page:
                on_page(i + 1)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    pdf.close()
    with open(out, "wb") as fh:
        writer.write(fh)


def list_ocr_languages() -> list[str]:
    proc = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    return [l.strip() for l in proc.stdout.splitlines()[1:] if l.strip()]


# --------------------------------------------------------------- more formats

def images_to_pdf(inputs: list[str], out: str) -> int:
    """JPEG/PNG images -> one PDF, one image per page."""
    if not inputs:
        raise ToolkitError("No images given.")
    frames = [Image.open(p).convert("RGB") for p in inputs]
    frames[0].save(out, "PDF", save_all=True, append_images=frames[1:], resolution=150.0)
    return len(frames)


def pdf_to_docx_text(src: str, out: str) -> int:
    """Extracts the text of a PDF into a DOCX (one heading per page break).

    Text flow only: fonts, columns, tables and images are NOT preserved.
    Returns the number of pages that had extractable text."""
    from docx import Document
    reader = PdfReader(src)
    doc = Document()
    kept = 0
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        kept += 1
        doc.add_heading(f"Page {i}", level=2)
        for line in text.splitlines():
            doc.add_paragraph(line)
    if kept == 0:
        raise ToolkitError("No extractable text found. If this is a scan, run OCR first.")
    doc.save(out)
    return kept


def text_to_docx(text: str, out: str, title: str | None = None) -> None:
    """Plain text -> simple DOCX (one paragraph per line)."""
    from docx import Document
    doc = Document()
    if title:
        doc.add_heading(title, 0)
    for line in text.splitlines():
        doc.add_paragraph(line)
    doc.save(out)
