"""Unicode text drawing for the PDF writers (engine R68).

Every op that writes words onto a page (stamp, page numbers, header and
footer, watermark, form values, comparison and signing records) draws them
through here so Nepali, Hindi, Arabic and Chinese, Japanese and Korean text
gets real glyphs instead of empty boxes:

- the text is split into runs by script, each run set in a bundled font
  that has its letters (Noto Sans, Noto Sans Devanagari, Noto Sans Arabic,
  Droid Sans Fallback for CJK; office_toolkit/fonts, OFL and Apache 2.0);
- Devanagari and Arabic runs are shaped with HarfBuzz (uharfbuzz, Apache
  2.0) so vowel signs, conjuncts and joined letters come out right;
- widths are the shaped widths, so centred and right aligned text lands
  where it should.

Pure helpers over a reportlab canvas; nothing here reads or writes files
except registering the fonts once per process.
"""
from __future__ import annotations

import glob
import os
import re
import tempfile
import threading
import unicodedata

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

# (reportlab name, regular file, bold file or None)
_FACES = (
    ("MTSans", "NotoSans-Regular.ttf", "NotoSans-Bold.ttf"),
    ("MTDeva", "NotoSansDevanagari-Regular.ttf", None),
    ("MTArab", "NotoSansArabic-Regular.ttf", None),
    ("MTCJK", "DroidSansFallback.ttf", None),
)
_SHAPED = {"MTDeva", "MTArab"}
_lock = threading.Lock()
_maps: dict[str, set] = {}
_bold_ok = {"MTSans": False}

_DEVA_RE = re.compile(r"[\u0900-\u097f\ua8e0-\ua8ff]")
_RTL_RE = re.compile(r"[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]")
_LETTER_RE = re.compile(r"[^\W\d_]")


def font_file(fn: str) -> str:
    """Path of a bundled font; the CJK fallback rides as parts in a plain
    checkout and is joined once into a temp folder."""
    p = os.path.join(FONT_DIR, fn)
    if os.path.exists(p):
        return p
    parts = sorted(glob.glob(os.path.join(FONT_DIR, "parts", fn + ".part*")))
    if not parts:
        return p
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


def available() -> bool:
    try:
        register()
        return bool(_maps)
    except Exception:
        return False


def register() -> None:
    if _maps:
        return
    with _lock:
        if _maps:
            return
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        found: dict[str, set] = {}
        for name, reg, bold in _FACES:
            path = font_file(reg)
            if not os.path.exists(path):
                continue
            f = TTFont(name, path)
            pdfmetrics.registerFont(f)
            found[name] = set(f.face.charToGlyph.keys())
            if bold and os.path.exists(font_file(bold)):
                pdfmetrics.registerFont(TTFont(name + "B", font_file(bold)))
                _bold_ok[name] = True
        _maps.update(found)


def _face(name: str, bold: bool) -> str:
    return name + "B" if bold and _bold_ok.get(name) else name


def _pick(ch: str, prev: str | None) -> str:
    o = ord(ch)
    # spaces, punctuation and digits stay in the current run when its font has them
    if prev and o in _maps.get(prev, ()) and (ch.isspace() or not _LETTER_RE.match(ch)) \
            and unicodedata.category(ch) not in ("Mn", "Mc"):
        return prev
    if prev in _SHAPED and unicodedata.category(ch) in ("Mn", "Mc", "Cf") and o in _maps[prev]:
        return prev
    for name, _, _ in _FACES:
        if name in _maps and o in _maps[name]:
            return name
    return prev or next(iter(_maps), "MTSans")


def runs(text: str) -> list[tuple[str, str]]:
    """[(font, text)] runs, each run drawable in one bundled font."""
    register()
    out: list[tuple[str, str]] = []
    prev = None
    for ch in text:
        f = _pick(ch, prev)
        if out and out[-1][0] == f:
            out[-1] = (f, out[-1][1] + ch)
        else:
            out.append((f, ch))
        prev = f
    return out


def _shape(chunk: str, font: str, size: float):
    """(width, drawable) for one run; shaped runs come back as reportlab's
    ShapedStr, which carries the glyph positions."""
    from reportlab.pdfbase import pdfmetrics
    plain = pdfmetrics.stringWidth(chunk, font, size)
    if font.rstrip("B") not in _SHAPED:
        return plain, chunk
    try:
        from reportlab.pdfbase.ttfonts import ShapedStr, shapeFragWord
        from reportlab.lib.abag import ABag
        w = shapeFragWord([plain, (ABag(fontName=font, fontSize=size), chunk)])
        if len(w) == 2 and isinstance(w[1][1], ShapedStr):
            return float(w[0]), w[1][1]
        # the shaper split the word over several pieces: join them back
        if len(w) >= 2:
            s = w[1][1]
            if isinstance(s, ShapedStr):
                return float(w[0]), s
    except Exception:
        pass
    return plain, chunk


def _visual(text: str) -> str:
    """Small bidi: an Arabic or Hebrew word keeps its letters (the shaper
    puts them in visual order); a line that is mostly right to left runs
    its words right to left."""
    words = re.findall(r"\S+|\s+", text)
    if not _RTL_RE.search(text):
        return text
    rtl = len(_RTL_RE.findall(text)) > len(_LETTER_RE.findall(text)) / 2
    groups: list[list] = []
    for w in words:
        is_rtl = bool(_RTL_RE.search(w)) or (w.isspace() and groups and groups[-1][0])
        if groups and groups[-1][0] == is_rtl:
            groups[-1][1].append(w)
        else:
            groups.append([is_rtl, [w]])
    if rtl:
        groups.reverse()
    out = []
    for is_rtl, ws in groups:
        out.extend(reversed(ws) if is_rtl else ws)
    return "".join(out)


def _pieces(text: str, size: float, bold: bool):
    text = _visual(text)
    return [(_face(f, bold),) + _shape(t, _face(f, bold), size) for f, t in runs(text)]


def width(text: str, size: float, bold: bool = False) -> float:
    return sum(w for _, w, _ in _pieces(text, size, bold))


def draw(c, x: float, y: float, text: str, size: float, bold: bool = False,
         align: str = "left") -> float:
    """Draws one line at (x, y) on canvas c. align is left, center or right
    (x is then the centre or the right edge). Returns the drawn width."""
    pieces = _pieces(text, size, bold)
    total = sum(w for _, w, _ in pieces)
    if align == "center":
        x -= total / 2
    elif align == "right":
        x -= total
    for font, w, s in pieces:
        c.setFont(font, size)
        try:
            c.drawString(x, y, s)
        except Exception:
            c.drawString(x, y, str(s))
        x += w
    return total


def needs_unicode(text: str) -> bool:
    """True when text has letters outside Latin 1 (Helvetica cannot draw them)."""
    try:
        text.encode("latin-1")
        return False
    except UnicodeEncodeError:
        return True


def has_devanagari(text: str) -> bool:
    return bool(_DEVA_RE.search(text or ""))


def missing(text: str) -> str:
    """The characters no bundled font can draw (for a plain message)."""
    register()
    out = []
    for ch in text:
        if ch.isspace() or unicodedata.category(ch)[0] == "C":
            continue
        if not any(ord(ch) in m for m in _maps.values()) and ch not in out:
            out.append(ch)
    return "".join(out)
