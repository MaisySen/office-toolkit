"""Engine P2 ops, helper B (contract section E2).

Pure functions first; the job API wiring (OPERATIONS entries and the
handler map) sits at the bottom and is hooked into engine.py with one
update and one dispatch line.

Every function treats its input as hostile: real bytes are sniffed, zip
containers are checked for size before parsing, pages, rows and bytes are
capped, subprocesses run under timeout -k with a private profile, and HTML
or SVG never reaches a renderer with a remote or local reference left in it.
"""
from __future__ import annotations

import csv
import datetime as _dt
import html
import io
import json
import glob
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser

from office_toolkit.pdfops import ToolkitError

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

# ------------------------------------------------------------------ caps
TEXT_MAX_PAGES = 50          # extract-text reads the first 50 pages
OCR_MAX_PAGES = 30           # of which at most 30 are OCRed
OCR_DPI = 200
OCR_MAX_PIXELS = 40_000_000  # an image to OCR, after any downscale
EXTRACT_MAX_CHARS = 2_000_000
PDF_TO_MAX_PAGES = 200       # txt/md
PDF_TO_PNG_MAX_PAGES = 100
TABLES_MAX_PAGES = 100
TABLES_MAX = 200
EXCEL_MAX_FILES = 20
EXCEL_MAX_ROWS = 200_000     # all inputs together
EXCEL_MAX_COLS = 200
EXCEL_MAX_CELLS = 2_000_000
PII_MAX_PAGES = 200
SEGMENTS_MAX_PAGES = 50
SEGMENTS_MAX = 2000
TO_PDF_MAX_TEXT_BYTES = 5 * 1024 * 1024
CSV_MAX_ROWS_TO_PDF = 5000
RENDER_MAX_ROWS = 500
RENDER_MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
IMAGE_MAX_PIXELS = 50_000_000
ZIP_MAX_UNCOMPRESSED = 300 * 1024 * 1024
ZIP_MAX_ENTRIES = 5000
SOFFICE_TIMEOUT = 180
SOFFICE_BATCH = 40           # files per LibreOffice run in render-batch
TESSERACT_TIMEOUT = 120

# ------------------------------------------------------------- sniffing
_TEXT_EXT = {".txt", ".csv", ".md", ".markdown", ".html", ".htm", ".svg", ".tsv"}
_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp"}


def sniff_type(path: str, name_hint: str = "") -> str:
    """Returns pdf, docx, xlsx, xls, image, html, md, csv, txt or svg from the
    real bytes (the name only decides between the plain text kinds)."""
    with open(path, "rb") as fh:
        head = fh.read(4096)
    ext = os.path.splitext(name_hint or path)[1].lower()
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist()[:ZIP_MAX_ENTRIES + 1])
        except zipfile.BadZipFile:
            raise ToolkitError("That file is damaged or not a supported document.")
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        raise ToolkitError("That zip based file is not a Word or Excel document.")
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls" if ext in (".xls", "") or ext not in (".doc",) else "doc"
    if (head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM",
                         b"II*\x00", b"MM\x00*"))
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")):
        return "image"
    if b"\x00" in head:
        raise ToolkitError("That file type is not supported.")
    low = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if ext == ".svg" or low.startswith(b"<svg") or (low.startswith(b"<?xml") and b"<svg" in low):
        return "svg"
    if ext in (".html", ".htm") or low.startswith((b"<!doctype html", b"<html")):
        return "html"
    if ext in (".md", ".markdown"):
        return "md"
    if ext in (".csv", ".tsv"):
        return "csv"
    return "txt"


def read_text_file(path: str, max_bytes: int = TO_PDF_MAX_TEXT_BYTES) -> str:
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ToolkitError(f"Text files up to {max_bytes // (1024 * 1024)} MB are supported.")
    with open(path, "rb") as fh:
        data = fh.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise ToolkitError("That file is not readable text.")
    if b"\x00" in data:
        raise ToolkitError("That file is not plain text.")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def check_zip(path: str, max_total: int = ZIP_MAX_UNCOMPRESSED) -> None:
    """Refuses zip bombs and odd containers before any parser opens them."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        raise ToolkitError("That file is damaged.")
    if len(infos) > ZIP_MAX_ENTRIES:
        raise ToolkitError("That document has too many parts.")
    total = 0
    for i in infos:
        total += i.file_size
        if i.file_size > 50 * max(i.compress_size, 1) and i.file_size > 20 * 1024 * 1024:
            raise ToolkitError("That document expands to an unsafe size.")
    if total > max_total:
        raise ToolkitError("That document expands to an unsafe size.")


def open_image(path: str, max_pixels: int = IMAGE_MAX_PIXELS):
    import warnings
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(path)
        w, h = img.size
        if w * h > max_pixels:
            raise ToolkitError("That image is too large.")
        img.load()
    except ToolkitError:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ToolkitError("That image is too large.")
    except Exception:
        raise ToolkitError("That image could not be read.")
    return img


def safe_name(name: str, fallback: str = "file", max_len: int = 80) -> str:
    """A file name safe in a zip on every system: no paths, no control or
    reserved characters, not empty, not a reserved device name."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name):
        name = fallback
    return name[:max_len].rstrip(" .") or fallback


def dedupe_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        key = n.lower()
        if key in seen:
            seen[key] += 1
            stem, ext = os.path.splitext(n)
            cand = f"{stem} ({seen[key]}){ext}"
            while cand.lower() in seen:
                seen[key] += 1
                cand = f"{stem} ({seen[key]}){ext}"
            seen[cand.lower()] = 1
            out.append(cand)
        else:
            seen[key] = 1
            out.append(n)
    return out


def zip_files(pairs: list[tuple[str, str]], out: str) -> None:
    """pairs: (path on disk, name in zip). Names are made safe and unique."""
    names = dedupe_names([safe_name(n) for _, n in pairs])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for (p, _), n in zip(pairs, names):
            z.write(p, n)


def _run(cmd: list[str], timeout: int, cwd: str | None = None, env: dict | None = None):
    """Runs a command (argument list, never a shell string) with a hard kill."""
    full = ["timeout", "-k", "5", str(timeout)] + cmd
    try:
        return subprocess.run(full, capture_output=True, timeout=timeout + 15, cwd=cwd, env=env,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolkitError("The file took too long to process.")


# ------------------------------------------------------- HTML sanitising
_ALLOWED_TAGS = {
    "html", "head", "body", "title", "meta", "style", "div", "span", "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6", "b", "strong", "i", "em", "u", "s", "strike",
    "sub", "sup", "small", "big", "mark", "code", "pre", "kbd", "samp", "blockquote", "q",
    "ul", "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "tr", "th",
    "td", "caption", "colgroup", "col", "img", "a", "font", "center", "section",
    "article", "header", "footer", "main", "aside", "nav", "figure", "figcaption",
    "address", "abbr", "cite", "del", "ins", "time", "label",
}
_VOID = {"br", "hr", "img", "meta", "col"}
_DROP_WITH_CONTENT = {"script", "iframe", "object", "embed", "frame", "frameset", "applet",
                      "noscript", "template", "svg", "math", "form", "input", "button",
                      "select", "textarea", "video", "audio", "source", "track", "canvas",
                      "link", "base", "portal", "xml"}
_ALLOWED_ATTRS = {"class", "id", "style", "align", "valign", "width", "height", "colspan",
                  "rowspan", "alt", "title", "border", "cellpadding", "cellspacing",
                  "bgcolor", "color", "face", "size", "dir", "lang", "start", "type",
                  "charset", "src", "href", "name"}
_URL_RE = re.compile(r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
_DATA_IMG = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\s]+$", re.I)


def _clean_css(css: str) -> str:
    css = re.sub(r"(?is)@import[^;]*;?", "", css)
    css = re.sub(r"(?is)@font-face\s*\{[^}]*\}", "", css)
    css = re.sub(r"(?i)expression\s*\(", "(", css)
    css = re.sub(r"(?i)behaviou?r\s*:", "x-blocked:", css)

    def _url(m):
        return m.group(0) if _DATA_IMG.match(m.group(2).strip()) else "none"
    return _URL_RE.sub(_url, css)


class _Sanitiser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0        # depth inside a dropped element
        self.in_style = False
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag in _DROP_WITH_CONTENT and tag not in _VOID:
                self.skip += 1
            return
        if tag in _DROP_WITH_CONTENT:
            self.blocked += 1
            if tag not in ("link", "base", "source", "track", "input"):
                self.skip = 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        if tag == "meta":
            d = {k.lower(): (v or "") for k, v in attrs}
            if "charset" in d:
                self.out.append('<meta charset="utf-8">')
            else:
                self.blocked += 1   # refresh, csp and the rest
            return
        parts = [tag]
        for k, v in attrs:
            k = k.lower()
            v = v or ""
            if k not in _ALLOWED_ATTRS or k.startswith("on"):
                continue
            if k == "src":
                if tag != "img" or not _DATA_IMG.match(v.strip()):
                    self.blocked += 1
                    continue
            if k == "href":
                # links stay as text; nothing is fetched or embedded from them
                if not re.match(r"(?i)^(https?:|mailto:|#)", v.strip()):
                    continue
            if k == "style":
                v = _clean_css(v)
            parts.append(f'{k}="{html.escape(v, quote=True)}"')
        if tag == "img" and not any(p.startswith("src=") for p in parts[1:]):
            return
        self.out.append("<" + " ".join(parts) + (">"))
        if tag == "style":
            self.in_style = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() == "style":
            self.in_style = False

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag in _DROP_WITH_CONTENT:
                self.skip -= 1
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID:
            self.out.append(f"</{tag}>")
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_style:
            self.out.append(_clean_css(data).replace("</", "<\\/"))
        else:
            self.out.append(html.escape(data, quote=False))

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


def sanitise_html(text: str) -> tuple[str, int]:
    """Rebuilds HTML from an allow list: no scripts, frames, objects, forms,
    links to stylesheets, base or refresh tags, no event handlers, and no
    src or CSS url() other than inline data: images. Returns the HTML and
    how many references were blocked."""
    p = _Sanitiser()
    p.feed(text)
    p.close()
    body = "".join(p.out)
    if "<html" not in body.lower():
        body = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
                + body + "</body></html>")
    elif "<meta charset" not in body.lower():
        body = re.sub(r"(?i)<html([^>]*)>", r'<html\1><head><meta charset="utf-8"></head>',
                      body, count=1) if "<head" not in body.lower() else \
            re.sub(r"(?i)<head([^>]*)>", r'<head\1><meta charset="utf-8">', body, count=1)
    return body, p.blocked


_BASE_CSS = ("<style>body{font-family:'Noto Sans','Noto Sans Devanagari',"
             "'Droid Sans Fallback',sans-serif;font-size:11pt}"
             "table{border-collapse:collapse}td,th{border:1px solid #999;padding:2pt 4pt}"
             "pre{white-space:pre-wrap;font-family:monospace}</style>")


def markdown_to_html(text: str) -> str:
    import markdown  # Python-Markdown, BSD
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"],
                             output_format="html")
    return f"<!DOCTYPE html><html><head><meta charset=\"utf-8\">{_BASE_CSS}</head><body>{body}</body></html>"


def text_to_html(text: str) -> str:
    return (f"<!DOCTYPE html><html><head><meta charset=\"utf-8\">{_BASE_CSS}</head><body>"
            f"<pre>{html.escape(text)}</pre></body></html>")


