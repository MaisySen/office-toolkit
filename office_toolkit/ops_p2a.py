"""Engine P2 ops, helper A: thumbnails, organize, form-fields, form-fill,
compare, protect, stamp, sign and scan-clean (the widened OCR languages
live on the P1 ocr entry in engine.py).

Loaded by engine.py through opspec: OPERATIONS and HANDLERS below. Each
handler is handler(ctx, inputs, opts, out) -> facts. Inputs were type
sniffed and page counted by the engine; option values were parsed (json
options arrive as Python objects and are shape checked here).
"""
from __future__ import annotations

import difflib
import glob
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timezone

import pikepdf
import pypdfium2 as pdfium
from PIL import Image, ImageFilter, ImageMath, ImageOps
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import NameObject
from reportlab.lib.colors import Color
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

from office_toolkit.opspec import JSON, PDF, ZIP, ExtraFiles, Operation, Option
from office_toolkit.pdfops import ToolkitError

# ------------------------------------------------------------------ caps

THUMB_MAX_PAGES = 200
THUMB_EDGE = 200
ORGANIZE_MAX_ENTRIES = 1000
FORM_MAX_FIELDS = 1000
FORM_VALUE_MAX = 2000
COMPARE_MAX_PAGES = 100            # each file
COMPARE_MAX_WORDS_PAGE = 6000
COMPARE_MAX_BYTES = 30 * 1024 * 1024
STAMP_MAX_PAGES = 500
SIGN_MAX_PLACEMENTS = 50
SIGN_MAX_IMAGES = 10
SIGN_IMAGE_MAX_BYTES = 1024 * 1024
SIGN_IMAGE_MAX_PX = 2000
SIGN_MAX_EVENTS = 200
SCAN_MAX_IMAGES = 20
SCAN_MAX_BYTES = 40 * 1024 * 1024
SCAN_MAX_PIXELS = 40_000_000       # per photo, before scaling
SCAN_EDGE = 2400                   # long edge after cleaning (about 200 dpi on A4)
OCR_TIMEOUT = 180                  # seconds per page

OCR_LANGUAGES = ("eng", "nep", "hin", "chi_sim", "chi_tra", "ara", "vie", "ind", "spa", "fra",
                 "deu", "jpn", "kor", "tha", "fil", "eng+nep", "eng+hin")
YES_NO = ("yes", "no")

# ------------------------------------------------------------------ helpers


def _open_reader(path: str) -> PdfReader:
    try:
        r = PdfReader(path)
    except Exception:
        raise ToolkitError("The PDF could not be parsed (corrupt or unsupported).")
    if r.is_encrypted:
        raise ToolkitError("This PDF has a password. Remove it first with Remove password.")
    return r


def _check_not_encrypted(path: str) -> None:
    _open_reader(path)


def _write(writer: PdfWriter, out: str) -> None:
    with open(out, "wb") as fh:
        writer.write(fh)


def _font_path() -> str | None:
    """A Unicode TrueType font if the machine has one (DejaVu in the image)."""
    here = os.path.dirname(__file__)
    for p in [os.path.join(here, "fonts", "NotoSans-Regular.ttf"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]:
        if os.path.exists(p):
            return p
    return None


_FONT_CACHE: dict = {}


def _font(bold: bool = False) -> str:
    """Registers and returns a reportlab font name able to draw Unicode text
    where the machine has a TrueType font, else Helvetica."""
    key = "b" if bold else "r"
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    name = "Helvetica-Bold" if bold else "Helvetica"
    path = _font_path()
    if path:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        bpath = path.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
        use = bpath if bold and os.path.exists(bpath) else path
        try:
            reg = f"P2A{key}"
            pdfmetrics.registerFont(TTFont(reg, use))
            name = reg
        except Exception:
            pass
    _FONT_CACHE[key] = name
    return name


def _safe_text(s: str, font: str) -> str:
    """Helvetica only draws Latin 1; replace what it cannot draw."""
    if font.startswith("Helvetica"):
        return s.encode("latin-1", "replace").decode("latin-1")
    return s



def _tw(text: str, size: float, font: str, bold: bool = False) -> float:
    """Width of text as drawn by _td (shaped, per script font) when the
    bundled fonts are present, else reportlab's width in font."""
    from office_toolkit import textdraw
    if textdraw.needs_unicode(text) and textdraw.available():
        return textdraw.width(text, size, bold=bold)
    from reportlab.pdfbase import pdfmetrics
    return pdfmetrics.stringWidth(_safe_text(text, font), font, size)


def _td(c, x: float, y: float, text: str, size: float, font: str, bold: bool = False,
        align: str = "left") -> None:
    """Draws one line in any script (R68: Nepali, Hindi, Arabic, CJK get
    their own bundled font and HarfBuzz shaping). Falls back to font."""
    from office_toolkit import textdraw
    if textdraw.needs_unicode(text) and textdraw.available():
        textdraw.draw(c, x, y, text, size, bold=bold, align=align)
        return
    c.setFont(font, size)
    t = _safe_text(text, font)
    if align == "center":
        c.drawCentredString(x, y, t)
    elif align == "right":
        c.drawRightString(x, y, t)
    else:
        c.drawString(x, y, t)


def _markup(text: str, font: str, escape) -> str:
    """Paragraph markup for text in any script: runs the base font cannot
    draw go in the bundled font for their script (shaped by reportlab)."""
    from office_toolkit import textdraw
    if not textdraw.available() or not textdraw.needs_unicode(text):
        return escape(_safe_text(text, font))
    out = []
    for face, t in textdraw.runs(text):
        out.append(escape(t) if face == "MTSans" else f'<font face="{face}">{escape(t)}</font>')
    return "".join(out)


def _display(page) -> tuple[float, float, float, float, int]:
    """(x0, y0, width, height, rotate) of a page as shown."""
    mb = page.mediabox
    x0, y0 = float(mb.left), float(mb.bottom)
    w, h = float(mb.width), float(mb.height)
    rot = int(page.get("/Rotate", 0) or 0) % 360
    return x0, y0, w, h, rot


def _overlay_canvas(page):
    """A reportlab canvas the size of the page whose drawing coordinates are
    the page as displayed (rotation applied), origin bottom left. Returns
    (canvas, buffer, display width, display height)."""
    x0, y0, w, h, rot = _display(page)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(w + x0, h + y0))
    c.translate(x0, y0)
    if rot == 90:
        c.translate(w, 0); c.rotate(90); dw, dh = h, w
    elif rot == 180:
        c.translate(w, h); c.rotate(180); dw, dh = w, h
    elif rot == 270:
        c.translate(0, h); c.rotate(-90); dw, dh = h, w
    else:
        dw, dh = w, h
    return c, buf, dw, dh


def _merge_overlay(page, c, buf) -> None:
    c.showPage()
    c.save()
    buf.seek(0)
    page.merge_page(PdfReader(buf).pages[0])


def _parse_pages(spec: str | None, n: int) -> set[int]:
    """1-based page set from '1-3,5' (None = all pages)."""
    if not spec:
        return set(range(1, n + 1))
    out: set[int] = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        if lo < 1 or hi > n or lo > hi:
            raise ToolkitError(f"Pages must be between 1 and {n}.")
        out.update(range(lo, hi + 1))
    return out


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e6


# ------------------------------------------------------------------ thumbnails


