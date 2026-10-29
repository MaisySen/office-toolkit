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