def read_csv_rows(text: str, max_rows: int, max_cols: int = EXCEL_MAX_COLS) -> list[list[str]]:
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    csv.field_size_limit(1_000_000)
    rows = []
    try:
        for row in csv.reader(io.StringIO(text), dialect):
            if len(rows) >= max_rows:
                raise ToolkitError(f"Files up to {max_rows:,} rows are supported.")
            if len(row) > max_cols:
                raise ToolkitError(f"Files up to {max_cols} columns are supported.")
            rows.append(row)
    except csv.Error:
        raise ToolkitError("That CSV file could not be read.")
    return rows


def csv_to_html(text: str) -> str:
    rows = read_csv_rows(text, CSV_MAX_ROWS_TO_PDF, 60)
    out = [f"<!DOCTYPE html><html><head><meta charset=\"utf-8\">{_BASE_CSS}"
           "<style>body{font-size:8pt}</style></head><body><table>"]
    for i, r in enumerate(rows):
        cell = "th" if i == 0 else "td"
        out.append("<tr>" + "".join(f"<{cell}>{html.escape(c)}</{cell}>" for c in r) + "</tr>")
    out.append("</table></body></html>")
    return "".join(out)


# ------------------------------------------------------------ LibreOffice

def soffice_convert(srcs: list[str], out_dir: str, timeout: int = SOFFICE_TIMEOUT,
                    infilter: str | None = None) -> list[str]:
    """Converts files to PDF in one LibreOffice run with a private profile
    (so two jobs never share one) and returns the PDFs in input order."""
    if not shutil.which("soffice"):
        raise ToolkitError("LibreOffice is not available on this machine.")
    os.makedirs(out_dir, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="lo-profile-")
    try:
        cmd = ["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
               "--norestore", "--nolockcheck", "--nodefault", "--nologo"]
        if infilter:
            cmd.append(f"--infilter={infilter}")
        cmd += ["--convert-to", "pdf", "--outdir", out_dir] + srcs
        env = dict(os.environ, HOME=profile, SAL_USE_VCLPLUGIN="svp")
        # no proxy and an unroutable one: belt and braces if anything slips through
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env[k] = "http://127.0.0.1:9"
        env["no_proxy"] = ""
        proc = _run(cmd, timeout, env=env)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    outs = []
    for s in srcs:
        o = os.path.join(out_dir, os.path.splitext(os.path.basename(s))[0] + ".pdf")
        if not os.path.exists(o):
            if proc.returncode == 124 or proc.returncode == 137:
                raise ToolkitError("The file took too long to convert.")
            raise ToolkitError("The file could not be converted to PDF.")
        outs.append(o)
    return outs


def _pdf_pages(path: str) -> int:
    import pypdfium2 as pdfium
    d = pdfium.PdfDocument(path)
    try:
        return len(d)
    finally:
        d.close()


def _html_file_to_pdf(html_text: str, out: str, work: str, stem: str = "doc") -> int:
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, f"{stem}.html")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    made = soffice_convert([src], os.path.join(work, "pdf"), infilter="HTML (StarWriter)")[0]
    shutil.move(made, out)
    return _pdf_pages(out)


# ------------------------------------------------------------------ SVG

def sanitise_svg(text: str) -> str:
    """Accepts an SVG only without DOCTYPE or entities, scripts, foreign
    objects or any reference that is not internal (#id) or an inline image."""
    import xml.etree.ElementTree as ET
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    if re.search(r"(?i)<!DOCTYPE|<!ENTITY", text):
        raise ToolkitError("SVG files with a DOCTYPE or entities are not supported.")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        raise ToolkitError("That SVG file could not be read.")
    if not root.tag.endswith("svg"):
        raise ToolkitError("That file is not an SVG image.")
    bad = {"script", "foreignObject", "iframe", "image", "use", "feImage", "a", "style"}
    keep_img = re.compile(_DATA_IMG.pattern, re.I)
    for parent in list(root.iter()):
        for child in list(parent):
            local = child.tag.rsplit("}", 1)[-1]
            href = next((v for k, v in child.attrib.items() if k.rsplit("}", 1)[-1] == "href"), None)
            if local in ("image", "feImage") and href and keep_img.match(href.strip()):
                continue
            if local in ("use",) and href and href.strip().startswith("#"):
                continue
            if local == "style" and child.text and "url(" not in child.text.lower() \
                    and "@import" not in child.text.lower():
                continue
            if local in bad:
                parent.remove(child)
    for el in root.iter():
        for k in list(el.attrib):
            lk = k.rsplit("}", 1)[-1].lower()
            v = el.attrib[k]
            if lk.startswith("on"):
                del el.attrib[k]
            elif lk == "href" and not (v.strip().startswith("#") or keep_img.match(v.strip())):
                del el.attrib[k]
            elif lk == "style":
                el.attrib[k] = _clean_css(v)
            elif "url(" in v.lower():
                el.attrib[k] = _URL_RE.sub(lambda m: m.group(0) if m.group(2).startswith("#") else "none", v)
    return ET.tostring(root, encoding="unicode")


# --------------------------------------------------------------- to-pdf

def to_pdf(src: str, out: str, work: str, name_hint: str = "") -> dict:
    """HTML, Markdown, CSV, text, images or SVG to one PDF. Facts: pages."""
    kind = sniff_type(src, name_hint)
    if kind == "pdf":
        raise ToolkitError("That file is already a PDF.")
    if kind in ("docx", "xlsx", "xls", "doc"):
        raise ToolkitError("Use Convert to PDF for Word, Excel and PowerPoint files.")
    if kind == "image":
        img = open_image(src)
        from PIL import ImageSequence
        frames = []
        for i, fr in enumerate(ImageSequence.Iterator(img)):
            if i >= 50:
                break
            fr = fr.convert("RGBA") if fr.mode in ("P", "LA", "RGBA") else fr
            if fr.mode == "RGBA":
                bg = __import__("PIL.Image", fromlist=["Image"]).new("RGB", fr.size, "white")
                bg.paste(fr, mask=fr.split()[-1])
                fr = bg
            frames.append(fr.convert("RGB"))
            if getattr(img, "format", "") in ("GIF", "WEBP"):
                break   # first frame of an animation
        frames[0].save(out, "PDF", save_all=True, append_images=frames[1:], resolution=150.0)
        return {"pages": len(frames)}
    if kind == "svg":
        clean = sanitise_svg(read_text_file(src, 10 * 1024 * 1024))
        os.makedirs(work, exist_ok=True)
        p = os.path.join(work, "image.svg")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(clean)
        made = soffice_convert([p], os.path.join(work, "pdf"))[0]
        shutil.move(made, out)
        return {"pages": _pdf_pages(out)}
    text = read_text_file(src)
    if kind == "html":
        doc, blocked = sanitise_html(text)
        pages = _html_file_to_pdf(doc, out, work)
        return {"pages": pages, "remote_blocked": blocked}
    if kind == "md":
        doc, blocked = sanitise_html(markdown_to_html(text))
        return {"pages": _html_file_to_pdf(doc, out, work), "remote_blocked": blocked}
    if kind == "csv":
        return {"pages": _html_file_to_pdf(csv_to_html(text), out, work)}
    return {"pages": _html_file_to_pdf(text_to_html(text), out, work)}


# ------------------------------------------------------- PDF reading helpers

def _open_pdf(path: str):
    """pypdfium2 document; encrypted PDFs get a plain refusal."""
    import pypdfium2 as pdfium
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError as e:
        if "password" in str(e).lower():
            raise ToolkitError("This PDF is password protected. Remove the password first.")
        raise ToolkitError("The PDF could not be parsed (corrupt or unsupported).")


def _page_text(page) -> str:
    tp = page.get_textpage()
    try:
        return tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
    finally:
        tp.close()


def _xlsx_safe(v):
    """Values written to a spreadsheet: text that looks like a formula stays text."""
    if isinstance(v, str):
        v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", v)[:32767]
    return v


def _write_sheet_rows(ws, rows):
    from openpyxl.cell import WriteOnlyCell
    for r in rows:
        cells = []
        for v in r:
            v = _xlsx_safe(v)
            c = WriteOnlyCell(ws, value=v)
            if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) and len(v) > 1:
                c.data_type = "s"
            cells.append(c)
        ws.append(cells)


def _sheet_title(name: str, used: set[str]) -> str:
    t = re.sub(r"[\[\]\*\?/\\:]", " ", name).strip().strip("'") or "Sheet"
    t = t[:31]
    base, n = t, 1
    while t.lower() in used:
        n += 1
        suffix = f" ({n})"
        t = base[:31 - len(suffix)] + suffix
    used.add(t.lower())
    return t


def find_tables(src: str, max_pages: int = TABLES_MAX_PAGES, on_page=None) -> list[dict]:
    """Tables found by pdfplumber (MIT), page by page with its cache dropped."""
    import pdfplumber
    tables = []
    try:
        pdf = pdfplumber.open(src)
    except Exception as e:
        if "password" in str(e).lower() or "encrypt" in str(e).lower():
            raise ToolkitError("This PDF is password protected. Remove the password first.")
        raise ToolkitError("The PDF could not be parsed (corrupt or unsupported).")
    with pdf:
        n = len(pdf.pages)
        if n > max_pages:
            raise ToolkitError(f"Table finding takes PDFs up to {max_pages} pages; this has {n}.")
        for i, page in enumerate(pdf.pages, 1):
            for t in page.extract_tables():
                rows = [[("" if c is None else str(c)) for c in row] for row in t]
                rows = [r for r in rows if any(c.strip() for c in r)]
                if len(rows) >= 2 or (rows and len(rows[0]) >= 2):
                    tables.append({"page": i, "rows": rows})
                    if len(tables) > TABLES_MAX:
                        raise ToolkitError(f"More than {TABLES_MAX} tables found; split the PDF first.")
            page.flush_cache()
            page.close()
            if on_page:
                on_page(i, n)
    return tables


def pdf_tables(src: str, out: str, on_page=None) -> dict:
    """PDF -> xlsx, one sheet per table. Facts: tables."""
    tables = find_tables(src, on_page=on_page)
    if not tables:
        raise ToolkitError("No tables were found in this PDF. Scanned pages need OCR first.")
    _tables_to_xlsx(tables, out)
    return {"tables": len(tables)}


def _tables_to_xlsx(tables, out):
    from openpyxl import Workbook
    wb = Workbook(write_only=True)
    used: set[str] = set()
    for n, t in enumerate(tables, 1):
        ws = wb.create_sheet(_sheet_title(f"Table {n} (page {t['page']})", used))
        _write_sheet_rows(ws, t["rows"])
    wb.save(out)


