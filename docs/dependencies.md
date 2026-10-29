# Dependencies and licences

Status: audit in progress, not final. Python libraries below are permissive or
weakly copyleft (file-level). Some operations invoke external executables at
runtime; those are listed separately because their licences differ and the
effect of subprocess use on this project has not had a licence review.
Choosing to keep, replace or drop any of these is part of the owner's
licence decision, which is still open.

| Library | Licence | Used for |
|---|---|---|
| pikepdf | MPL-2.0 | structure, encryption, metadata, compression |
| pypdf | BSD-3-Clause | merge, split, rotate, stamping |
| pypdfium2 | BSD-3-Clause / Apache-2.0 (PDFium) | rendering PDF pages to images |
| Pillow | MIT-CMU (HPND) | image handling |
| reportlab | BSD | overlay/watermark generation, page rebuilds |
| Flask | BSD-3-Clause | web UI |
| Tesseract | Apache-2.0 | OCR engine (system package, invoked directly) |
| LibreOffice | MPL-2.0 | Office-to-PDF conversion (system package, invoked as a subprocess) |
| qpdf (via pikepdf) | Apache-2.0 | compression internals |

## External executables invoked at runtime

| Tool | Licence | Invoked by | Notes |
|---|---|---|---|
| Tesseract | Apache-2.0 | ocr_pdf() subprocess | OCR engine binary |
| LibreOffice | MPL-2.0 | office_to_pdf() subprocess | only needed for Office-to-PDF conversion |

Ghostscript (AGPL v3 / commercial, Artifex) is deliberately NOT in the runtime
path. An earlier design ran OCR through OCRmyPDF, which invokes Ghostscript;
OCR now calls Tesseract directly so the publishable scope has no AGPL
component at all. If OCRmyPDF is ever reintroduced for quality reasons, that
is a dependency adoption decision for the owner, not a silent change.

## Explicitly not used

- **PyMuPDF**  - AGPL v3 or paid commercial licence from Artifex. An early
  prototype used it; this codebase does not. If a future feature needs it,
  that is a licence decision for the project owner first.

## Notes

- MPL-2.0 is file-level copyleft: modifications to the MPL-licensed files
  themselves must stay under MPL, but merely using the libraries does not
  affect this project's own licence choice.
- OCR language support depends on Tesseract language packs installed on the
  machine. This environment has English only.