def thumbnails(src: str, out_zip: str, work: str, edge: int = THUMB_EDGE, progress=None) -> int:
    import zipfile
    _check_not_encrypted(src)
    pdf = pdfium.PdfDocument(src)
    n = len(pdf)
    try:
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_STORED) as z:
            for i in range(n):
                page = pdf[i]
                w, h = page.get_size()
                scale = edge / max(w, h, 1)
                img = page.render(scale=scale).to_pil().convert("RGB")
                page.close()
                img.thumbnail((edge, edge))
                b = io.BytesIO()
                img.save(b, "PNG", optimize=True)
                z.writestr(f"page-{i + 1:03d}.png", b.getvalue())
                if progress:
                    progress(i + 1, n)
    finally:
        pdf.close()
    return n


def _h_thumbnails(ctx, inputs, opts, out):
    return {"pages": thumbnails(inputs[0], out, ctx.work, progress=ctx.progress)}


# ------------------------------------------------------------------ organize


def check_plan(plan, n: int) -> list[tuple[int, int]]:
    if not isinstance(plan, list) or not plan:
        raise ToolkitError("The page plan must list at least one page.")
    if len(plan) > ORGANIZE_MAX_ENTRIES:
        raise ToolkitError(f"The page plan can list up to {ORGANIZE_MAX_ENTRIES} pages.")
    steps = []
    for e in plan:
        if not isinstance(e, dict) or not _is_int(e.get("page")):
            raise ToolkitError("Each page in the plan needs a page number.")
        p, r = e["page"], e.get("rotate", 0)
        if not 1 <= p <= n:
            raise ToolkitError(f"Page {p} is not in this PDF (it has {n} pages).")
        if not _is_int(r) or r not in (0, 90, 180, 270):
            raise ToolkitError("Rotate must be 0, 90, 180 or 270.")
        steps.append((p, r))
    return steps


def organize(src: str, out: str, plan) -> int:
    r = _open_reader(src)
    steps = check_plan(plan, len(r.pages))
    w = PdfWriter()
    for p, rot in steps:
        added = w.add_page(r.pages[p - 1])
        if rot:
            added.rotate(rot)
    _write(w, out)
    return len(steps)


def _h_organize(ctx, inputs, opts, out):
    return {"pages": organize(inputs[0], out, opts["plan"])}


# ------------------------------------------------------------------ forms


def _field_name(annot) -> str:
    parts = []
    node = annot
    seen = 0
    while node is not None and seen < 50:
        t = node.get("/T")
        if t is not None:
            parts.append(str(t))
        node = node.get("/Parent")
        node = node.get_object() if node is not None else None
        seen += 1
    return ".".join(reversed(parts))


def _inherited(annot, key):
    node, seen = annot, 0
    while node is not None and seen < 50:
        if key in node:
            return node[key]
        node = node.get("/Parent")
        node = node.get_object() if node is not None else None
        seen += 1
    return None


def _on_states(annot) -> list[str]:
    ap = annot.get("/AP")
    if ap is None:
        return []
    n = ap.get_object().get("/N")
    if n is None or not hasattr(n.get_object(), "keys"):
        return []
    return [str(k)[1:] for k in n.get_object().keys() if str(k) != "/Off"]


def _decode_value(v):
    if v is None:
        return None
    v = v.get_object() if hasattr(v, "get_object") else v
    if isinstance(v, list):
        return [str(x) for x in v]
    s = str(v)
    return s[1:] if s.startswith("/") else s


def form_fields(src: str) -> list[dict]:
    r = _open_reader(src)
    fields: dict[str, dict] = {}
    for pno, page in enumerate(r.pages, start=1):
        annots = page.get("/Annots")
        if not annots:
            continue
        for a in annots.get_object():
            a = a.get_object()
            if a.get("/Subtype") != "/Widget":
                continue
            name = _field_name(a)
            if not name:
                continue
            ft = str(_inherited(a, "/FT") or "")
            ff = int(_inherited(a, "/Ff") or 0)
            if ft == "/Tx":
                kind = "text"
            elif ft == "/Btn":
                if ff & (1 << 16):      # push button: nothing to fill
                    continue
                kind = "radio" if ff & (1 << 15) else "checkbox"
            elif ft == "/Ch":
                kind = "choice"
            elif ft == "/Sig":
                kind = "signature"
            else:
                continue
            rect = [round(float(x), 2) for x in a.get("/Rect", [0, 0, 0, 0])]
            f = fields.get(name)
            if f is None:
                if len(fields) >= FORM_MAX_FIELDS:
                    raise ToolkitError(f"Forms with up to {FORM_MAX_FIELDS} fields are supported.")
                opts = []
                if kind == "choice":
                    for o in (_inherited(a, "/Opt") or []):
                        o = o.get_object() if hasattr(o, "get_object") else o
                        if isinstance(o, list) and o:
                            o = o[-1]
                        opts.append(str(o))
                f = {"name": name, "type": kind, "value": _decode_value(_inherited(a, "/V")),
                     "options": opts, "page": pno, "rect": rect,
                     "required": bool(ff & 2)}
                if kind == "checkbox":
                    f["options"] = _on_states(a)[:1] or ["Yes"]
                fields[name] = f
            if kind == "radio":
                for s in _on_states(a):
                    if s not in f["options"]:
                        f["options"].append(s)
    return list(fields.values())


def _h_form_fields(ctx, inputs, opts, out):
    fields = form_fields(inputs[0])
    with open(out, "w") as fh:
        json.dump({"fields": fields}, fh)
    return {"fields": len(fields)}


_TRUE = {"yes", "true", "on", "1", "checked", "x"}


def form_fill(src: str, out: str, values, flatten: bool) -> int:
    if not isinstance(values, dict) or not values:
        raise ToolkitError("Give at least one field value.")
    known = {f["name"]: f for f in form_fields(src)}
    if not known:
        raise ToolkitError("This PDF has no form fields to fill.")
    fill: dict[str, str] = {}
    for name, v in values.items():
        f = known.get(name)
        if f is None or f["type"] == "signature":
            continue
        if isinstance(v, bool):
            v = "yes" if v else "no"
        if not isinstance(v, (str, int, float)):
            raise ToolkitError(f"The value for {name} must be text.")
        v = str(v)
        if len(v) > FORM_VALUE_MAX:
            raise ToolkitError(f"The value for {name} is too long.")
        if f["type"] == "checkbox":
            on = f["options"][0] if f["options"] else "Yes"
            fill[name] = "/" + on if v.strip().lower() in _TRUE else "/Off"
        elif f["type"] == "radio":
            if v not in f["options"]:
                raise ToolkitError(f"{name} must be one of {', '.join(f['options'])}.")
            fill[name] = "/" + v
        elif f["type"] == "choice" and f["options"] and v not in f["options"]:
            raise ToolkitError(f"{name} must be one of {', '.join(f['options'][:20])}.")
        else:
            fill[name] = v
    if not fill:
        raise ToolkitError("None of those fields are in this PDF.")
    w = PdfWriter(clone_from=_open_reader(src))
    # pypdf matches by the field's own /T; map qualified names to it, only
    # where that short name belongs to one field (never fill two by accident).
    shorts: dict[str, int] = {}
    for name in known:
        s = name.rsplit(".", 1)[-1]
        shorts[s] = shorts.get(s, 0) + 1
    by_short: dict[str, str] = {}
    for name, v in fill.items():
        s = name.rsplit(".", 1)[-1]
        if s != name and shorts.get(s) == 1:
            by_short[s] = v
    from office_toolkit import textdraw
    # R68: values in Nepali, Hindi and other non Latin scripts get an
    # appearance drawn with the bundled fonts (pypdf would draw them in the
    # form's Helvetica, which shows question marks or nothing).
    uni = {k for k, v in {**by_short, **fill}.items()
           if not v.startswith("/") and textdraw.needs_unicode(v) and textdraw.available()}
    plain = {k: v for k, v in {**by_short, **fill}.items() if k not in uni}
    drawn = {k: v for k, v in {**by_short, **fill}.items() if k in uni}
    for page in w.pages:
        if page.get("/Annots"):
            if plain:
                w.update_page_form_field_values(page, plain, auto_regenerate=False,
                                                flatten=flatten)
            if drawn:
                w.update_page_form_field_values(page, drawn, auto_regenerate=False,
                                                flatten=False)
    if drawn:
        _unicode_appearances(w, drawn, flatten)
    if flatten:
        for page in w.pages:
            annots = page.get("/Annots")
            if annots:
                keep = [a for a in annots.get_object()
                        if a.get_object().get("/Subtype") != "/Widget"]
                if keep:
                    from pypdf.generic import ArrayObject
                    page[NameObject("/Annots")] = ArrayObject(keep)
                else:
                    del page["/Annots"]
        if "/AcroForm" in w._root_object:
            del w._root_object["/AcroForm"]
    else:
        # every filled field carries its own appearance; a viewer told to
        # redraw them would use the form's font and lose non Latin letters
        w.set_need_appearances_writer(not drawn)
    _write(w, out)
    return len(fill)