def pdf_to(src: str, target: str, work: str, on_page=None) -> dict:
    """PDF -> txt, md, csv, xlsx or png (a zip of pages). Returns facts plus
    the output under "_output": {"path", "name", "content_type"}."""
    os.makedirs(work, exist_ok=True)
    if target in ("csv", "xlsx"):
        tables = find_tables(src, on_page=on_page)
        if not tables:
            raise ToolkitError("No tables were found in this PDF. Scanned pages need OCR first.")
        if target == "xlsx":
            out = os.path.join(work, "tables.xlsx")
            _tables_to_xlsx(tables, out)
            return {"tables": len(tables), "_output": {"path": out, "name": "tables.xlsx",
                                                       "content_type": CONTENT_TYPES["xlsx"]}}
        out = os.path.join(work, "tables.csv")
        with open(out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            for n, t in enumerate(tables):
                if n:
                    w.writerow([])
                for r in t["rows"]:
                    w.writerow([("'" + c) if c[:1] in ("=", "+", "-", "@") and len(c) > 1 else c
                                for c in r])
        return {"tables": len(tables), "_output": {"path": out, "name": "tables.csv",
                                                   "content_type": CONTENT_TYPES["csv"]}}
    doc = _open_pdf(src)
    try:
        n = len(doc)
        if target == "png":
            if n > PDF_TO_PNG_MAX_PAGES:
                raise ToolkitError(f"PDF to images takes PDFs up to {PDF_TO_PNG_MAX_PAGES} pages.")
            paths = []
            for i in range(n):
                page = doc[i]
                bm = page.render(scale=150 / 72)
                img = bm.to_pil()
                p = os.path.join(work, f"page-{i + 1:03d}.png")
                img.save(p, optimize=False)
                img.close(); bm.close(); page.close()
                paths.append(p)
                if on_page:
                    on_page(i + 1, n)
            out = os.path.join(work, "pages.zip")
            zip_files([(p, os.path.basename(p)) for p in paths], out)
            return {"pages": n, "_output": {"path": out, "name": "pages.zip",
                                            "content_type": CONTENT_TYPES["zip"]}}
        if target not in ("txt", "md"):
            raise ToolkitError("Choose txt, md, csv, xlsx or png.")
        if n > PDF_TO_MAX_PAGES:
            raise ToolkitError(f"PDF to text takes PDFs up to {PDF_TO_MAX_PAGES} pages.")
        out = os.path.join(work, f"document.{target}")
        chars = 0
        with open(out, "w", encoding="utf-8") as fh:
            for i in range(n):
                page = doc[i]
                text = _page_text(page).strip()
                page.close()
                chars += len(text)
                if target == "txt":
                    if i:
                        fh.write("\n\f\n")
                    fh.write(text)
                else:
                    fh.write(f"## Page {i + 1}\n\n")
                    for para in re.split(r"\n\s*\n", text):
                        para = re.sub(r"\s*\n\s*", " ", para).strip()
                        if para:
                            fh.write(_md_escape(para) + "\n\n")
                if on_page:
                    on_page(i + 1, n)
        if chars == 0:
            raise ToolkitError("No text found. If this is a scan, run Make searchable (OCR) first.")
        return {"pages": n, "chars": chars,
                "_output": {"path": out, "name": f"document.{target}",
                            "content_type": CONTENT_TYPES[target]}}
    finally:
        doc.close()


def _md_escape(s: str) -> str:
    s = re.sub(r"^([#>\-\+\*]|\d+\.)(\s)", r"\\\1\2", s)
    return s.replace("<", "&lt;")


# ------------------------------------------------------------------ Excel

def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def read_workbook(path: str, name_hint: str = "") -> list[tuple[str, list[list]]]:
    """Returns [(sheet name, rows)] for xlsx (values, not formulas), xls or csv."""
    kind = sniff_type(path, name_hint)
    sheets: list[tuple[str, list[list]]] = []
    if kind == "xlsx":
        check_zip(path)
        from openpyxl import load_workbook
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except Exception:
            raise ToolkitError("That Excel file could not be read.")
        try:
            for ws in wb.worksheets:
                rows = []
                for r in ws.iter_rows(values_only=True):
                    if len(rows) >= EXCEL_MAX_ROWS:
                        raise ToolkitError(f"Sheets up to {EXCEL_MAX_ROWS:,} rows are supported.")
                    r = list(r)
                    if len(r) > EXCEL_MAX_COLS:
                        if any(x is not None for x in r[EXCEL_MAX_COLS:]):
                            raise ToolkitError(
                                f"Sheets up to {EXCEL_MAX_COLS} columns are supported.")
                        r = r[:EXCEL_MAX_COLS]
                    rows.append(r)
                while rows and all(v is None or v == "" for v in rows[-1]):
                    rows.pop()
                sheets.append((ws.title, rows))
        finally:
            wb.close()
    elif kind == "xls":
        import xlrd  # BSD
        try:
            book = xlrd.open_workbook(path, on_demand=True)
        except Exception:
            raise ToolkitError("That Excel file could not be read.")
        try:
            for sh in book.sheets():
                if sh.nrows > EXCEL_MAX_ROWS or sh.ncols > EXCEL_MAX_COLS:
                    raise ToolkitError("That sheet is too large.")
                rows = []
                for ri in range(sh.nrows):
                    row = []
                    for ci in range(sh.ncols):
                        c = sh.cell(ri, ci)
                        if c.ctype == xlrd.XL_CELL_DATE:
                            try:
                                row.append(xlrd.xldate.xldate_as_datetime(c.value, book.datemode))
                            except Exception:
                                row.append(c.value)
                        elif c.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                            row.append(None)
                        elif c.ctype == xlrd.XL_CELL_ERROR:
                            row.append(None)
                        else:
                            row.append(c.value)
                    rows.append(row)
                sheets.append((sh.name, rows))
        finally:
            book.release_resources()
    elif kind in ("csv", "txt"):
        rows = read_csv_rows(read_text_file(path, 50 * 1024 * 1024), EXCEL_MAX_ROWS)
        sheets.append(("Sheet1", [list(r) for r in rows]))
    else:
        raise ToolkitError("Excel (xlsx, xls) or CSV files only.")
    return sheets


def _count_cells(sheets) -> int:
    return sum(len(r) for _, rows in sheets for r in rows)


def excel_merge(inputs: list[str], out: str, mode: str = "sheets",
                names: list[str] | None = None) -> dict:
    """2 to 20 xlsx/xls/csv -> one xlsx. sheets: every input sheet becomes a
    sheet. stack: rows under one header, columns matched by name. Facts: rows, sheets."""
    if not (2 <= len(inputs) <= EXCEL_MAX_FILES):
        raise ToolkitError(f"Merge takes 2 to {EXCEL_MAX_FILES} files.")
    from openpyxl import Workbook
    books = []
    total_rows = cells = 0
    for i, p in enumerate(inputs):
        sheets = read_workbook(p, (names or [""] * len(inputs))[i])
        total_rows += sum(len(r) for _, r in sheets)
        cells += _count_cells(sheets)
        if total_rows > EXCEL_MAX_ROWS or cells > EXCEL_MAX_CELLS:
            raise ToolkitError(f"Merging is limited to {EXCEL_MAX_ROWS:,} rows in all.")
        books.append(sheets)
    wb = Workbook(write_only=True)
    if mode == "sheets":
        used: set[str] = set()
        n = 0
        rows_out = 0
        for i, sheets in enumerate(books, 1):
            for title, rows in sheets:
                label = title if len(sheets) == 1 and title not in ("Sheet1", "Sheet", "") \
                    else f"{i} {title}"
                ws = wb.create_sheet(_sheet_title(label, used))
                _write_sheet_rows(ws, rows)
                rows_out += len(rows)
                n += 1
        wb.save(out)
        return {"rows": rows_out, "sheets": n}
    if mode != "stack":
        raise ToolkitError("Mode must be sheets or stack.")
    header: list[str] = []
    keys: dict[str, int] = {}
    blocks = []
    for sheets in books:
        for _, rows in sheets:
            rows = [r for r in rows if any(v not in (None, "") for v in r)]
            if not rows:
                continue
            hdr = [_cell_text(h).strip() for h in rows[0]]
            idx = []
            for j, h in enumerate(hdr):
                k = re.sub(r"\s+", " ", h).lower() or f"column {j + 1}"
                if k not in keys:
                    keys[k] = len(header)
                    header.append(h or f"Column {j + 1}")
                idx.append(keys[k])
            blocks.append((idx, rows[1:]))
    if len(header) > EXCEL_MAX_COLS:
        raise ToolkitError(f"Up to {EXCEL_MAX_COLS} different columns can be stacked.")
    ws = wb.create_sheet("Merged")
    _write_sheet_rows(ws, [header])
    count = 0
    for idx, rows in blocks:
        def remap(r):
            o = [None] * len(header)
            for j, v in enumerate(r[:len(idx)]):
                o[idx[j]] = v
            return o
        _write_sheet_rows(ws, (remap(r) for r in rows))
        count += len(rows)
    wb.save(out)
    return {"rows": count, "sheets": 1}


_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$"), "ymd"),
    (re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$"), "dmy"),
    (re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2})$"), "dmy2"),
    (re.compile(r"^(\d{1,2})[ \-]([A-Za-z]{3,9})[ \-,]+(\d{4})$"), "dMy"),
    (re.compile(r"^([A-Za-z]{3,9}) (\d{1,2}),? (\d{4})$"), "Mdy"),
]
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(s: str):
    """Text to a date, Australian day first for numeric forms. None if not a date."""
    s = s.strip()
    for rx, kind in _DATE_PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        try:
            if kind == "ymd":
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
            elif kind in ("dmy", "dmy2"):
                d, mo, y = int(m[1]), int(m[2]), int(m[3])
                if kind == "dmy2":
                    y += 2000 if y < 70 else 1900
            elif kind == "dMy":
                d, y = int(m[1]), int(m[3])
                mo = _MONTHS.get(m[2][:3].lower())
            else:
                mo = _MONTHS.get(m[1][:3].lower())
                d, y = int(m[2]), int(m[3])
            if not mo:
                return None
            return _dt.date(y, mo, d)
        except ValueError:
            return None
    return None


_NUM_RE = re.compile(r"^-?(0|[1-9]\d{0,14})(\.\d{1,10})?$")


