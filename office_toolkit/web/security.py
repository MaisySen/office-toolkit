"""Upload validation and HTTP hardening for the web app."""
from __future__ import annotations

from office_toolkit.pdfops import ToolkitError

PDF_MAGIC = b"%PDF-"
IMAGE_MAGICS = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a",
                b"RIFF", b"BM")
OFFICE_MAGICS = (b"PK\x03\x04",)  # docx/xlsx/pptx are zip containers
ODF_MAGIC = b"PK\x03\x04"          # odt etc. also zip
MAX_PDF_PAGES = 500


def sniff(path: str, kind: str) -> None:
    """Reject files whose real bytes do not match the claimed type."""
    with open(path, "rb") as fh:
        head = fh.read(16)
    if kind == "pdf" and not head.startswith(PDF_MAGIC):
        raise ToolkitError("That file is not a PDF (its contents say otherwise).")
    if kind == "image" and not any(head.startswith(m) for m in IMAGE_MAGICS):
        raise ToolkitError("That file is not a supported image.")
    if kind == "office" and not head.startswith(OFFICE_MAGICS):
        raise ToolkitError("That file is not a supported Office document.")


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .xls and .doc
_EXT_KIND = {".pdf": "pdf", ".jpg": "image", ".jpeg": "image", ".png": "image",
             ".webp": "image", ".gif": "image", ".bmp": "image",
             ".docx": "office", ".xlsx": "office", ".pptx": "office",
             ".odt": "office", ".ods": "office", ".odp": "office"}
TEXT_EXTS = (".txt", ".csv", ".md", ".markdown", ".html", ".htm", ".svg", ".json")


def sniff_ext(path: str, ext: str) -> None:
    """Same check as sniff, chosen by the file's extension (P2 ops that take
    several types). Text types must be text: no NUL bytes, valid UTF-8 (a
    UTF-8 or UTF-16 byte order mark is fine)."""
    ext = ext.lower()
    strict = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",)}
    if ext in strict:
        with open(path, "rb") as fh:
            if not fh.read(8).startswith(strict[ext]):
                raise ToolkitError(f"That file is not a {'PNG' if ext == '.png' else 'JPEG'} image.")
        return
    if ext == ".webp":
        with open(path, "rb") as fh:
            head = fh.read(12)
        if not (head.startswith(b"RIFF") and head[8:12] == b"WEBP"):
            raise ToolkitError("That file is not a supported image.")
        return
    if ext in _EXT_KIND:
        return sniff(path, _EXT_KIND[ext])
    if ext in (".xls", ".doc"):
        with open(path, "rb") as fh:
            if fh.read(8) != OLE_MAGIC:
                raise ToolkitError("That file is not a supported Office document.")
        return
    if ext in TEXT_EXTS:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024)
        if head.startswith((b"\xff\xfe", b"\xfe\xff")):
            return
        if b"\x00" in head:
            raise ToolkitError("That file is not a text file.")
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as e:
            # the 64 KB read may cut the last character in half; that is fine
            if not (e.reason == "unexpected end of data" and len(head) == 64 * 1024):
                raise ToolkitError("That file is not UTF-8 text.")
        return
    extra = _extra_sniffers().get(ext)
    if extra:
        return extra(path, ext)
    raise ToolkitError("That file type is not supported.")


def _extra_sniffers() -> dict:
    """R86: video, audio, HEIC and e-book checks live beside their ops."""
    try:
        from office_toolkit.ops_r86 import SNIFFERS
    except ModuleNotFoundError:
        return {}
    return SNIFFERS


def page_count_guard(path: str) -> None:
    from pypdf import PdfReader
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            return  # count happens after the correct password is supplied
        n = len(reader.pages)
    except Exception:
        raise ToolkitError("The PDF could not be parsed (corrupt or unsupported).")
    if n > MAX_PDF_PAGES:
        raise ToolkitError(f"PDFs up to {MAX_PDF_PAGES} pages are supported; this has {n}.")