def _qualified(annot) -> str:
    parts = []
    node = annot
    for _ in range(50):
        t = node.get("/T")
        if t is not None:
            parts.append(str(t))
        parent = node.get("/Parent")
        if parent is None:
            break
        node = parent.get_object()
    return ".".join(reversed(parts))


def _field_flags(annot) -> int:
    node = annot
    for _ in range(50):
        if "/Ff" in node:
            return int(node["/Ff"])
        parent = node.get("/Parent")
        if parent is None:
            return 0
        node = parent.get_object()
    return 0


def _da_size(annot) -> float:
    node = annot
    for _ in range(50):
        da = node.get("/DA")
        if da is not None:
            m = re.search(r"([\d.]+)\s+Tf", str(da))
            return float(m.group(1)) if m else 0.0
        parent = node.get("/Parent")
        if parent is None:
            return 0.0
        node = parent.get_object()
    return 0.0


def _wrap_lines(text: str, size: float, width: float) -> list[str]:
    from office_toolkit import textdraw
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        cur = ""
        for word in re.findall(r"\S+\s*", para) or [""]:
            cand = cur + word
            if cur and textdraw.width(cand.rstrip(), size) > width:
                lines.append(cur.rstrip())
                cur = word
            else:
                cur = cand
        lines.append(cur.rstrip())
    return lines