def excel_clean(src: str, out: str, trim: bool = True, dedupe: bool = True,
                dates: str = "keep", drop_empty: bool = True, name_hint: str = "") -> dict:
    """Cleans every sheet: trims spaces, removes duplicate and empty rows and
    empty columns, writes dates as real dates (iso yyyy-mm-dd or au dd/mm/yyyy
    display). Text that looks like a formula stays text. Facts: rows, rows_removed."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    sheets = read_workbook(src, name_hint)
    if _count_cells(sheets) > EXCEL_MAX_CELLS:
        raise ToolkitError(f"Files up to {EXCEL_MAX_CELLS:,} cells are supported.")
    from_csv = sniff_type(src, name_hint) in ("csv", "txt")
    fmt = {"iso": "yyyy-mm-dd", "au": "dd/mm/yyyy"}.get(dates)
    wb = Workbook(write_only=True)
    used: set[str] = set()
    kept_total = removed = cols_removed = dates_fixed = 0
    for title, rows in sheets:
        clean = []
        for r in rows:
            row = []
            for v in r:
                if isinstance(v, str):
                    if trim:
                        v = re.sub(r"[ \t\u00a0\u2007\u202f]+", " ", v).strip()
                    if v == "":
                        v = None
                    elif fmt:
                        d = parse_date(v)
                        if d:
                            v = d
                            dates_fixed += 1
                    if isinstance(v, str) and from_csv and _NUM_RE.match(v):
                        v = float(v) if "." in v else int(v)
                row.append(v)
            clean.append(row)
        before = len(clean)
        if drop_empty:
            clean = [r for r in clean if any(v is not None for v in r)]
        if dedupe:
            seen, uniq = set(), []
            for r in clean:
                key = tuple("" if v is None else str(v) for v in r)
                key = key[:max((i + 1 for i, x in enumerate(key) if x != ""), default=0)]
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(r)
            clean = uniq
        if drop_empty and clean:
            width = max(len(r) for r in clean)
            keep_cols = [c for c in range(width)
                         if any(c < len(r) and r[c] is not None for r in clean)]
            cols_removed += width - len(keep_cols)
            clean = [[r[c] if c < len(r) else None for c in keep_cols] for r in clean]
        removed += before - len(clean)
        kept_total += len(clean)
        ws = wb.create_sheet(_sheet_title(title, used))
        for r in clean:
            cells = []
            for v in r:
                v = _xlsx_safe(v)
                c = WriteOnlyCell(ws, value=v)
                if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") and len(v) > 1:
                    c.data_type = "s"
                if fmt and isinstance(v, (_dt.date, _dt.datetime)):
                    c.number_format = fmt
                cells.append(c)
            ws.append(cells)
    wb.save(out)
    return {"rows": kept_total, "rows_removed": removed, "columns_removed": cols_removed,
            "dates_fixed": dates_fixed}


# ------------------------------------------------------------ extract-text
OCR_LANGUAGES = ("eng", "nep", "hin", "chi_sim", "chi_tra", "ara", "vie", "ind", "spa", "fra",
                 "deu", "jpn", "kor", "tha", "fil", "eng+nep", "eng+hin")


def installed_ocr_languages() -> set[str]:
    try:
        proc = _run(["tesseract", "--list-langs"], 20)
    except ToolkitError:
        return set()
    return {l.strip() for l in proc.stdout.decode(errors="ignore").splitlines()[1:] if l.strip()}


def check_ocr_language(language: str) -> None:
    if not shutil.which("tesseract"):
        raise ToolkitError("OCR is not available on this machine.")
    have = installed_ocr_languages()
    if any(part not in have for part in language.split("+")):
        raise ToolkitError(f"OCR language {language} is not installed.")


def ocr_image(img, language: str, work: str, tag: str = "img") -> str:
    """Tesseract on one PIL image (grey, capped size); returns its text."""
    img = img.convert("L")
    w, h = img.size
    if w * h > OCR_MAX_PIXELS:
        scale = (OCR_MAX_PIXELS / (w * h)) ** 0.5
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    os.makedirs(work, exist_ok=True)
    png = os.path.join(work, f"{tag}.png")
    img.save(png)
    img.close()
    try:
        # one thread: steadier memory and no oversubscription on a small host
        proc = _run(["tesseract", png, "stdout", "-l", language, "--psm", "3"], TESSERACT_TIMEOUT,
                    env=dict(os.environ, OMP_THREAD_LIMIT="1"))
    finally:
        try:
            os.remove(png)
        except OSError:
            pass
    if proc.returncode != 0:
        raise ToolkitError("OCR failed on a page.")
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _needs_ocr(text: str) -> bool:
    return len(re.sub(r"\s+", "", text)) < 10


def extract_text(src: str, out: str, work: str, ocr: str = "auto", language: str = "eng",
                 name_hint: str = "", on_page=None) -> dict:
    """PDF, image or DOCX -> json {"pages":[{"page","text","ocr"}], "chars"}.
    Text layer first; OCR only where asked (auto = pages with no text), at
    most OCR_MAX_PAGES pages; only the first TEXT_MAX_PAGES pages are read.
    Facts: pages, chars, pages_read, ocr_pages, truncated."""
    kind = sniff_type(src, name_hint)
    pages_out: list[dict] = []
    chars = 0
    ocr_pages = 0
    total = 1
    truncated = False
    if ocr not in ("auto", "no", "yes"):
        raise ToolkitError("OCR must be auto, no or yes.")
    if ocr != "no" and kind in ("pdf", "image"):
        check_ocr_language(language) if ocr == "yes" or kind == "image" else None
    if kind == "pdf":
        doc = _open_pdf(src)
        try:
            total = len(doc)
            n = min(total, TEXT_MAX_PAGES)
            truncated = total > n
            lang_checked = ocr == "yes"
            for i in range(n):
                page = doc[i]
                text = _page_text(page).strip()
                used_ocr = False
                want = ocr == "yes" or (ocr == "auto" and _needs_ocr(text))
                if want and ocr_pages < OCR_MAX_PAGES:
                    if not lang_checked:
                        check_ocr_language(language)
                        lang_checked = True
                    bm = page.render(scale=OCR_DPI / 72, grayscale=True)
                    img = bm.to_pil()
                    bm.close()
                    o = ocr_image(img, language, work, f"p{i}")
                    ocr_pages += 1
                    if o or ocr == "yes":
                        text, used_ocr = o, True
                page.close()
                if chars + len(text) > EXTRACT_MAX_CHARS:
                    text = text[:max(0, EXTRACT_MAX_CHARS - chars)]
                    truncated = True
                chars += len(text)
                pages_out.append({"page": i + 1, "text": text, "ocr": used_ocr})
                if on_page:
                    on_page(i + 1, n)
                if chars >= EXTRACT_MAX_CHARS:
                    break
        finally:
            doc.close()
    elif kind == "image":
        if ocr == "no":
            raise ToolkitError("An image has no text layer; choose OCR auto or yes.")
        img = open_image(src)
        text = ocr_image(img, language, work)[:EXTRACT_MAX_CHARS]
        ocr_pages = 1
        chars = len(text)
        pages_out.append({"page": 1, "text": text, "ocr": True})
    elif kind == "docx":
        check_zip(src)
        text = "\n".join(p.text for p in _docx_paragraphs(_open_docx(src)))
        text = text[:EXTRACT_MAX_CHARS]
        chars = len(text)
        pages_out.append({"page": 1, "text": text, "ocr": False})
    else:
        raise ToolkitError("PDF, image or Word (docx) files only.")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"pages": pages_out, "chars": chars}, fh, ensure_ascii=False)
    return {"pages": total, "pages_read": len(pages_out), "chars": chars,
            "ocr_pages": ocr_pages, "truncated": truncated}


# ------------------------------------------------------------ DOCX helpers

def _open_docx(path: str):
    from docx import Document
    try:
        return Document(path)
    except Exception:
        raise ToolkitError("That Word file could not be read.")


def _docx_paragraphs(doc):
    """Every paragraph in reading order: body (tables included, nested too),
    then headers and footers of each section (each part once)."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    def walk(container):
        for block in container.iter_inner_content():
            if isinstance(block, Paragraph):
                yield block
            elif isinstance(block, Table):
                for row in block.rows:
                    seen = set()
                    for cell in row.cells:
                        if id(cell._tc) in seen:   # merged cells repeat
                            continue
                        seen.add(id(cell._tc))
                        yield from walk(cell)
    yield from walk(doc)
    parts = set()
    for s in doc.sections:
        for hf in (s.header, s.footer, s.first_page_header, s.first_page_footer,
                   s.even_page_header, s.even_page_footer):
            try:
                if hf.is_linked_to_previous:
                    continue
                key = hf.part.partname
            except Exception:
                continue
            if key in parts:
                continue
            parts.add(key)
            yield from walk(hf)


def _para_runs(p):
    """Runs of a paragraph including those inside hyperlinks, in order."""
    from docx.text.run import Run
    return [Run(r, p) for r in p._p.xpath("./w:r | ./w:hyperlink/w:r | ./w:ins/w:r")]


# ----------------------------------------------------------------- pii-find
PII_KINDS = ("tfn", "abn", "acn", "bsb_account", "card", "phone", "email", "medicare",
             "iban", "passport")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def tfn_ok(s: str) -> bool:
    d = [int(c) for c in _digits(s)]
    if len(d) == 9:
        w = (1, 4, 3, 7, 5, 8, 6, 9, 10)
    elif len(d) == 8:
        w = (10, 7, 8, 4, 6, 3, 5, 1)
    else:
        return False
    if len(set(d)) == 1:
        return False
    return sum(a * b for a, b in zip(d, w)) % 11 == 0


def abn_ok(s: str) -> bool:
    d = [int(c) for c in _digits(s)]
    if len(d) != 11 or d[0] == 0:
        return False
    d[0] -= 1
    w = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    return sum(a * b for a, b in zip(d, w)) % 89 == 0


def acn_ok(s: str) -> bool:
    d = [int(c) for c in _digits(s)]
    if len(d) != 9 or len(set(d)) == 1:
        return False
    rem = sum(a * b for a, b in zip(d[:8], (8, 7, 6, 5, 4, 3, 2, 1))) % 10
    return (10 - rem) % 10 == d[8]


def medicare_ok(s: str) -> bool:
    d = [int(c) for c in _digits(s)]
    if len(d) not in (10, 11) or not 2 <= d[0] <= 6:
        return False
    check = sum(a * b for a, b in zip(d[:8], (1, 3, 7, 9, 1, 3, 7, 9))) % 10
    return check == d[8] and d[9] != 0


def luhn_ok(s: str) -> bool:
    d = [int(c) for c in _digits(s)][::-1]
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, x in enumerate(d):
        if i % 2:
            x *= 2
            if x > 9:
                x -= 9
        total += x
    return total % 10 == 0


def card_brand_ok(s: str) -> bool:
    d = _digits(s)
    n = len(d)
    p2, p4 = int(d[:2]), int(d[:4])
    if d[0] == "4" and n in (13, 16, 19):
        return True                                   # Visa
    if (51 <= p2 <= 55 or 2221 <= p4 <= 2720) and n == 16:
        return True                                   # Mastercard
    if p2 in (34, 37) and n == 15:
        return True                                   # Amex
    if (d.startswith("6011") or d.startswith("65") or 644 <= int(d[:3]) <= 649) and n in (16, 19):
        return True                                   # Discover
    if 3528 <= p4 <= 3589 and 16 <= n <= 19:
        return True                                   # JCB
    if (p2 in (36, 38, 39) or 300 <= int(d[:3]) <= 305) and 14 <= n <= 19:
        return True                                   # Diners
    if d.startswith("62") and 16 <= n <= 19:
        return True                                   # UnionPay
    return False


def iban_ok(s: str) -> bool:
    t = re.sub(r"\s", "", s).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", t):
        return False
    r = t[4:] + t[:4]
    num = "".join(str(int(c, 36)) for c in r)
    return int(num) % 97 == 1