def _mk_color(arr):
    try:
        v = [float(t) for t in arr]
    except Exception:
        return None
    if len(v) == 1:
        return Color(v[0], v[0], v[0])
    if len(v) == 3:
        return Color(*v)
    if len(v) == 4:
        c, m, y, k = v
        return Color((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


def _draw_box(c, x0, y0, w, h, look) -> None:
    """The field's own background and border (its /MK), as viewers draw it."""
    bg, bc, bw = look
    if bg is None and bc is None:
        return
    c.saveState()
    if bg is not None:
        c.setFillColor(bg)
    if bc is not None and bw > 0:
        c.setStrokeColor(bc); c.setLineWidth(bw)
    c.rect(x0 + bw / 2, y0 + bw / 2, w - bw, h - bw, stroke=int(bc is not None and bw > 0),
           fill=int(bg is not None))
    c.restoreState()


def _look(a):
    mk = a.get("/MK")
    mk = mk.get_object() if mk is not None else {}
    bg = _mk_color(mk.get("/BG")) if mk.get("/BG") is not None else None
    bc = _mk_color(mk.get("/BC")) if mk.get("/BC") is not None else None
    bs = a.get("/BS")
    try:
        bw = float(bs.get_object().get("/W", 1)) if bs is not None else 1.0
    except Exception:
        bw = 1.0
    return bg, bc, bw


def _draw_field(c, x0: float, y0: float, w: float, h: float, value: str, size: float,
                multiline: bool, look=(None, None, 0.0)) -> None:
    from office_toolkit import textdraw
    _draw_box(c, x0, y0, w, h, look)
    c.setFillColor(Color(0, 0, 0))
    pad = 2
    if multiline:
        size = size or 10
        lines = _wrap_lines(value, size, w - 2 * pad)
        y = y0 + h - pad - size
        for ln in lines:
            if y < y0 - size * 0.3:
                break
            textdraw.draw(c, x0 + pad, y, ln, size)
            y -= size * 1.2
        return
    if not size:   # auto size: fit the height, then the width
        size = max(4.0, min(12.0, h * 0.62))
        while size > 4 and textdraw.width(value, size) > w - 2 * pad:
            size -= 0.5
    # Devanagari sits higher above the baseline than Latin; centre the line
    textdraw.draw(c, x0 + pad, y0 + (h - size) / 2 + size * 0.22, value, size)


def _unicode_appearances(w, values: dict, flatten: bool) -> None:
    """Draws each listed text field's value with the bundled fonts: as its
    appearance stream, or (flatten) straight onto the page."""
    from pypdf.generic import (ArrayObject, DictionaryObject, FloatObject, NameObject,
                               StreamObject)
    for page in w.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        overlay = []
        for ref in annots.get_object():
            a = ref.get_object()
            if a.get("/Subtype") != "/Widget":
                continue
            q = _qualified(a)
            v = values.get(q)
            if v is None:
                v = values.get(q.rsplit(".", 1)[-1])
            if v is None:
                continue
            x1, y1, x2, y2 = [float(t) for t in a["/Rect"]]
            rx, ry, rw, rh = min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)
            if rw < 1 or rh < 1:
                continue
            multiline = bool(_field_flags(a) & 4096)
            size = _da_size(a)
            if flatten:
                overlay.append((rx, ry, rw, rh, v, size, multiline, _look(a)))
                continue
            buf = io.BytesIO()
            c = rl_canvas.Canvas(buf, pagesize=(rw, rh))
            _draw_field(c, 0, 0, rw, rh, v, size, multiline, _look(a))
            c.showPage(); c.save(); buf.seek(0)
            src = PdfReader(buf).pages[0]
            xo = StreamObject()
            xo.set_data(src.get_contents().get_data())
            xo.update({NameObject("/Type"): NameObject("/XObject"),
                       NameObject("/Subtype"): NameObject("/Form"),
                       NameObject("/BBox"): ArrayObject([FloatObject(0), FloatObject(0),
                                                         FloatObject(rw), FloatObject(rh)]),
                       NameObject("/Resources"): src["/Resources"].clone(w)})
            a[NameObject("/AP")] = DictionaryObject({NameObject("/N"): w._add_object(xo)})
        if overlay:
            mb = page.mediabox
            buf = io.BytesIO()
            c = rl_canvas.Canvas(buf, pagesize=(float(mb.right) + 1, float(mb.top) + 1))
            for item in overlay:
                _draw_field(c, *item)
            c.showPage(); c.save(); buf.seek(0)
            page.merge_page(PdfReader(buf).pages[0])


def _h_form_fill(ctx, inputs, opts, out):
    return {"fields_filled": form_fill(inputs[0], out, opts["values"], opts["flatten"] == "yes")}


# ------------------------------------------------------------------ compare


def _page_words(textpage) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Words on a page with their boxes (left, bottom, right, top)."""
    n = textpage.count_chars()
    words, cur, box = [], [], None
    for i in range(n):
        ch = textpage.get_text_range(i, 1)
        if not ch or ch.isspace() or ch in "\x00\ufffe":
            if cur:
                words.append(("".join(cur), box))
                cur, box = [], None
            continue
        try:
            l, b, r, t = textpage.get_charbox(i)
        except Exception:
            l = b = r = t = 0.0
        cur.append(ch)
        box = (l, b, r, t) if box is None else (min(box[0], l), min(box[1], b),
                                               max(box[2], r), max(box[3], t))
        if len(words) > COMPARE_MAX_WORDS_PAGE:
            break
    if cur:
        words.append(("".join(cur), box))
    return words


def _doc_words(path: str) -> list[list]:
    pdf = pdfium.PdfDocument(path)
    try:
        out = []
        for i in range(len(pdf)):
            page = pdf[i]
            tp = page.get_textpage()
            out.append(_page_words(tp))
            tp.close()
            page.close()
        return out
    finally:
        pdf.close()


def _merge_boxes(boxes):
    """Joins word boxes on the same line into one rectangle each."""
    lines = []
    for l, b, r, t in boxes:
        for ln in lines:
            if abs(ln[1] - b) < 3 and abs(ln[3] - t) < 3 and l - ln[2] < 12:
                ln[0], ln[2] = min(ln[0], l), max(ln[2], r)
                break
        else:
            lines.append([l, b, r, t])
    return lines


def compare_words(old: list[list], new: list[list]) -> list[dict]:
    pages = []
    for i in range(max(len(old), len(new))):
        a = old[i] if i < len(old) else []
        b = new[i] if i < len(new) else []
        sm = difflib.SequenceMatcher(None, [w for w, _ in a], [w for w, _ in b], autojunk=False)
        added, removed, abox, rbox = [], [], [], []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("replace", "delete"):
                removed.append(" ".join(w for w, _ in a[i1:i2]))
                rbox += [bx for _, bx in a[i1:i2] if bx]
            if tag in ("replace", "insert"):
                added.append(" ".join(w for w, _ in b[j1:j2]))
                abox += [bx for _, bx in b[j1:j2] if bx]
        if added or removed or (i >= len(old)) or (i >= len(new)):
            pages.append({"page": i + 1, "added": added, "removed": removed,
                          "words_added": sum(len(x.split()) for x in added),
                          "words_removed": sum(len(x.split()) for x in removed),
                          "_abox": abox, "_rbox": rbox,
                          "only_in": "new" if i >= len(old) else "old" if i >= len(new) else None})
    return pages


def _summary_page(pages, n_old, n_new, words_added, words_removed) -> PdfReader:
    from reportlab.lib.pagesizes import A4
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    f, fb = _font(), _font(bold=True)
    W, H = A4
    y = H - 72
    c.setFont(fb, 20); c.drawString(56, y, "Comparison of two versions"); y -= 34
    c.setFont(f, 11)
    def pages_word(n):
        return f"{n} page" if n == 1 else f"{n} pages"
    lines = [f"Old version: {pages_word(n_old)}. New version: {pages_word(n_new)}.",
             f"Pages with changes: {len(pages)}",
             f"Words added: {words_added}",
             f"Words removed: {words_removed}"]
    if not pages:
        lines.append("No differences in the text were found.")
    for ln in lines:
        c.drawString(56, y, ln); y -= 18
    y -= 10
    if pages:
        c.setFont(fb, 12); c.drawString(56, y, "Changed pages"); y -= 20
        c.setFont(f, 10)
        for p in pages:
            if y < 72:
                c.showPage(); c.setFont(f, 10); y = H - 72
            if p["only_in"] == "new":
                txt = f"Page {p['page']}: only in the new version"
            elif p["only_in"] == "old":
                txt = f"Page {p['page']}: only in the old version (removed)"
            else:
                txt = f"Page {p['page']}: {p['words_added']} words added, {p['words_removed']} removed"
            c.drawString(66, y, _safe_text(txt, f)); y -= 15
        y -= 8
    if y > 90:
        c.setFont(f, 9); c.setFillColor(Color(0.35, 0.35, 0.35))
        c.drawString(56, max(y, 72), "Following pages: old on the left, new on the right. "
                                     "Red boxes were removed, green boxes were added.")
    c.showPage(); c.save(); buf.seek(0)
    return PdfReader(buf)


def compare(old_path: str, new_path: str, out: str, as_json: bool, work: str,
            progress=None) -> dict:
    r_old, r_new = _open_reader(old_path), _open_reader(new_path)
    for r in (r_old, r_new):
        if len(r.pages) > COMPARE_MAX_PAGES:
            raise ToolkitError(f"Compare takes PDFs up to {COMPARE_MAX_PAGES} pages each.")
    old, new = _doc_words(old_path), _doc_words(new_path)
    if not any(old) and not any(new):
        raise ToolkitError("These PDFs have no text to compare. Run OCR on scans first.")
    pages = compare_words(old, new)
    added = sum(p["words_added"] for p in pages)
    removed = sum(p["words_removed"] for p in pages)
    facts = {"pages_changed": len(pages), "words_added": added, "words_removed": removed}
    if as_json:
        with open(out, "w") as fh:
            json.dump({"pages": [{"page": p["page"], "added": p["added"],
                                  "removed": p["removed"]} for p in pages]}, fh)
        return facts
    w = PdfWriter()
    for pg in _summary_page(pages, len(r_old.pages), len(r_new.pages), added, removed).pages:
        w.add_page(pg)
    gap = 18
    for k, p in enumerate(pages):
        i = p["page"] - 1
        lp = r_old.pages[i] if i < len(r_old.pages) else None
        rp = r_new.pages[i] if i < len(r_new.pages) else None
        dims = [(float(x.mediabox.width), float(x.mediabox.height)) if x is not None else None
                for x in (lp, rp)]
        lw, lh = dims[0] or dims[1]
        rw, rh = dims[1] or dims[0]
        W, H = lw + gap + rw, max(lh, rh) + 28
        sheet = w.add_blank_page(W, H)
        for src_page, dx, (pw, ph) in ((lp, 0, (lw, lh)), (rp, lw + gap, (rw, rh))):
            if src_page is None:
                continue
            x0, y0 = float(src_page.mediabox.left), float(src_page.mediabox.bottom)
            sheet.merge_transformed_page(src_page, Transformation().translate(dx - x0, -y0))
        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=(W, H))
        f = _font()
        c.setFont(f, 9); c.setFillColor(Color(0.3, 0.3, 0.3))
        c.drawString(4, H - 14, _safe_text(f"Old, page {p['page']}" if lp is not None
                                           else "Not in the old version", f))
        c.drawString(lw + gap + 4, H - 14, _safe_text(f"New, page {p['page']}" if rp is not None
                                                      else "Not in the new version", f))
        c.setStrokeColor(Color(0.7, 0.7, 0.7)); c.setLineWidth(0.5)
        c.line(lw + gap / 2, 0, lw + gap / 2, H)
        for boxes, dx, src_page, col in ((p["_rbox"], 0, lp, Color(0.85, 0.1, 0.1)),
                                         (p["_abox"], lw + gap, rp, Color(0.1, 0.6, 0.2))):
            if src_page is None:
                continue
            x0, y0 = float(src_page.mediabox.left), float(src_page.mediabox.bottom)
            c.setStrokeColor(col); c.setFillColor(col); c.setFillAlpha(0.15)
            c.setLineWidth(1)
            for l, b, r, t in _merge_boxes(boxes)[:2000]:
                c.rect(dx + l - x0 - 1, b - y0 - 1, (r - l) + 2, (t - b) + 2, stroke=1, fill=1)
        c.showPage(); c.save(); buf.seek(0)
        sheet.merge_page(PdfReader(buf).pages[0])
        if progress:
            progress(k + 1, len(pages))
    _write(w, out)
    return facts


def _h_compare(ctx, inputs, opts, out):
    as_json = opts["output"] == "json"
    if as_json:
        out = ctx.output("changes.json")
    return compare(inputs[0], inputs[1], out, as_json, ctx.work, progress=ctx.progress)


# ------------------------------------------------------------------ protect


def protect(src: str, out: str, password: str, allow_print: bool, allow_copy: bool) -> int:
    if not isinstance(password, str) or not 4 <= len(password) <= 128:
        raise ToolkitError("The password must be 4 to 128 characters.")
    try:
        pdf = pikepdf.open(src)
    except pikepdf.PasswordError:
        raise ToolkitError("This PDF already has a password. Remove it first with Remove password.")
    except Exception:
        raise ToolkitError("The PDF could not be parsed (corrupt or unsupported).")
    with pdf:
        if pdf.is_encrypted:
            raise ToolkitError("This PDF already has a password. Remove it first with Remove password.")
        allow = pikepdf.Permissions(
            print_lowres=allow_print, print_highres=allow_print,
            extract=allow_copy, accessibility=True,
            modify_annotation=False, modify_assembly=False, modify_form=False,
            modify_other=False)
        # A random owner password: nobody can lift the limits without it.
        pdf.save(out, encryption=pikepdf.Encryption(owner=secrets.token_urlsafe(24),
                                                    user=password, R=6, allow=allow))
        return len(pdf.pages)


def _h_protect(ctx, inputs, opts, out):
    pages = protect(inputs[0], out, opts["password"], opts["allow_print"] == "yes",
                    opts["allow_copy"] == "yes")
    opts.clear()  # the password is not kept
    return {"pages": pages, "encryption": "AES-256"}


# ------------------------------------------------------------------ stamp

STAMPS = {"approved": ("APPROVED", (0.1, 0.55, 0.2)), "paid": ("PAID", (0.1, 0.55, 0.2)),
          "draft": ("DRAFT", (0.45, 0.45, 0.45)), "confidential": ("CONFIDENTIAL", (0.8, 0.1, 0.1)),
          "copy": ("COPY", (0.15, 0.3, 0.75)), "custom": (None, (0.8, 0.1, 0.1))}


def _draw_stamp(c, label: str, rgb, position: str, dw: float, dh: float, font: str) -> None:
    col = Color(*rgb)
    if position == "center":
        size = max(28, min(dw, dh) / 9)
        c.saveState()
        c.translate(dw / 2, dh / 2); c.rotate(30)
        tw = _tw(label, size, font, bold=True)
        c.setStrokeColor(Color(*rgb, alpha=0.55)); c.setFillColor(Color(*rgb, alpha=0.45))
        c.setLineWidth(3)
        c.roundRect(-tw / 2 - 16, -size * 0.45, tw + 32, size * 1.3, 8, stroke=1, fill=0)
        _td(c, 0, -size * 0.1, label, size, font, bold=True, align="center")
        c.restoreState()
        return
    size = 16
    tw = _tw(label, size, font, bold=True)
    bw, bh, m = tw + 20, size + 14, 28
    x = m if position.endswith("left") else dw - m - bw
    y = dh - m - bh if position.startswith("top") else m
    c.saveState()
    c.setStrokeColor(col); c.setFillColor(col); c.setLineWidth(2)
    c.roundRect(x, y, bw, bh, 5, stroke=1, fill=0)
    _td(c, x + bw / 2, y + 9, label, size, font, bold=True, align="center")
    c.restoreState()


def stamp(src: str, out: str, opts: dict) -> int:
    from office_toolkit import textdraw
    r = _open_reader(src)
    n = len(r.pages)
    kind = opts.get("stamp") or "none"
    label = None
    if kind != "none":
        label, rgb = STAMPS[kind]
        if kind == "custom":
            label = (opts.get("stamp_text") or "").strip().upper()
            if not label:
                raise ToolkitError("Type the stamp text for a custom stamp.")
    numbers = opts.get("page_numbers") or "none"
    header = (opts.get("header") or "").strip()
    footer = (opts.get("footer") or "").strip()
    if not label and numbers == "none" and not header and not footer:
        raise ToolkitError("Choose a stamp, page numbers, a header or a footer.")
    targets = _parse_pages(opts.get("ranges"), n)
    font, fontb = _font(), _font(bold=True)
    unicode_ok = textdraw.available()
    for s in (label or "", header, footer):
        if not unicode_ok and font.startswith("Helvetica") and _safe_text(s, font) != s:
            raise ToolkitError("Stamps, headers and footers take Latin letters only on this server.")
        if unicode_ok and textdraw.missing(s):
            raise ToolkitError("Some letters in the stamp, header or footer cannot be drawn: "
                               + textdraw.missing(s)[:10])
    # R72: page numbers follow their own setting, not the language of the stamp,
    # header or footer. Latin digits unless asked: "devanagari" always, "auto"
    # (the R68 behaviour) when Nepali or Hindi words are on the page.
    digits = opts.get("number_digits") or "latin"
    if digits not in NUMBER_DIGITS:
        raise ToolkitError("Page number digits: choose latin, devanagari or auto.")
    deva = digits == "devanagari" or (
        digits == "auto" and any(textdraw.has_devanagari(s) for s in (label or "", header, footer)))
    numpos = opts.get("number_position") or "bottom-center"
    w = PdfWriter(clone_from=r)
    for i, page in enumerate(w.pages, start=1):
        if i not in targets:
            continue
        c, buf, dw, dh = _overlay_canvas(page)
        if label:
            _draw_stamp(c, label, rgb, opts.get("position") or "top-right", dw, dh, fontb)
        c.setFillColor(Color(0.2, 0.2, 0.2))
        c.setFont(font, 9)
        if header:
            _td(c, dw / 2, dh - 22, header, 9, font, align="center")
        if numbers != "none":
            if deva:
                txt = _deva_digits(str(i)) if numbers == "n" else \
                    f"पृष्ठ {_deva_digits(str(i))} / {_deva_digits(str(n))}"
            else:
                txt = str(i) if numbers == "n" else f"Page {i} of {n}"
            if numpos == "bottom-right":
                _td(c, dw - 30, 18, txt, 9, font, align="right")
            elif numpos == "top-right":
                _td(c, dw - 30, dh - 22, txt, 9, font, align="right")
            else:
                _td(c, dw / 2, 18, txt, 9, font, align="center")
        if footer:
            if numbers != "none" and numpos == "bottom-center":
                _td(c, 30, 18, footer, 9, font)
            else:
                _td(c, dw / 2, 18, footer, 9, font, align="center")
        _merge_overlay(page, c, buf)
    _write(w, out)
    return len(targets)


_DEVA_DIGITS = str.maketrans("0123456789", "०१२३४५६७८९")
NUMBER_DIGITS = ("latin", "devanagari", "auto")


def _deva_digits(s: str) -> str:
    return s.translate(_DEVA_DIGITS)


def _h_stamp(ctx, inputs, opts, out):
    return {"pages_changed": stamp(inputs[0], out, opts)}


# ------------------------------------------------------------------ sign


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_time(s) -> datetime:
    if not isinstance(s, str) or len(s) > 40:
        raise ToolkitError("Each signing event needs a time.")
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ToolkitError("A signing event time is not a valid date and time.")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _short(v, n: int, what: str) -> str:
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise ToolkitError(f"{what} must be text.")
    v = str(v).strip()
    if len(v) > n:
        raise ToolkitError(f"{what} is too long.")
    return v


def check_signatures(sigs, n_images: int, pages) -> list[dict]:
    if not isinstance(sigs, list) or not sigs:
        raise ToolkitError("Place at least one signature.")
    if len(sigs) > SIGN_MAX_PLACEMENTS:
        raise ToolkitError(f"Up to {SIGN_MAX_PLACEMENTS} signatures per document.")
    out = []
    for k, s in enumerate(sigs, start=1):
        if not isinstance(s, dict):
            raise ToolkitError(f"Signature {k} is not valid.")
        p, img = s.get("page"), s.get("image")
        if not _is_int(p) or not 1 <= p <= len(pages):
            raise ToolkitError(f"Signature {k} is on a page this PDF does not have.")
        if not _is_int(img) or not 0 <= img < n_images:
            raise ToolkitError(f"Signature {k} points at an image that was not sent.")
        x, y, w, h = (s.get(key) for key in ("x", "y", "w", "h"))
        if not all(_num(v) for v in (x, y, w, h)) or w <= 2 or h <= 2:
            raise ToolkitError(f"Signature {k} needs a position and size.")
        _, _, pw, ph, rot = _display(pages[p - 1])
        dw, dh = (ph, pw) if rot in (90, 270) else (pw, ph)
        if x < 0 or y < 0 or x + w > dw + 0.5 or y + h > dh + 0.5:
            raise ToolkitError(f"Signature {k} is outside page {p}.")
        out.append({"page": p, "image": img, "x": float(x), "y": float(y), "w": float(w),
                    "h": float(h), "name": _short(s.get("name"), 100, "The signer name"),
                    "date": _short(s.get("date"), 40, "The signing date")})
    return out


def _load_signature(path: str) -> ImageReader:
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        raise ToolkitError("A signature image could not be read.")
    if img.width * img.height > 16_000_000:
        raise ToolkitError("A signature image is too large.")
    img = img.convert("RGBA")
    img.thumbnail((SIGN_IMAGE_MAX_PX, SIGN_IMAGE_MAX_PX))
    return ImageReader(img), img.width, img.height


def _check_audit(audit) -> dict | None:
    if audit is None:
        return None
    if not isinstance(audit, dict):
        raise ToolkitError("The signing record is not valid.")
    events = audit.get("events") or []
    if not isinstance(events, list) or len(events) > SIGN_MAX_EVENTS:
        raise ToolkitError(f"The signing record can hold up to {SIGN_MAX_EVENTS} events.")
    rows = []
    for e in events:
        if not isinstance(e, dict):
            raise ToolkitError("A signing event is not valid.")
        rows.append({"at": _parse_time(e.get("at")), "who": _short(e.get("who"), 100, "A name"),
                     "email": _short(e.get("email"), 254, "An email"),
                     "what": _short(e.get("what"), 60, "An action"),
                     "ip_country": _short(e.get("ip_country"), 60, "A country")})
    sha = _short(audit.get("document_sha256"), 64, "The document fingerprint").lower()
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ToolkitError("The document fingerprint must be a SHA-256 hex value.")
    return {"title": _short(audit.get("title"), 200, "The title"), "sha": sha, "events": rows}


def _sydney(t: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo("Australia/Sydney")).strftime("%d %b %Y %H:%M %Z")
    except Exception:
        return ""


def _record_pages(audit: dict, uploaded_sha: str, size) -> PdfReader:
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape
    f, fb = _font(), _font(bold=True)
    st = ParagraphStyle("b", fontName=f, fontSize=10, leading=13)
    small = ParagraphStyle("s", fontName=f, fontSize=8, leading=10)
    head = ParagraphStyle("h", fontName=fb, fontSize=18, leading=22, spaceAfter=6)
    cellh = ParagraphStyle("ch", fontName=fb, fontSize=8, leading=10)

    def P(s, style=st):
        return Paragraph(_markup(s, f, escape), style)

    def footer(c, doc):
        c.saveState(); c.setFont(f, 8); c.setFillColor(Color(0.35, 0.35, 0.35))
        c.drawCentredString(doc.pagesize[0] / 2, 24, "Signed electronically in Multies")
        c.restoreState()

    buf = io.BytesIO()
    W = max(size[0], 420)
    doc = SimpleDocTemplate(buf, pagesize=(W, max(size[1], 560)), leftMargin=40,
                            rightMargin=40, topMargin=48, bottomMargin=48,
                            title="Signing record")
    story = [P("Signing record", head)]
    if audit["title"]:
        story.append(P(f"Document: {audit['title']}"))
    shown = audit["sha"] or uploaded_sha
    story.append(P("Document fingerprint (SHA-256 of the original):"))
    story.append(P(shown, small))
    if audit["sha"] and audit["sha"] != uploaded_sha:
        story.append(P("Fingerprint of the file this record was added to:"))
        story.append(P(uploaded_sha, small))
    story.append(Spacer(1, 10))
    rows = [[Paragraph(x, cellh) for x in ("Time (UTC)", "Time (Sydney)", "Name", "Email",
                                           "Action", "IP country")]]
    for e in audit["events"]:
        rows.append([P(e["at"].strftime("%d %b %Y %H:%M:%S UTC"), small), P(_sydney(e["at"]), small),
                     P(e["who"], small), P(e["email"], small), P(e["what"], small),
                     P(e["ip_country"], small)])
    if len(rows) == 1:
        rows.append([P("No events recorded", small)] + [""] * 5)
    avail = W - 80
    t = Table(rows, colWidths=[avail * x for x in (0.17, 0.17, 0.17, 0.25, 0.12, 0.12)],
              repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, Color(0.75, 0.75, 0.75)),
                           ("BACKGROUND", (0, 0), (-1, 0), Color(0.93, 0.93, 0.93)),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    buf.seek(0)
    return PdfReader(buf)


def sign(src: str, out: str, sigs, images: list[str], audit=None) -> dict:
    r = _open_reader(src)
    if not images:
        raise ToolkitError("Send at least one signature image.")
    placements = check_signatures(sigs, len(images), r.pages)
    record = _check_audit(audit)
    uploaded_sha = _sha256(src)
    loaded = [_load_signature(p) for p in images]
    w = PdfWriter(clone_from=r)
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    font = _font()
    by_page: dict[int, list[dict]] = {}
    for s in placements:
        by_page.setdefault(s["page"], []).append(s)
    for pno, items in by_page.items():
        page = w.pages[pno - 1]
        c, buf, dw, dh = _overlay_canvas(page)
        for s in items:
            reader, iw, ih = loaded[s["image"]]
            scale = min(s["w"] / iw, s["h"] / ih)
            dwid, dhei = iw * scale, ih * scale
            x = s["x"] + (s["w"] - dwid) / 2
            top = dh - s["y"]
            y = top - s["h"] + (s["h"] - dhei) / 2
            c.drawImage(reader, x, y, dwid, dhei, mask="auto")
            if s["name"]:
                line = f"Signed by {s['name']}, {s['date'] or today}"
                size = 7
                c.setFont(font, size); c.setFillColor(Color(0.25, 0.25, 0.25))
                ly = top - s["h"] - size - 1
                if ly < 2:          # no room under it: put the line inside the bottom edge
                    ly = top - s["h"] + 2
                _td(c, s["x"], ly, line, size, font)
        _merge_overlay(page, c, buf)
    if record is not None:
        first = r.pages[0]
        size = (float(first.mediabox.width), float(first.mediabox.height))
        for pg in _record_pages(record, uploaded_sha, size).pages:
            w.add_page(pg)
    w.add_metadata({"/Producer": "Multies"})
    _write(w, out)
    return {"pages": len(w.pages), "signatures": len(placements),
            "signing_record": record is not None, "sha256": _sha256(out)}


def _h_sign(ctx, inputs, opts, out):
    return sign(inputs[0], out, opts["signatures"], ctx.extra, opts.get("audit"))


# ------------------------------------------------------------------ scan-clean


def _pixels(img: Image.Image) -> list:
    flat = getattr(img, "get_flattened_data", None)   # Pillow 12.1+; getdata goes in 14
    return list(flat() if flat else img.getdata())


def _row_means(mask: Image.Image) -> list[float]:
    return _pixels(mask.resize((1, mask.height), Image.BOX))


def _col_means(mask: Image.Image) -> list[float]:
    return _pixels(mask.resize((mask.width, 1), Image.BOX))


def _otsu(gray: Image.Image) -> int:
    hist = gray.histogram()[:256]
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    wb = sb = 0
    best, thr = -1.0, 128
    for t in range(256):
        wb += hist[t]
        if wb == 0:
            continue
        wf = total - wb
        if wf == 0:
            break
        sb += t * hist[t]
        mb, mf = sb / wb, (sum_all - sb) / wf
        between = wb * wf * (mb - mf) ** 2
        if between > best:
            best, thr = between, t
    return thr


def _longest_run(vals: list[float], cut: float) -> tuple[int, int]:
    best = (0, len(vals))
    run_start, best_len = None, 0
    for i, v in enumerate(vals + [-1]):
        if v >= cut and run_start is None:
            run_start = i
        elif v < cut and run_start is not None:
            if i - run_start > best_len:
                best_len, best = i - run_start, (run_start, i)
            run_start = None
    return best


def find_page_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """The paper's box in img (bright region against a darker background),
    or None when no clear page edge shows."""
    small = img.convert("L")
    small.thumbnail((600, 600))
    small = small.filter(ImageFilter.GaussianBlur(2))
    thr = _otsu(small)
    if thr > 235 or thr < 20:
        return None
    mask = small.point(lambda v: 255 if v > thr else 0)
    mask = mask.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    rows, cols = _row_means(mask), _col_means(mask)
    y0, y1 = _longest_run(rows, 255 * 0.35)
    x0, x1 = _longest_run(cols, 255 * 0.35)
    area = (x1 - x0) * (y1 - y0) / (small.width * small.height)
    if area < 0.2 or area > 0.97:
        return None
    sx, sy = img.width / small.width, img.height / small.height
    return (int(x0 * sx), int(y0 * sy), int(x1 * sx), int(y1 * sy))


def find_skew(gray: Image.Image, limit: float = 10.0) -> float:
    """Angle (degrees, counter clockwise) that makes text lines level, found
    by the projection profile: rows of text are sharpest when level."""
    small = gray.copy()
    small.thumbnail((900, 900))
    thr = _otsu(small)
    ink = small.point(lambda v: 255 if v < min(thr, 200) else 0)
    if sum(_row_means(ink)) / max(1, ink.height) < 0.5:   # almost no ink
        return 0.0

    def score(a: float) -> float:
        rows = _row_means(ink.rotate(a, resample=Image.NEAREST, fillcolor=0))
        m = sum(rows) / len(rows)
        return sum((v - m) ** 2 for v in rows)

    best_a, best_s = 0.0, score(0.0)
    a = -limit
    while a <= limit:
        s = score(a)
        if s > best_s:
            best_a, best_s = a, s
        a += 1.0
    for step in (0.25,):
        for k in range(-3, 4):
            a = best_a + k * step
            s = score(a)
            if s > best_s:
                best_a, best_s = a, s
    return best_a


def even_light(band: Image.Image) -> Image.Image:
    """Divides one channel by its smoothed background so shadows and colour
    casts go and the paper comes out white."""
    w, h = band.size
    small = band.resize((max(8, w // 8), max(8, h // 8)), Image.BILINEAR)
    small = small.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(6))
    bg = small.resize((w, h), Image.BILINEAR)
    return ImageMath.lambda_eval(
        lambda a: a["convert"](a["min"](a["float"](a["x"]) * 255.0
                                        / a["max"](a["float"](a["b"]), 1.0), 255.0), "L"),
        x=band, b=bg)


def _edge_colour(img: Image.Image) -> tuple:
    """Average colour of the photo's border (the desk), to fill the corners a turn opens."""
    s = img.resize((32, 32), Image.BOX)
    edge = [s.getpixel((x, y)) for x in range(32) for y in (0, 31)] + \
           [s.getpixel((x, y)) for y in range(32) for x in (0, 31)]
    return tuple(sum(p[k] for p in edge) // len(edge) for k in range(3))


def clean_image(path: str, mode: str) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = SCAN_MAX_PIXELS
    try:
        img = Image.open(path)
        if img.width * img.height > SCAN_MAX_PIXELS:
            raise ToolkitError("A photo is too large (over 40 megapixels).")
        if img.format == "JPEG":
            img.draft("RGB", (SCAN_EDGE * 2, SCAN_EDGE * 2))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except ToolkitError:
        raise
    except Exception:
        raise ToolkitError("A photo could not be read.")
    img.thumbnail((SCAN_EDGE * 1.25, SCAN_EDGE * 1.25), Image.LANCZOS)
    # 1. a rough page box, so the desk does not count as ink when measuring the tilt
    box = find_page_box(img)
    probe = img
    if box:
        x0, y0, x1, y1 = box
        ix, iy = (x1 - x0) // 12, (y1 - y0) // 12
        probe = img.crop((x0 + ix, y0 + iy, x1 - ix, y1 - iy))
    # 2. level the text lines on the whole photo
    angle = find_skew(probe.convert("L"))
    if abs(angle) >= 0.2:
        fill = (255, 255, 255) if not box else _edge_colour(img)
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=fill)
        box = find_page_box(img) if box else None
    # 3. crop to the now level page, a hair inside its edge
    if box:
        x0, y0, x1, y1 = box
        ix, iy = int((x1 - x0) * 0.012), int((y1 - y0) * 0.012)
        img = img.crop((x0 + ix, y0 + iy, x1 - ix, y1 - iy))
    img.thumbnail((SCAN_EDGE, SCAN_EDGE), Image.LANCZOS)
    if mode == "color":
        img = Image.merge("RGB", [even_light(b) for b in img.split()])
        img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=70, threshold=3))
        return img
    g = even_light(img.convert("L"))
    g = ImageOps.autocontrast(g, cutoff=1)
    g = g.filter(ImageFilter.UnsharpMask(radius=1.5, percent=80, threshold=3))
    if mode == "bw":
        t = min(_otsu(g) + 25, 225)
        return g.point(lambda v: 255 if v > t else 0).convert("1")
    return g