_ND = r"(?<![\d])"     # not after a digit
_NA = r"(?![\d])"      # not before a digit
_SEP = r"[ \-\u00a0]?"
_PII_PATTERNS = {
    "abn": re.compile(_ND + r"\d{2}" + _SEP + r"\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3}" + _NA),
    "acn": re.compile(_ND + r"\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3}" + _NA),
    "tfn": re.compile(_ND + r"\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{2,3}" + _NA),
    "medicare": re.compile(_ND + r"[2-6]\d{3}" + _SEP + r"\d{5}" + _SEP + r"\d(?:[ \-/]?\d)?" + _NA),
    "card": re.compile(_ND + r"(?:\d{4}[ \-]?){2,4}\d{1,4}" + _NA + r"|" + _ND + r"\d{4}[ \-]\d{6}[ \-]\d{4,5}" + _NA),
    "bsb_account": re.compile(_ND + r"(\d{3})[ \-](\d{3})" + r"(?:\s*[,/|]?\s*(?:(?:acc(?:ount)?|a/c)(?:\s*(?:no|number|#))?\.?\s*:?\s*)?)(\d[\d ]{4,12}\d)" + _NA, re.I),
    "phone": re.compile(
        r"(?<![\w+])(?:"
        r"\+61[ \-]?\(?0?\)?[ \-]?4\d{2}[ \-]?\d{3}[ \-]?\d{3}"            # +61 4xx xxx xxx
        r"|\+61[ \-]?\(?0?\)?[ \-]?[2378][ \-]?\d{4}[ \-]?\d{4}"           # +61 2 xxxx xxxx
        r"|04\d{2}[ \-]?\d{3}[ \-]?\d{3}"                                  # 04xx xxx xxx
        r"|\(0[2378]\)[ \-]?\d{4}[ \-]?\d{4}|0[2378][ \-]?\d{4}[ \-]?\d{4}"  # (02) xxxx xxxx
        r"|1[38]00[ \-]?\d{3}[ \-]?\d{3}"                                  # 1300 / 1800
        r"|\+(?!61)[1-9]\d{0,2}(?:[ \-.]?\(?\d{1,4}\)?){2,5}"              # international
        r")" + _NA),
    "email": re.compile(r"(?<![\w.+\-])[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,62}[A-Za-z0-9])?)*\.[A-Za-z]{2,24}(?![\w])"),
    "iban": re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?(?![A-Za-z0-9])"),
    "passport": re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\d{7}(?![A-Za-z0-9])"),
}
_CTX = {
    "tfn": re.compile(r"(?i)\b(tfn|tax\s*file)"),
    "acn": re.compile(r"(?i)\b(a\.?c\.?n\.?|company\s*(no|number))\b"),
    "abn": re.compile(r"(?i)\ba\.?b\.?n\.?\b"),
    "passport": re.compile(r"(?i)passport"),
    "bsb": re.compile(r"(?i)\bbsb\b"),
}
# order matters: longer and more specific patterns claim their characters first
_PII_ORDER = ("email", "iban", "card", "abn", "medicare", "bsb_account", "acn", "tfn",
              "phone", "passport")


def _context(text: str, start: int, end: int, span: int = 40) -> str:
    return text[max(0, start - span):start] + " " + text[end:end + 12]


def find_pii_in_text(text: str, kinds) -> list[tuple[str, int, int]]:
    """(kind, start, end) for every match in text. Checksums are required
    where they exist; kinds without one need a nearby label."""
    kinds = set(kinds)
    taken: list[tuple[int, int]] = []
    out = []

    def free(a, b):
        return all(b <= x or a >= y for x, y in taken)

    for kind in _PII_ORDER:
        # card and phone regexes still run when not asked, so a card number
        # is never reported as a TFN fragment; they are just not returned
        rx = _PII_PATTERNS[kind]
        for m in rx.finditer(text):
            a, b = m.span()
            s = m.group(0)
            if not free(a, b):
                continue
            ctx = _context(text, a, b)
            ok = False
            if kind == "email":
                ok = True
            elif kind == "iban":
                ok = iban_ok(s)
            elif kind == "card":
                ok = luhn_ok(s) and card_brand_ok(s) and len(set(_digits(s))) > 1
            elif kind == "abn":
                ok = abn_ok(s)
            elif kind == "medicare":
                ok = medicare_ok(s) and (len(_digits(s)) == 10 or "medicare" in ctx.lower()
                                         or bool(re.search(r"[ \-]", s)))
            elif kind == "bsb_account":
                acct = _digits(m.group(3))
                labelled = (bool(_CTX["bsb"].search(ctx)) or bool(re.search(r"(?i)acc|a/c", s))
                            or s[3:4] == "-")
                ok = 6 <= len(acct) <= 10 and labelled
            elif kind == "acn":
                ok = acn_ok(s) and bool(_CTX["acn"].search(ctx))
            elif kind == "tfn":
                labelled = bool(_CTX["tfn"].search(ctx))
                other = bool(_CTX["acn"].search(ctx) or _CTX["abn"].search(ctx))
                grouped = bool(re.fullmatch(r"\d{3}[ \-]\d{3}[ \-]\d{2,3}", s))
                ok = tfn_ok(s) and (labelled or (grouped and not other and len(_digits(s)) == 9))
            elif kind == "phone":
                ok = 8 <= len(_digits(s)) <= 15
            elif kind == "passport":
                ok = bool(_CTX["passport"].search(ctx))
            if not ok:
                continue
            taken.append((a, b))
            if kind in kinds:
                out.append((kind, a, b))
    out.sort(key=lambda t: t[1])
    return out


def _char_rects(tp, a: int, b: int, page_h: float) -> list[list[float]]:
    """Boxes for chars a..b, one per line, as [x0, top, x1, bottom] in PDF
    points from the page's top left."""
    rects: list[list[float]] = []
    for i in range(a, b):
        try:
            l, bt, r, t = tp.get_charbox(i, loose=True)
        except Exception:
            continue
        if r - l <= 0 and t - bt <= 0:
            continue
        top, bottom = page_h - t, page_h - bt
        if rects and abs(rects[-1][1] - top) < max(2.0, (bottom - top) * 0.5):
            cur = rects[-1]
            cur[0], cur[1] = min(cur[0], l), min(cur[1], top)
            cur[2], cur[3] = max(cur[2], r), max(cur[3], bottom)
        else:
            rects.append([l, top, r, bottom])
    return [[round(v, 2) for v in rc] for rc in rects]


def pii_find(src: str, out: str, kinds=None, on_page=None) -> dict:
    """PDF -> json {"matches":[{"kind","text","page","rect","rects"}], "pages":[...]}.
    rect: [x0, top, x1, bottom] in PDF points from the top left of the page
    (rects: one per line when a match wraps). Facts: matches, pages_without_text."""
    kinds = list(kinds or PII_KINDS)
    bad = [k for k in kinds if k not in PII_KINDS]
    if bad:
        raise ToolkitError("Unknown kinds: " + ", ".join(map(str, bad)) + ".")
    doc = _open_pdf(src)
    matches = []
    sizes = []
    no_text = 0
    try:
        n = len(doc)
        if n > PII_MAX_PAGES:
            raise ToolkitError(f"Finding sensitive info takes PDFs up to {PII_MAX_PAGES} pages.")
        for i in range(n):
            page = doc[i]
            w, h = page.get_size()
            sizes.append({"page": i + 1, "width": round(w, 2), "height": round(h, 2)})
            tp = page.get_textpage()
            try:
                text = tp.get_text_range()
                if _needs_ocr(text):
                    no_text += 1
                for kind, a, b in find_pii_in_text(text, kinds):
                    rects = _char_rects(tp, a, b, h)
                    if not rects:
                        continue
                    union = [min(r[0] for r in rects), min(r[1] for r in rects),
                             max(r[2] for r in rects), max(r[3] for r in rects)]
                    matches.append({"kind": kind, "text": text[a:b].replace("\r\n", " "),
                                    "page": i + 1, "rect": union, "rects": rects})
                    if len(matches) >= 5000:
                        break
            finally:
                tp.close()
                page.close()
            if on_page:
                on_page(i + 1, n)
    finally:
        doc.close()
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"matches": matches, "pages": sizes, "rect_origin": "top-left"}, fh,
                  ensure_ascii=False)
    by_kind: dict[str, int] = {}
    for m in matches:
        by_kind[m["kind"]] = by_kind.get(m["kind"], 0) + 1
    return {"matches": len(matches), "by_kind": by_kind, "pages_without_text": no_text}


# ----------------------------------------------------- segments / translate
_LETTER = re.compile(r"[^\W\d_]", re.U)
_SCANNED_MSG = ("This PDF is a scan with no text layer. Run Make searchable (OCR) on it first, "
                "then translate the result.")


@dataclass
class _Line:
    text: str
    box: list[float]          # x0, y0 (bottom), x1, y1 (top) in PDF user space
    size: float


@dataclass
class _Block:
    page: int
    lines: list[_Line] = field(default_factory=list)

    @property
    def text(self) -> str:
        out = ""
        for ln in self.lines:
            t = ln.text.strip()
            if not out:
                out = t
            elif out.endswith("-") and t[:1].islower():
                out = out[:-1] + t
            else:
                out += " " + t
        return out

    @property
    def box(self) -> list[float]:
        return [min(l.box[0] for l in self.lines), min(l.box[1] for l in self.lines),
                max(l.box[2] for l in self.lines), max(l.box[3] for l in self.lines)]

    @property
    def size(self) -> float:
        s = sorted(l.size for l in self.lines)
        return s[len(s) // 2]


def _page_lines(page) -> list[_Line]:
    import pypdfium2.raw as pdfium_c
    tp = page.get_textpage()
    try:
        n = tp.count_chars()
        if n <= 0:
            return []
        text = tp.get_text_range(0, n)
        lines: list[_Line] = []
        cur_chars: list[str] = []
        boxes: list[tuple] = []
        sizes: list[float] = []

        def flush():
            t = "".join(cur_chars)
            if t.strip() and boxes:
                bx = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                      max(b[2] for b in boxes), max(b[3] for b in boxes)]
                sz = sorted(sizes)[len(sizes) // 2] if sizes else (bx[3] - bx[1])
                lines.append(_Line(t, bx, max(3.0, sz)))
            cur_chars.clear(); boxes.clear(); sizes.clear()

        for i in range(min(n, len(text))):
            ch = text[i]
            if ch in "\r\n":
                if ch == "\n" or (ch == "\r" and (i + 1 >= len(text) or text[i + 1] != "\n")):
                    flush()
                continue
            cur_chars.append(ch)
            if ch.strip():
                try:
                    l, b, r, t = tp.get_charbox(i, loose=True)
                except Exception:
                    continue
                if r > l and t > b:
                    boxes.append((l, b, r, t))
                    fs = pdfium_c.FPDFText_GetFontSize(tp.raw, i)
                    sizes.append(fs if fs and fs > 0 else (t - b) / 1.2)
        flush()
        return lines
    finally:
        tp.close()


def _blocks(lines: list[_Line], page_no: int) -> list[_Block]:
    blocks: list[_Block] = []
    for ln in lines:
        b = blocks[-1] if blocks else None
        if b is not None:
            last = b.lines[-1]
            gap = last.box[1] - ln.box[3]                  # vertical gap (positive = below)
            similar = abs(ln.size - last.size) <= 0.15 * max(ln.size, last.size)
            aligned = (abs(ln.box[0] - last.box[0]) <= 2.5 * ln.size
                       or (ln.box[0] < last.box[2] and ln.box[2] > last.box[0]))
            if -0.3 * ln.size <= gap <= 0.9 * ln.size and similar and aligned \
                    and len(b.lines) < 40:
                b.lines.append(ln)
                continue
        blocks.append(_Block(page_no, [ln]))
    return blocks


def _pdf_segments(src: str, max_pages: int = SEGMENTS_MAX_PAGES):
    """[(id, block)] for blocks with letters, in page order; ids p<page>-<n>."""
    doc = _open_pdf(src)
    out = []
    chars = 0
    try:
        n = len(doc)
        if n > max_pages:
            raise ToolkitError(f"Translation takes PDFs up to {max_pages} pages; this has {n}.")
        for i in range(n):
            page = doc[i]
            lines = _page_lines(page)
            page.close()
            chars += sum(len(l.text.strip()) for l in lines)
            k = 0
            for b in _blocks(lines, i + 1):
                k += 1
                if _LETTER.search(b.text):
                    out.append((f"p{i + 1}-{k}", b))
                    if len(out) > SEGMENTS_MAX:
                        raise ToolkitError(f"This document has more than {SEGMENTS_MAX} pieces "
                                           "of text; split it first.")
    finally:
        doc.close()
    if n and chars < 20:
        raise ToolkitError(_SCANNED_MSG)
    return out


def _docx_segments(doc):
    out = []
    for n, p in enumerate(_docx_paragraphs(doc), 1):
        runs = _para_runs(p)
        text = "".join(r.text for r in runs)
        if _LETTER.search(text):
            out.append((f"d{n}", p, runs, text))
            if len(out) > SEGMENTS_MAX:
                raise ToolkitError(f"This document has more than {SEGMENTS_MAX} paragraphs; "
                                   "split it first.")
    return out


def segments(src: str, out: str, name_hint: str = "") -> dict:
    """PDF or DOCX -> json {"segments":[{"id","text","page"}]}. PDF segments
    are paragraphs (lines grouped by position and size); DOCX segments are
    paragraphs (body, tables, headers, footers). Facts: segments, chars."""
    kind = sniff_type(src, name_hint)
    segs = []
    if kind == "pdf":
        for sid, b in _pdf_segments(src):
            segs.append({"id": sid, "text": b.text, "page": b.page})
    elif kind == "docx":
        check_zip(src)
        for sid, _, _, text in _docx_segments(_open_docx(src)):
            segs.append({"id": sid, "text": text, "page": None})
    else:
        raise ToolkitError("PDF or Word (docx) files only.")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"segments": segs, "type": kind}, fh, ensure_ascii=False)
    return {"segments": len(segs), "chars": sum(len(s["text"]) for s in segs), "type": kind}