def _tesseract_langs() -> set[str]:
    try:
        proc = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True,
                              timeout=30)
    except Exception:
        return set()
    return {l.strip() for l in proc.stdout.splitlines()[1:] if l.strip()}


def check_language(language: str) -> None:
    if not shutil.which("tesseract"):
        raise ToolkitError("Text recognition is not available on this server.")
    have = _tesseract_langs()
    missing = [p for p in language.split("+") if p not in have]
    if missing:
        raise ToolkitError(f"Text recognition for {language} is not installed on this server.")


def scan_clean(inputs: list[str], out: str, work: str, mode: str, ocr: bool,
               language: str = "eng", progress=None) -> dict:
    if not 1 <= len(inputs) <= SCAN_MAX_IMAGES:
        raise ToolkitError(f"Send 1 to {SCAN_MAX_IMAGES} photos.")
    if ocr:
        check_language(language)
    n = len(inputs)
    pages, with_text = [], 0
    for i, p in enumerate(inputs):
        img = clean_image(p, mode)
        dpi = max(72, round(img.width / 8.27))     # page width of A4
        base = os.path.join(work, f"page-{i:03d}")
        if mode == "bw":
            path = base + ".png"
            img.save(path, "PNG", dpi=(dpi, dpi), optimize=True)
        else:
            path = base + ".jpg"
            img.save(path, "JPEG", quality=82, dpi=(dpi, dpi), optimize=True)
        size = (img.width * 72 / dpi, img.height * 72 / dpi)
        img.close()
        del img
        if ocr:
            try:
                proc = subprocess.run(["tesseract", path, base, "-l", language,
                                       "--dpi", str(dpi), "pdf", "txt"],
                                      capture_output=True, text=True, timeout=OCR_TIMEOUT,
                                      env=dict(os.environ, OMP_THREAD_LIMIT="1"))
            except subprocess.TimeoutExpired:
                raise ToolkitError(f"Text recognition took too long on photo {i + 1}.")
            if proc.returncode != 0 or not os.path.exists(base + ".pdf"):
                raise ToolkitError(f"Text recognition failed on photo {i + 1}.")
            try:
                with open(base + ".txt", encoding="utf-8", errors="replace") as fh:
                    if fh.read().strip():
                        with_text += 1
            except OSError:
                pass
            pages.append(("pdf", base + ".pdf", size))
        else:
            pages.append(("img", path, size))
        if progress:
            progress(i + 1, n)
    w = PdfWriter()
    for kind, path, size in pages:
        if kind == "pdf":
            w.add_page(PdfReader(path).pages[0])
        else:
            buf = io.BytesIO()
            c = rl_canvas.Canvas(buf, pagesize=size)
            c.drawImage(path, 0, 0, size[0], size[1])
            c.showPage(); c.save(); buf.seek(0)
            w.add_page(PdfReader(buf).pages[0])
    _write(w, out)
    return {"pages": len(pages), "pages_with_text": with_text}