def _clean_translations(translations) -> dict[str, str]:
    if not isinstance(translations, dict):
        raise ToolkitError("Translations must be an object of id to text.")
    out = {}
    for k, v in translations.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ToolkitError("Every translation must be text.")
        v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
        if len(v) > 20000:
            raise ToolkitError("A translation is too long.")
        out[k] = v
    return out


# -- font handling for drawn text
_FONTS = (("P2Sans", "NotoSans-Regular.ttf"), ("P2Deva", "NotoSansDevanagari-Regular.ttf"),
          ("P2Arab", "NotoSansArabic-Regular.ttf"), ("P2CJK", "DroidSansFallback.ttf"))
_SHAPED = {"P2Deva", "P2Arab"}
_font_maps: dict[str, set] = {}
# The CJK fallback (3.4 MB) is kept in the repo as parts under fonts/parts; the image build joins them
# (deploy/render/Dockerfile). A checkout without the joined file joins them once into a temp folder.


def _font_path(fn: str) -> str:
    p = os.path.join(FONT_DIR, fn)
    if os.path.exists(p):
        return p
    parts = sorted(glob.glob(os.path.join(FONT_DIR, "parts", fn + ".part*")))
    if parts:
        out = os.path.join(tempfile.gettempdir(), "multies-fonts", fn)
        if not os.path.exists(out):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            tmp = out + f".{os.getpid()}"
            with open(tmp, "wb") as w:
                for part in parts:
                    with open(part, "rb") as r:
                        w.write(r.read())
            os.replace(tmp, out)
        return out
    return p


def _register_fonts():
    if _font_maps:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, fn in _FONTS:
        f = TTFont(name, _font_path(fn))
        pdfmetrics.registerFont(f)
        _font_maps[name] = set(f.face.charToGlyph.keys())


def _font_for(ch: str, prev: str | None) -> str:
    o = ord(ch)
    if ch.isspace() or unicodedata.category(ch)[0] in "PZ" and prev:
        if prev and o in _font_maps[prev]:
            return prev
    for name, _ in _FONTS:
        if o in _font_maps[name]:
            if name == "P2Sans" and prev in _SHAPED and (ch.isspace() or not ch.isalpha()) \
                    and o in _font_maps[prev]:
                return prev
            return name
    return prev or "P2Sans"


def _runs_by_font(text: str) -> list[tuple[str, str]]:
    runs: list[tuple[str, str]] = []
    prev = None
    for ch in text:
        f = _font_for(ch, prev)
        if runs and runs[-1][0] == f:
            runs[-1] = (f, runs[-1][1] + ch)
        else:
            runs.append((f, ch))
        prev = f
    return runs


def _width(text: str, size: float) -> float:
    from reportlab.pdfbase import pdfmetrics
    return sum(pdfmetrics.stringWidth(t, f, size) for f, t in _runs_by_font(text))


_CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")
_RTL_RE = re.compile(r"[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]")


def _tokens(text: str) -> list[str]:
    toks: list[str] = []
    for part in re.findall(r"\S+\s*", text):
        if _CJK_RE.search(part):
            buf = ""
            for ch in part:
                if _CJK_RE.match(ch):
                    if buf:
                        toks.append(buf)
                        buf = ""
                    toks.append(ch)
                else:
                    buf += ch
            if buf:
                toks.append(buf)
        else:
            toks.append(part)
    return toks


def _wrap(text: str, size: float, width: float) -> list[str] | None:
    lines: list[str] = []
    cur = ""
    for tok in _tokens(text):
        cand = cur + tok
        if _width(cand.rstrip(), size) <= width or not cur:
            if not cur and _width(tok.rstrip(), size) > width:
                return None   # a single word wider than the box
            cur = cand
        else:
            lines.append(cur.rstrip())
            cur = tok
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def fit_text(text: str, width: float, height: float, start: float, min_size: float = 4.0):
    """(size, lines) of the largest size <= start that fits the box, else
    the minimum size with what fits (and overflow True)."""
    _register_fonts()
    size = max(min_size, start)
    while size >= min_size:
        lines = _wrap(text, size, width)
        if lines is not None and len(lines) * size * 1.18 <= height + 0.5:
            return size, lines, False
        size -= 0.5
    lines = _wrap(text, min_size, width) or [text]
    return min_size, lines, True


def _visual_order(line: str, rtl: bool) -> str:
    """A small bidi: words of the other direction keep their own order as a
    group; an RTL line runs right to left. Glyphs inside a word are already
    in visual order after shaping."""
    words = re.findall(r"\S+", line)
    if not words:
        return line
    groups: list[list[str]] = []
    for w in words:
        is_rtl = bool(_RTL_RE.search(w))
        if groups and groups[-1][0] == is_rtl:
            groups[-1][1].append(w)
        else:
            groups.append([is_rtl, [w]])
    if rtl:
        groups.reverse()
    out = []
    for is_rtl, ws in groups:
        out.extend(reversed(ws) if is_rtl else ws)
    return " ".join(out)


def draw_text_box(c, text: str, box: list[float], size: float, lines: list[str]) -> None:
    """Draws wrapped lines into box (x0, y0, x1, y1), top down."""
    from reportlab.pdfbase.ttfonts import shapeStr
    x0, y0, x1, y1 = box
    y = y1 - size * 0.95
    for ln in lines:
        if y < y0 - size * 0.5:
            break
        rtl = len(_RTL_RE.findall(ln)) > len(_LETTER.findall(ln)) / 2
        ln = _visual_order(ln, rtl)
        x = x1 - _width(ln, size) if rtl else x0
        for font, chunk in _runs_by_font(ln):
            c.setFont(font, size)
            if font in _SHAPED:
                try:
                    c.drawString(x, y, shapeStr(chunk, font, size))
                except Exception:
                    c.drawString(x, y, chunk)
            else:
                c.drawString(x, y, chunk)
            from reportlab.pdfbase import pdfmetrics
            x += pdfmetrics.stringWidth(chunk, font, size)
        y -= size * 1.18


def _translate_pdf(src: str, out: str, tr: dict[str, str], on_page=None) -> dict:
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas as rl_canvas
    _register_fonts()
    segs = _pdf_segments(src)
    by_page: dict[int, list[tuple[_Block, str]]] = {}
    for sid, b in segs:
        t = tr.get(sid)
        if t is not None and t.strip() and t.strip() != b.text.strip():
            by_page.setdefault(b.page, []).append((b, t.strip()))
    reader = PdfReader(src)
    if reader.is_encrypted:
        raise ToolkitError("This PDF is password protected. Remove the password first.")
    writer = PdfWriter(clone_from=reader)
    replaced = shrunk = overflow = 0
    total = len(writer.pages)
    for pno, page in enumerate(writer.pages, 1):
        items = by_page.get(pno)
        if items:
            mb = page.mediabox
            buf = io.BytesIO()
            c = rl_canvas.Canvas(buf, pagesize=(float(mb.right), float(mb.top)))
            c.setFillColorRGB(1, 1, 1)
            for b, _ in items:
                for ln in b.lines:
                    x0, y0, x1, y1 = ln.box
                    c.rect(x0 - 1, y0 - 1, (x1 - x0) + 2, (y1 - y0) + 2, stroke=0, fill=1)
            c.setFillColorRGB(0, 0, 0)
            for b, t in items:
                x0, y0, x1, y1 = b.box
                height = (y1 - y0) + 1
                size, lines, over = fit_text(t, max(10.0, x1 - x0), height, b.size)
                draw_text_box(c, t, [x0, y0 - (b.size if over else 0), x1, y1], size, lines)
                replaced += 1
                shrunk += size < b.size - 0.01
                overflow += over
            c.save()
            buf.seek(0)
            page.merge_page(PdfReader(buf).pages[0])
        if on_page:
            on_page(pno, total)
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    with open(out, "wb") as fh:
        writer.write(fh)
    return {"segments_replaced": replaced, "segments_shrunk": shrunk,
            "segments_overflow": overflow, "segments_total": len(segs)}


def _rpr_key(run) -> bytes:
    from lxml import etree
    rpr = run._r.rPr
    return etree.tostring(rpr) if rpr is not None else b""


def _split_proportional(text: str, weights: list[int]) -> list[str]:
    toks = _tokens(text)
    total_w = sum(weights) or 1
    total_c = sum(len(t) for t in toks) or 1
    out = ["" for _ in weights]
    targets = []
    acc = 0
    for w in weights:
        acc += w
        targets.append(acc / total_w * total_c)
    pos, ri = 0, 0
    for t in toks:
        mid = pos + len(t) / 2
        while ri < len(weights) - 1 and mid > targets[ri]:
            ri += 1
        out[ri] += t
        pos += len(t)
    return out


def _translate_docx(src: str, out: str, tr: dict[str, str]) -> dict:
    doc = _open_docx(src)
    segs = _docx_segments(doc)
    replaced = 0
    for sid, p, runs, text in segs:
        t = tr.get(sid)
        if t is None or not t.strip() or t == text:
            continue
        live = [r for r in runs if r.text]
        if not live:
            continue
        if len({_rpr_key(r) for r in live}) == 1:
            parts = [t] + [""] * (len(live) - 1)
        else:
            parts = _split_proportional(t, [len(r.text) for r in live])
        for r, part in zip(live, parts):
            r.text = part
        replaced += 1
    doc.save(out)
    return {"segments_replaced": replaced, "segments_total": len(segs)}


def translate_apply(src: str, translations, work: str, name_hint: str = "",
                    on_page=None) -> dict:
    """The same file with text replaced in place. Returns facts plus "_output"."""
    tr = _clean_translations(translations)
    kind = sniff_type(src, name_hint)
    os.makedirs(work, exist_ok=True)
    if kind == "pdf":
        out = os.path.join(work, "translated.pdf")
        facts = _translate_pdf(src, out, tr, on_page)
        facts["_output"] = {"path": out, "name": "translated.pdf", "content_type": CONTENT_TYPES["pdf"]}
    elif kind == "docx":
        check_zip(src)
        out = os.path.join(work, "translated.docx")
        facts = _translate_docx(src, out, tr)
        facts["_output"] = {"path": out, "name": "translated.docx", "content_type": CONTENT_TYPES["docx"]}
    else:
        raise ToolkitError("PDF or Word (docx) files only.")
    if facts["segments_replaced"] == 0:
        raise ToolkitError("None of the translations matched this document. "
                           "Send the same file that was split into segments.")
    return facts