def _h_scan_clean(ctx, inputs, opts, out):
    return scan_clean(inputs, out, ctx.work, opts["mode"], opts["ocr"] == "yes",
                      opts["language"], progress=ctx.progress)


# ------------------------------------------------------------------ registry

_LANG = Option("language", "choice", "Language", default="eng", choices=OCR_LANGUAGES)

OPERATIONS = {
    "thumbnails": Operation(
        "thumbnails", "Page thumbnails", "pdf", 1, 1, "thumbnails.zip", ZIP,
        max_pages=THUMB_MAX_PAGES, slow=True, limits=(("long_edge_px", THUMB_EDGE),)),
    "organize": Operation(
        "organize", "Organize pages", "pdf", 1, 1, "organized.pdf", PDF,
        (Option("plan", "json", "Page plan", required=True, max_len=40_000),),
        limits=(("max_plan_entries", ORGANIZE_MAX_ENTRIES),)),
    "form-fields": Operation(
        "form-fields", "List form fields", "pdf", 1, 1, "fields.json", JSON,
        limits=(("max_fields", FORM_MAX_FIELDS),)),
    "form-fill": Operation(
        "form-fill", "Fill PDF form", "pdf", 1, 1, "filled.pdf", PDF,
        (Option("values", "json", "Field values", required=True, max_len=200_000),
         Option("flatten", "choice", "Flatten so it cannot be changed", default="no",
                choices=YES_NO)),
        limits=(("max_fields", FORM_MAX_FIELDS), ("max_value_chars", FORM_VALUE_MAX))),
    "compare": Operation(
        "compare", "Compare PDFs", "pdf", 2, 2, "comparison.pdf", PDF,
        (Option("output", "choice", "Result", default="pdf", choices=("pdf", "json")),),
        max_pages=COMPARE_MAX_PAGES, max_bytes=COMPARE_MAX_BYTES, slow=True,
        outputs=("changes.json",),
        limits=(("max_words_per_page", COMPARE_MAX_WORDS_PAGE),)),
    "protect": Operation(
        "protect", "Add password", "pdf", 1, 1, "protected.pdf", PDF,
        (Option("password", "password", "Password (4 to 128 characters)", required=True,
                max_len=128),
         Option("allow_print", "choice", "Allow printing", default="yes", choices=YES_NO),
         Option("allow_copy", "choice", "Allow copying text", default="yes", choices=YES_NO)),
        limits=(("password_min", 4), ("password_max", 128))),
    "stamp": Operation(
        "stamp", "Stamp, page numbers, header and footer", "pdf", 1, 1, "stamped.pdf", PDF,
        (Option("stamp", "choice", "Stamp", default="none",
                choices=("none", "approved", "paid", "draft", "confidential", "copy", "custom")),
         Option("stamp_text", "text", "Custom stamp text", max_len=40),
         Option("position", "choice", "Stamp position", default="top-right",
                choices=("top-left", "top-right", "bottom-left", "bottom-right", "center")),
         Option("page_numbers", "choice", "Page numbers", default="none",
                choices=("none", "n", "page n of N")),
         Option("number_position", "choice", "Page number position", default="bottom-center",
                choices=("bottom-center", "bottom-right", "top-right")),
         Option("number_digits", "choice", "Page number digits", default="latin",
                choices=NUMBER_DIGITS),
         Option("header", "text", "Header", max_len=120),
         Option("footer", "text", "Footer", max_len=120),
         Option("ranges", "ranges", "Pages", max_len=200)),
        max_pages=STAMP_MAX_PAGES),
    "sign": Operation(
        "sign", "Sign PDF", "pdf", 1, 1, "signed.pdf", PDF,
        (Option("signatures", "json", "Signature places", required=True, max_len=20_000),
         Option("audit", "json", "Signing record", max_len=100_000)),
        extra=ExtraFiles("images", "Signature images", 1, SIGN_MAX_IMAGES, (".png",),
                         max_bytes_each=SIGN_IMAGE_MAX_BYTES),
        limits=(("max_signatures", SIGN_MAX_PLACEMENTS), ("max_events", SIGN_MAX_EVENTS),
                ("max_image_px", SIGN_IMAGE_MAX_PX))),
    "scan-clean": Operation(
        "scan-clean", "Scan to clean PDF", "file", 1, SCAN_MAX_IMAGES, "scan.pdf", PDF,
        (Option("ocr", "choice", "Make text searchable", default="no", choices=YES_NO),
         _LANG,
         Option("mode", "choice", "Colour", default="color", choices=("color", "gray", "bw"))),
        max_bytes=SCAN_MAX_BYTES, slow=True, exts=(".jpg", ".jpeg", ".png", ".webp"),
        limits=(("max_megapixels_each", SCAN_MAX_PIXELS // 1_000_000),
                ("long_edge_px", SCAN_EDGE), ("ocr_seconds_per_page", OCR_TIMEOUT))),
}

HANDLERS = {
    "thumbnails": _h_thumbnails,
    "organize": _h_organize,
    "form-fields": _h_form_fields,
    "form-fill": _h_form_fill,
    "compare": _h_compare,
    "protect": _h_protect,
    "stamp": _h_stamp,
    "sign": _h_sign,
    "scan-clean": _h_scan_clean,
}