# ------------------------------------------------------------------ render
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_][A-Za-z0-9_.\- ]{0,63}?)\s*\}\}")


def _value_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (int, float, str)):
        s = str(v)
        if isinstance(v, float) and v.is_integer():
            s = str(int(v))
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)[:10000]
    raise ToolkitError("Field values must be text or numbers.")


def _check_data(data) -> dict[str, str]:
    if not isinstance(data, dict):
        raise ToolkitError("Data must be an object of field names to values.")
    if len(data) > 500:
        raise ToolkitError("Up to 500 fields are supported.")
    return {str(k).strip(): _value_text(v) for k, v in data.items()}


def fill_html(template: str, data: dict[str, str]) -> tuple[str, set[str]]:
    missing: set[str] = set()

    def sub(m):
        k = m.group(1).strip()
        if k not in data:
            missing.add(k)
            return ""
        return html.escape(data[k], quote=True).replace("\n", "<br>")
    filled = _PLACEHOLDER.sub(sub, template)
    # a value inside a style attribute could carry a CSS url(); clean again
    return sanitise_html(filled)[0], missing


def _fill_paragraph(p, data: dict[str, str], missing: set[str]) -> int:
    """Replaces {{field}} placeholders that may span runs; the value takes
    the formatting of the run where its placeholder starts."""
    runs = _para_runs(p)
    texts = [r.text for r in runs]
    full = "".join(texts)
    if "{{" not in full:
        return 0
    spans = list(_PLACEHOLDER.finditer(full))
    if not spans:
        return 0
    starts = []
    acc = 0
    for t in texts:
        starts.append(acc)
        acc += len(t)

    def run_at(pos):
        for i in range(len(texts) - 1, -1, -1):
            if starts[i] <= pos and (len(texts[i]) or starts[i] < pos):
                if pos < starts[i] + len(texts[i]) or i == len(texts) - 1:
                    return i
        return 0

    new = list(texts)
    # walk from the end so earlier offsets stay valid
    for m in reversed(spans):
        a, b = m.span()
        k = m.group(1).strip()
        val = data.get(k)
        if val is None:
            missing.add(k)
            val = ""
        ra, rb = run_at(a), run_at(b - 1)
        la, lb = a - starts[ra], b - starts[rb]
        if ra == rb:
            new[ra] = new[ra][:la] + val + new[ra][lb:]
        else:
            new[ra] = new[ra][:la] + val
            for i in range(ra + 1, rb):
                new[i] = ""
            new[rb] = new[rb][lb:]
    for r, old, t in zip(runs, texts, new):
        if old != t:
            r.text = t
    return len(spans)


def fill_docx(src: str, out: str, data: dict[str, str]) -> set[str]:
    doc = _open_docx(src)
    missing: set[str] = set()
    for p in _docx_paragraphs(doc):
        _fill_paragraph(p, data, missing)
    doc.save(out)
    return missing


def _template_kind(path: str, name_hint: str = "") -> str:
    if os.path.getsize(path) > RENDER_MAX_TEMPLATE_BYTES:
        raise ToolkitError("Templates up to 2 MB are supported.")
    kind = sniff_type(path, name_hint)
    if kind == "docx":
        check_zip(path, 100 * 1024 * 1024)
        return "docx"
    if kind in ("html", "txt", "md"):
        return "html"
    raise ToolkitError("Templates must be HTML or Word (docx) files.")


def _prepare_html_template(path: str) -> str:
    text = read_text_file(path, RENDER_MAX_TEMPLATE_BYTES)
    clean, _ = sanitise_html(text)
    # placeholders inside attributes were escaped by the sanitiser and stay
    # placeholders; values are escaped again when filled
    return clean


def render(template: str, data, out: str, work: str, name_hint: str = "") -> dict:
    """One template (HTML or DOCX with {{field}} placeholders) + data -> PDF.
    Facts: files, missing_fields, pages."""
    vals = _check_data(data)
    kind = _template_kind(template, name_hint)
    os.makedirs(work, exist_ok=True)
    if kind == "html":
        filled, missing = fill_html(_prepare_html_template(template), vals)
        pages = _html_file_to_pdf(filled, out, work)
    else:
        src = os.path.join(work, "filled.docx")
        missing = fill_docx(template, src, vals)
        made = soffice_convert([src], os.path.join(work, "pdf"))[0]
        shutil.move(made, out)
        pages = _pdf_pages(out)
    return {"files": 1, "pages": pages, "missing_fields": sorted(missing)[:50]}


def render_batch(template: str, rows, name_field: str, out: str, work: str,
                 name_hint: str = "", on_progress=None) -> dict:
    """Template + up to 500 rows -> zip of PDFs named from name_field (safe,
    deduplicated). LibreOffice converts SOFFICE_BATCH files per run. Facts: files."""
    if not isinstance(rows, list) or not rows:
        raise ToolkitError("Rows must be a list of objects.")
    if len(rows) > RENDER_MAX_ROWS:
        raise ToolkitError(f"Up to {RENDER_MAX_ROWS} rows at a time.")
    vals = [_check_data(r) for r in rows]
    kind = _template_kind(template, name_hint)
    src_dir = os.path.join(work, "src")
    pdf_dir = os.path.join(work, "pdf")
    os.makedirs(src_dir, exist_ok=True)
    names = [safe_name(v.get(name_field, "") or f"document {i + 1}", f"document {i + 1}")
             for i, v in enumerate(vals)]
    names = dedupe_names([n + ".pdf" for n in names])
    missing: set[str] = set()
    tmpl = _prepare_html_template(template) if kind == "html" else None
    done = 0
    pairs: list[tuple[str, str]] = []
    total = len(vals)
    for start in range(0, total, SOFFICE_BATCH):
        chunk = []
        for i in range(start, min(total, start + SOFFICE_BATCH)):
            if kind == "html":
                filled, miss = fill_html(tmpl, vals[i])
                sp = os.path.join(src_dir, f"r{i:04d}.html")
                with open(sp, "w", encoding="utf-8") as fh:
                    fh.write(filled)
            else:
                sp = os.path.join(src_dir, f"r{i:04d}.docx")
                miss = fill_docx(template, sp, vals[i])
            missing |= miss
            chunk.append(sp)
        made = soffice_convert(chunk, pdf_dir, timeout=max(SOFFICE_TIMEOUT, 6 * len(chunk)),
                               infilter="HTML (StarWriter)" if kind == "html" else None)
        for sp in chunk:
            os.remove(sp)
        for i, m in zip(range(start, start + len(made)), made):
            pairs.append((m, names[i]))
        done += len(chunk)
        if on_progress:
            on_progress(done, total)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in pairs:
            z.write(path, name)
            os.remove(path)
    return {"files": len(pairs), "missing_fields": sorted(missing)[:50]}


CONTENT_TYPES = {
    "pdf": "application/pdf",
    "zip": "application/zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
    "csv": "text/csv; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "png": "image/png",
}


# =================================================================== job API
# Shapes from opspec; engine.py loads OPERATIONS and HANDLERS from this
# module by name (P2_MODULES), so nothing here is edited into the engine.
from office_toolkit import opspec  # noqa: E402
from office_toolkit.opspec import Operation, Option  # noqa: E402

CHAIN_MAX_SECONDS = 600
CHAIN_MAX_PAGES = 100        # PDF pages going into any one step
CHAIN_MAX_FILES = 50
CHAIN_ZIP_MAX_ENTRIES = 100
_YESNO = ("yes", "no")
_LANG = Option("language", "choice", "Language", default="eng", choices=OCR_LANGUAGES)
_TO_PDF_EXTS = (".html", ".htm", ".md", ".markdown", ".csv", ".txt", ".jpg", ".jpeg", ".png",
                ".webp", ".svg")
_SHEET_EXTS = (".xlsx", ".xls", ".csv")


def _move_to(ctx, facts: dict) -> dict:
    o = facts.pop("_output")
    shutil.move(o["path"], ctx.output(o["name"]))
    return facts


def _h_to_pdf(ctx, inputs, opts, out):
    return to_pdf(inputs[0], out, ctx.work, inputs[0])


def _h_pdf_to(ctx, inputs, opts, out):
    return _move_to(ctx, pdf_to(inputs[0], opts["target"], ctx.work,
                                on_page=ctx.progress))


def _h_pdf_tables(ctx, inputs, opts, out):
    return pdf_tables(inputs[0], out, on_page=ctx.progress)


def _h_excel_merge(ctx, inputs, opts, out):
    return excel_merge(inputs, out, opts["mode"], names=inputs)


def _h_excel_clean(ctx, inputs, opts, out):
    return excel_clean(inputs[0], out, trim=opts["trim"] == "yes", dedupe=opts["dedupe"] == "yes",
                       dates=opts["dates"], drop_empty=opts["drop_empty"] == "yes",
                       name_hint=inputs[0])


def _h_extract_text(ctx, inputs, opts, out):
    return extract_text(inputs[0], out, ctx.work, opts["ocr"], opts["language"], inputs[0],
                        on_page=ctx.progress)


def _h_pii_find(ctx, inputs, opts, out):
    kinds = opts.get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or not kinds
                              or not all(isinstance(k, str) for k in kinds)):
        raise ToolkitError("Kinds must be a list such as [\"tfn\", \"abn\"].")
    return pii_find(inputs[0], out, kinds, on_page=ctx.progress)


def _h_segments(ctx, inputs, opts, out):
    return segments(inputs[0], out, inputs[0])


def _h_translate_apply(ctx, inputs, opts, out):
    return _move_to(ctx, translate_apply(inputs[0], opts["translations"], ctx.work, inputs[0],
                                         on_page=ctx.progress))


def _h_render(ctx, inputs, opts, out):
    return render(inputs[0], opts["data"], out, ctx.work, inputs[0])


def _h_render_batch(ctx, inputs, opts, out):
    return render_batch(inputs[0], opts["rows"], opts["name_field"], out, ctx.work, inputs[0],
                        on_progress=ctx.progress)


# ------------------------------------------------------------------- chain

def _chain_steps(raw, engine) -> list[tuple[Operation, dict]]:
    if not isinstance(raw, list) or not 2 <= len(raw) <= 6:
        raise ToolkitError("A workflow has 2 to 6 steps.")
    steps = []
    for n, st in enumerate(raw, 1):
        if not isinstance(st, dict) or not isinstance(st.get("op"), str):
            raise ToolkitError(f"Step {n} needs an op.")
        slug = st["op"]
        op = engine.OPERATIONS.get(slug)
        if op is None or slug == "chain":
            raise ToolkitError(f"Step {n}: that tool cannot be used in a workflow.")
        if op.extra is not None and op.extra.min > 0:
            raise ToolkitError(f"Step {n}: {op.label} needs extra files and cannot be used "
                               "in a workflow.")
        given = st.get("options") or {}
        if not isinstance(given, dict):
            raise ToolkitError(f"Step {n}: options must be an object.")
        form = {}
        kinds = {o.name: o.kind for o in op.options}
        for k, v in given.items():
            if k not in kinds:
                raise ToolkitError(f"Step {n}: {op.label} has no setting called {k}.")
            if kinds[k] == "json" and not isinstance(v, str):
                v = json.dumps(v)
            elif isinstance(v, bool):
                v = "yes" if v else "no"
            elif isinstance(v, (int, float)):
                v = str(v)
            elif isinstance(v, list) and kinds[k] == "phrases":
                v = "\n".join(map(str, v))
            elif not isinstance(v, str):
                raise ToolkitError(f"Step {n}: setting {k} must be text.")
            form[k] = v
        try:
            parsed = engine.parse_options(op, form)
        except ToolkitError as e:
            raise ToolkitError(f"Step {n} ({op.label}): {e}")
        steps.append((op, parsed))
    return steps


_KIND_EXTS = {"pdf": (".pdf",), "image": (".jpg", ".jpeg", ".png"),
              "office": (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp")}


def _expand_zip(path: str, dest: str) -> list[str]:
    os.makedirs(dest, exist_ok=True)
    out = []
    total = 0
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > CHAIN_ZIP_MAX_ENTRIES:
            raise ToolkitError("A workflow step made too many files for the next step.")
        for k, info in enumerate(sorted(infos, key=lambda i: i.filename)):
            total += info.file_size
            if total > ZIP_MAX_UNCOMPRESSED:
                raise ToolkitError("A workflow step made files too large for the next step.")
            ext = os.path.splitext(info.filename)[1].lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
                ext = ""
            p = os.path.join(dest, f"{k:03d}{ext}")
            with z.open(info) as src, open(p, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            out.append(p)
    return out


def _check_step_inputs(n: int, op: Operation, files: list[str]) -> None:
    from office_toolkit.web.security import page_count_guard, sniff, sniff_ext
    if not op.min_files <= len(files) <= op.max_files:
        raise ToolkitError(f"Step {n} ({op.label}) got {len(files)} files; it takes "
                           f"{op.min_files} to {op.max_files}.")
    exts = op.exts if op.kind == "file" else _KIND_EXTS.get(op.kind, ())
    pages = 0
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in exts:
            raise ToolkitError(f"Step {n} ({op.label}) cannot take the "
                               f"{ext.lstrip('.') or 'unknown'} file the step before made.")
        if op.kind == "file":
            sniff_ext(f, ext)
        else:
            sniff(f, op.kind)
        if ext == ".pdf":
            page_count_guard(f)
            k = _pdf_pages(f)
            if op.max_pages and k > op.max_pages:
                raise ToolkitError(f"Step {n} ({op.label}) takes PDFs up to {op.max_pages} pages.")
            pages += k
    if pages > CHAIN_MAX_PAGES:
        raise ToolkitError(f"Workflows take up to {CHAIN_MAX_PAGES} pages at each step.")


def run_chain(ctx, inputs: list[str], raw_steps, clock=None) -> dict:
    """Runs 2 to 6 ops in a row, each output feeding the next (a zip output
    is unpacked for a step that takes several files). One time cap for the
    whole workflow and a page cap per step; each step's files are deleted
    once the next step has its output."""
    import time as _time
    from office_toolkit.web import engine
    clock = clock or _time.monotonic
    steps = _chain_steps(raw_steps, engine)
    started = clock()
    files = list(inputs)
    facts: dict = {}
    prev_dir = None
    for n, (op, opts) in enumerate(steps, 1):
        if clock() - started > CHAIN_MAX_SECONDS:
            raise ToolkitError(f"The workflow took longer than {CHAIN_MAX_SECONDS // 60} minutes "
                               f"and stopped before step {n}.")
        _check_step_inputs(n, op, files)
        sdir = os.path.join(ctx.work, f"chain-{n}")
        os.makedirs(os.path.join(sdir, "out"), exist_ok=True)
        job = engine.Job(id=f"chain{n}", op=op.slug, workspace="chain", user="chain", dir=sdir)
        out = os.path.join(sdir, "out", op.output_name)
        try:
            facts = engine._run_op(job, files, dict(opts), out)
        except ToolkitError as e:
            raise ToolkitError(f"Step {n} ({op.label}): {e}")
        made = os.path.join(sdir, "out", job.output_name or op.output_name)
        if not os.path.exists(made):
            raise ToolkitError(f"Step {n} ({op.label}) made no file.")
        if prev_dir:
            shutil.rmtree(prev_dir, ignore_errors=True)
        shutil.rmtree(os.path.join(sdir, "work"), ignore_errors=True)
        prev_dir = sdir
        ctx.progress(n, len(steps))
        if n < len(steps) and made.endswith(".zip"):
            files = _expand_zip(made, os.path.join(sdir, "unzipped"))
        else:
            files = [made]
    final = files[0]
    ext = os.path.splitext(final)[1].lower()
    name = "result" + ext
    if name not in CHAIN_OUTPUTS:
        raise ToolkitError("The last step made a file type a workflow cannot return.")
    shutil.move(final, ctx.output(name))
    if prev_dir:
        shutil.rmtree(prev_dir, ignore_errors=True)
    out_facts = {k: v for k, v in (facts or {}).items() if k not in ("steps", "ops")}
    out_facts.update(steps=len(steps), ops=[op.slug for op, _ in steps])
    return out_facts


def _h_chain(ctx, inputs, opts, out):
    return run_chain(ctx, inputs, opts["steps"])


CHAIN_OUTPUTS = tuple("result" + e for e in (".pdf", ".zip", ".docx", ".json", ".xlsx", ".csv",
                                               ".txt", ".md", ".png"))
_CHAIN_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".docx", ".xlsx", ".pptx", ".odt",
               ".ods", ".odp", ".xls", ".csv", ".txt", ".md", ".markdown", ".html", ".htm",
               ".svg")

OPERATIONS = {
    "to-pdf": Operation(
        "to-pdf", "Convert to PDF (web page, Markdown, CSV, text, image)", "file", 1, 1,
        "converted.pdf", opspec.PDF, exts=_TO_PDF_EXTS, max_bytes=10 * 1024 * 1024,
        limits=(("csv_rows", CSV_MAX_ROWS_TO_PDF), ("text_mb", 5),
                ("image_megapixels", IMAGE_MAX_PIXELS // 1_000_000))),
    "pdf-to": Operation(
        "pdf-to", "PDF to text, Markdown, CSV, Excel or images", "pdf", 1, 1, "document.txt",
        opspec.TXT, (Option("target", "choice", "Convert to", default="txt",
                            choices=("txt", "md", "csv", "xlsx", "png")),),
        max_pages=PDF_TO_MAX_PAGES, slow=True,
        outputs=("document.md", "tables.csv", "tables.xlsx", "pages.zip"),
        limits=(("text_pages", PDF_TO_MAX_PAGES), ("table_pages", TABLES_MAX_PAGES),
                ("image_pages", PDF_TO_PNG_MAX_PAGES), ("tables", TABLES_MAX))),
    "pdf-tables": Operation(
        "pdf-tables", "PDF tables to Excel", "pdf", 1, 1, "tables.xlsx", opspec.XLSX,
        max_pages=TABLES_MAX_PAGES, slow=True, limits=(("tables", TABLES_MAX),)),
    "excel-merge": Operation(
        "excel-merge", "Merge spreadsheets", "file", 2, EXCEL_MAX_FILES, "merged.xlsx",
        opspec.XLSX, (Option("mode", "choice", "How to merge", default="sheets",
                             choices=("sheets", "stack")),),
        exts=_SHEET_EXTS, max_bytes=50 * 1024 * 1024,
        limits=(("rows", EXCEL_MAX_ROWS), ("columns", EXCEL_MAX_COLS), ("cells", EXCEL_MAX_CELLS))),
    "excel-clean": Operation(
        "excel-clean", "Clean spreadsheet data", "file", 1, 1, "cleaned.xlsx", opspec.XLSX,
        (Option("trim", "choice", "Trim spaces", default="yes", choices=_YESNO),
         Option("dedupe", "choice", "Remove duplicate rows", default="yes", choices=_YESNO),
         Option("dates", "choice", "Dates", default="keep", choices=("keep", "iso", "au")),
         Option("drop_empty", "choice", "Remove empty rows and columns", default="yes",
                choices=_YESNO)),
        exts=_SHEET_EXTS, max_bytes=50 * 1024 * 1024,
        limits=(("rows", EXCEL_MAX_ROWS), ("columns", EXCEL_MAX_COLS), ("cells", EXCEL_MAX_CELLS))),
    "extract-text": Operation(
        "extract-text", "Extract text", "file", 1, 1, "text.json", opspec.JSON,
        (Option("ocr", "choice", "Read scanned pages", default="auto", choices=("auto", "no", "yes")),
         _LANG),
        exts=(".pdf", ".jpg", ".jpeg", ".png", ".webp", ".docx"), slow=True,
        limits=(("pages_read", TEXT_MAX_PAGES), ("ocr_pages", OCR_MAX_PAGES),
                ("chars", EXTRACT_MAX_CHARS))),
    "pii-find": Operation(
        "pii-find", "Find sensitive info", "pdf", 1, 1, "matches.json", opspec.JSON,
        (Option("kinds", "json", "What to look for", max_len=300),),
        max_pages=PII_MAX_PAGES, slow=True, limits=(("matches", 5000),)),
    "segments": Operation(
        "segments", "Split text for translation", "file", 1, 1, "segments.json", opspec.JSON,
        exts=(".pdf", ".docx"), max_pages=SEGMENTS_MAX_PAGES,
        limits=(("segments", SEGMENTS_MAX),)),
    "translate-apply": Operation(
        "translate-apply", "Put translated text in place", "file", 1, 1, "translated.pdf",
        opspec.PDF, (Option("translations", "json", "Translations", required=True,
                            max_len=450_000),),
        exts=(".pdf", ".docx"), max_pages=SEGMENTS_MAX_PAGES, slow=True,
        outputs=("translated.docx",), limits=(("segments", SEGMENTS_MAX),)),
    "render": Operation(
        "render", "Fill a template", "file", 1, 1, "document.pdf", opspec.PDF,
        (Option("data", "json", "Field values", required=True, max_len=100_000),),
        exts=(".html", ".htm", ".docx"), max_bytes=RENDER_MAX_TEMPLATE_BYTES,
        limits=(("fields", 500),)),
    "render-batch": Operation(
        "render-batch", "Fill a template for many rows", "file", 1, 1, "documents.zip",
        opspec.ZIP,
        (Option("rows", "json", "Rows", required=True, max_len=450_000),
         Option("name_field", "text", "Field that names each file", required=True, max_len=64)),
        exts=(".html", ".htm", ".docx"), max_bytes=RENDER_MAX_TEMPLATE_BYTES, slow=True,
        limits=(("rows", RENDER_MAX_ROWS),)),
    "chain": Operation(
        "chain", "Workflow (run tools in a row)", "file", 1, CHAIN_MAX_FILES, "result.pdf",
        opspec.PDF, (Option("steps", "json", "Steps", required=True, max_len=4000),),
        exts=_CHAIN_EXTS, max_pages=CHAIN_MAX_PAGES, slow=True, outputs=CHAIN_OUTPUTS[1:],
        limits=(("steps_min", 2), ("steps_max", 6), ("seconds", CHAIN_MAX_SECONDS),
                ("pages_per_step", CHAIN_MAX_PAGES))),
}

HANDLERS = {
    "to-pdf": _h_to_pdf, "pdf-to": _h_pdf_to, "pdf-tables": _h_pdf_tables,
    "excel-merge": _h_excel_merge, "excel-clean": _h_excel_clean,
    "extract-text": _h_extract_text, "pii-find": _h_pii_find, "segments": _h_segments,
    "translate-apply": _h_translate_apply, "render": _h_render,
    "render-batch": _h_render_batch, "chain": _h_chain,
}
