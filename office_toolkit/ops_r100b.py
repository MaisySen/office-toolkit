"""Engine R100b: slides and drawings (Impress and Draw) and the image service.

slides-convert: PowerPoint (pptx, ppt), OpenDocument slides (odp) and drawings
(odg) to PDF and between each other; a drawing to SVG; a PDF into an editable
drawing (odg). LibreOffice runs headless with a private profile per job, no
network (an unroutable proxy), a hard kill on time.

image-transform: resize, crop (a box or by gravity), smart crop and format
for the Multies image links. Pillow only, metadata stripped, orientation
applied, a pixel cap against decompression bombs, output edges capped.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile

from office_toolkit.opspec import Operation, Option
from office_toolkit.pdfops import ToolkitError

MB = 1024 * 1024
SLIDES_MAX_BYTES = 50 * MB
SLIDES_TIMEOUT = 150
PDF_TO_DRAW_MAX_PAGES = 40
ZIP_MAX_ENTRIES = 5000
ZIP_MAX_UNPACKED = 400 * MB

IMAGE_MAX_BYTES = 20 * MB
IMAGE_MAX_PIXELS = 40_000_000
IMAGE_MAX_EDGE = 4096
SMART_SAMPLE = 96

SLIDE_EXTS = (".pptx", ".ppt", ".odp")
DRAW_EXTS = (".odg",)
SLIDES_EXTS = SLIDE_EXTS + DRAW_EXTS + (".pdf",)
SLIDES_TARGETS = ("pdf", "pptx", "odp", "odg", "svg")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ODF_MIME = {".odp": b"application/vnd.oasis.opendocument.presentation",
             ".odg": b"application/vnd.oasis.opendocument.graphics"}

# which targets each source takes (the same format is never offered back)
ALLOWED = {
    ".pptx": ("pdf", "odp"),
    ".ppt": ("pdf", "pptx", "odp"),
    ".odp": ("pdf", "pptx"),
    ".odg": ("pdf", "svg"),
    ".pdf": ("odg",),
}

# LibreOffice export filters: slides and drawings use their own PDF writers
_FILTERS = {
    ("slides", "pdf"): "pdf:impress_pdf_Export",
    ("slides", "pptx"): "pptx:Impress MS PowerPoint 2007 XML",
    ("slides", "odp"): "odp:impress8",
    ("draw", "pdf"): "pdf:draw_pdf_Export",
    ("draw", "svg"): "svg:draw_svg_Export",
    ("pdf", "odg"): "odg:draw8",
}
MIME = {"pdf": "application/pdf", "svg": "image/svg+xml",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "odp": "application/vnd.oasis.opendocument.presentation",
        "odg": "application/vnd.oasis.opendocument.graphics"}


def _head(path: str, n: int = 2048) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def _check_zip(path: str, ext: str) -> None:
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > ZIP_MAX_ENTRIES or sum(i.file_size for i in infos) > ZIP_MAX_UNPACKED:
                raise ToolkitError("This file is too large inside to convert.")
            names = {i.filename for i in infos}
            if ext in _ODF_MIME:
                if "mimetype" not in names or z.read("mimetype").strip() != _ODF_MIME[ext]:
                    raise ToolkitError(f"That file is not an {ext[1:].upper()} file.")
            elif ext == ".pptx" and "ppt/presentation.xml" not in names:
                raise ToolkitError("That file is not a PowerPoint file.")
    except zipfile.BadZipFile:
        raise ToolkitError("That file is damaged or is not what its name says.")


def sniff_slides(path: str, ext: str) -> None:
    head = _head(path)
    if ext in (".pptx", ".odp", ".odg"):
        if not head.startswith(b"PK\x03\x04"):
            raise ToolkitError("That file is damaged or is not what its name says.")
        _check_zip(path, ext)
    elif ext == ".ppt":
        if not head.startswith(OLE_MAGIC):
            raise ToolkitError("That file is not a PowerPoint file.")
    elif ext == ".pdf":
        if b"%PDF-" not in head[:1024]:
            raise ToolkitError("That file is not a PDF.")
    else:
        raise ToolkitError("That file type is not supported.")


def sniff_image(path: str, ext: str) -> None:
    if image_kind(_head(path, 16)) is None:
        raise ToolkitError("That file is not a supported image.")


def image_kind(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


SNIFFERS = {**{e: sniff_slides for e in (".pptx", ".ppt", ".odp", ".odg")}, **{e: sniff_image for e in (".gif",)}}


def family(ext: str) -> str:
    if ext in SLIDE_EXTS:
        return "slides"
    if ext in DRAW_EXTS:
        return "draw"
    if ext == ".pdf":
        return "pdf"
    raise ToolkitError("That file type is not supported.")


def plan(ext: str, to: str) -> str:
    """The LibreOffice filter for a source and target, or a plain refusal."""
    ext = ext.lower()
    if ext not in ALLOWED:
        raise ToolkitError("Slides (PPTX, PPT, ODP), drawings (ODG) or a PDF only.")
    if to not in ALLOWED[ext]:
        targets = [t.upper() for t in ALLOWED[ext]]
        words = targets[0] if len(targets) == 1 else ", ".join(targets[:-1]) + " or " + targets[-1]
        raise ToolkitError(f"This {ext[1:].upper()} file can become {words} here.")
    return _FILTERS[(family(ext), to)]


def _pdf_pages(path: str) -> int:
    from pypdf import PdfReader
    try:
        r = PdfReader(path)
        if r.is_encrypted:
            raise ToolkitError("This PDF is locked. Remove its password first.")
        return len(r.pages)
    except ToolkitError:
        raise
    except Exception:
        raise ToolkitError("The PDF could not be read. It may be damaged.")


def soffice_to(src: str, out_dir: str, convert_to: str, infilter: str | None = None,
               timeout: int = SLIDES_TIMEOUT) -> str:
    from office_toolkit import ops_p2b as B
    if not shutil.which("soffice"):
        raise ToolkitError("LibreOffice is not available on this machine.")
    os.makedirs(out_dir, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="lo-profile-")
    try:
        cmd = ["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
               "--norestore", "--nolockcheck", "--nodefault", "--nologo"]
        if infilter:
            cmd.append(f"--infilter={infilter}")
        cmd += ["--convert-to", convert_to, "--outdir", out_dir, src]
        env = dict(os.environ, HOME=profile, SAL_USE_VCLPLUGIN="svp")
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env[k] = "http://127.0.0.1:9"
        env["no_proxy"] = ""
        proc = B._run(cmd, timeout, env=env)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    ext = convert_to.split(":", 1)[0]
    out = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + "." + ext)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        if proc.returncode in (124, 137):
            raise ToolkitError("The file took too long to convert.")
        raise ToolkitError("The file could not be converted. It may be damaged.")
    return out


def slides_convert(ctx, inputs, opts, out):
    src = inputs[0]
    ext = os.path.splitext(src)[1].lower()
    to = (opts.get("to") or "pdf").lower()
    filt = plan(ext, to)
    sniff_slides(src, ext)
    infilter = None
    if ext == ".pdf":
        n = _pdf_pages(src)
        if n > PDF_TO_DRAW_MAX_PAGES:
            raise ToolkitError(f"PDFs up to {PDF_TO_DRAW_MAX_PAGES} pages can become a drawing here.")
        infilter = "draw_pdf_import"
    ctx.progress(0, 1)
    # a plain ASCII name inside the job folder: LibreOffice names its output after it
    staged = os.path.join(ctx.work, "in" + ext)
    shutil.copyfile(src, staged)
    made = soffice_to(staged, os.path.join(ctx.work, "out"), filt, infilter=infilter)
    dest = ctx.output(f"converted.{to}")
    shutil.move(made, dest)
    ctx.progress(1, 1)
    facts = {"to": to}
    if to == "pdf":
        try:
            facts["pages"] = _pdf_pages(dest)
        except ToolkitError:
            pass
    return facts


# ------------------------------------------------------------------ images

FITS = ("inside", "cover", "contain", "fill")
GRAVITIES = ("smart", "center", "top", "bottom", "left", "right")
FORMATS = ("auto", "jpeg", "png", "webp")


def parse_area(raw: str | None) -> tuple[int, int, int, int] | None:
    """'x,y,w,h' in source pixels, or nothing."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        raise ToolkitError("Crop area must be four numbers: x, y, width, height.")
    try:
        x, y, w, h = (int(p.strip()) for p in parts)
    except ValueError:
        raise ToolkitError("Crop area must be four numbers: x, y, width, height.")
    if x < 0 or y < 0 or w < 1 or h < 1:
        raise ToolkitError("Crop area must sit inside the image.")
    return x, y, w, h


def target_size(sw: int, sh: int, w: int, h: int, fit: str) -> tuple[int, int, int, int]:
    """Returns (canvas_w, canvas_h, scaled_w, scaled_h) for the fit, never enlarging past the cap."""
    if not w and not h:
        w, h = sw, sh
        fit = "inside"
    if not w:
        w = max(1, round(sw * h / sh))
    if not h:
        h = max(1, round(sh * w / sw))
    w, h = min(w, IMAGE_MAX_EDGE), min(h, IMAGE_MAX_EDGE)
    if fit == "fill" or fit == "cover":
        return w, h, w, h
    scale = min(w / sw, h / sh)
    if fit == "inside":
        scale = min(scale, 1.0)  # inside never upscales
    sw2, sh2 = max(1, round(sw * scale)), max(1, round(sh * scale))
    if fit == "inside":
        return sw2, sh2, sw2, sh2
    return w, h, sw2, sh2  # contain: letterboxed on a canvas of w x h


def _prefix(vals: list[float]) -> list[float]:
    out = [0.0]
    for v in vals:
        out.append(out[-1] + v)
    return out


def smart_offset(scores: list[float], window: int) -> int:
    """The start of the best window over 1D interest scores, with a small pull to the middle."""
    n = len(scores)
    if window >= n:
        return 0
    pre = _prefix(scores)
    total = pre[-1] or 1.0
    best, best_at = -1.0, (n - window) // 2
    mid = (n - window) / 2
    for i in range(0, n - window + 1):
        s = (pre[i + window] - pre[i]) / total
        s *= 1.0 - 0.15 * (abs(i - mid) / max(mid, 1))
        if s > best + 1e-9:
            best, best_at = s, i
    return best_at


def interest(img) -> tuple[list[float], list[float]]:
    """Column and row interest on a small copy: edges plus colour saturation (busy, colourful parts win)."""
    from PIL import ImageFilter
    small = img.convert("RGB")
    sw, sh = small.size
    k = SMART_SAMPLE / max(sw, sh)
    if k < 1:
        small = small.resize((max(1, round(sw * k)), max(1, round(sh * k))))
    w, h = small.size
    edges = small.convert("L").filter(ImageFilter.FIND_EDGES)
    sat = small.convert("HSV").getchannel("S")
    e, s = list(edges.getdata()), list(sat.getdata())
    cols, rows = [0.0] * w, [0.0] * h
    for y in range(h):
        for x in range(w):
            if x in (0, w - 1) or y in (0, h - 1):
                continue  # the filter's own frame is not content
            v = e[y * w + x] + 0.35 * s[y * w + x]
            cols[x] += v
            rows[y] += v
    return cols, rows


def crop_box(sw: int, sh: int, tw: int, th: int, gravity: str, img=None) -> tuple[int, int, int, int]:
    """The source box with the target's shape, placed by gravity (or by interest for smart)."""
    r = tw / th
    if sw / sh > r:
        cw, ch = max(1, round(sh * r)), sh
    else:
        cw, ch = sw, max(1, round(sw / r))
    x, y = (sw - cw) // 2, (sh - ch) // 2
    if gravity == "left":
        x = 0
    elif gravity == "right":
        x = sw - cw
    elif gravity == "top":
        y = 0
    elif gravity == "bottom":
        y = sh - ch
    elif gravity == "smart" and img is not None and (cw < sw or ch < sh):
        cols, rows = interest(img)
        if cw < sw:
            n = len(cols)
            x = round(smart_offset(cols, max(1, round(cw * n / sw))) * sw / n)
            x = min(max(0, x), sw - cw)
        else:
            n = len(rows)
            y = round(smart_offset(rows, max(1, round(ch * n / sh))) * sh / n)
            y = min(max(0, y), sh - ch)
    return x, y, x + cw, y + ch


def out_format(fmt: str, kind: str, has_alpha: bool) -> str:
    if fmt != "auto":
        return fmt
    if kind in ("png", "gif"):
        return "png" if has_alpha or kind == "png" else "jpeg"
    return "webp" if kind == "webp" else "jpeg"


def image_transform(ctx, inputs, opts, out):
    from PIL import Image, ImageOps
    src = inputs[0]
    kind = image_kind(_head(src, 16))
    if kind is None:
        raise ToolkitError("That file is not a supported image.")
    w, h = int(opts.get("w") or 0), int(opts.get("h") or 0)
    fit = opts.get("fit") or "inside"
    gravity = opts.get("gravity") or "center"
    fmt = opts.get("format") or "auto"
    q = int(opts.get("quality") or 82)
    area = parse_area(opts.get("area"))
    Image.MAX_IMAGE_PIXELS = IMAGE_MAX_PIXELS
    try:
        with Image.open(src) as im0:
            if im0.width * im0.height > IMAGE_MAX_PIXELS:
                raise ToolkitError("Images up to 40 megapixels can be changed here.")
            im0.seek(0)  # the first frame of a GIF or animated WebP
            im = ImageOps.exif_transpose(im0)
            im.load()
    except ToolkitError:
        raise
    except Image.DecompressionBombError:
        raise ToolkitError("Images up to 40 megapixels can be changed here.")
    except Exception:
        raise ToolkitError("This image can't be opened. It may be damaged.")
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if has_alpha else "RGB")
    if area:
        x, y, aw, ah = area
        if x >= im.width or y >= im.height:
            raise ToolkitError("Crop area must sit inside the image.")
        im = im.crop((x, y, min(im.width, x + aw), min(im.height, y + ah)))
    sw, sh = im.size
    cw, chh, scw, sch = target_size(sw, sh, w, h, fit)
    if fit == "cover" and (w or h):
        box = crop_box(sw, sh, cw, chh, gravity, im if gravity == "smart" else None)
        im = im.crop(box).resize((cw, chh), Image.LANCZOS)
    elif fit == "contain" and (w and h):
        scaled = im.resize((scw, sch), Image.LANCZOS)
        bg = (255, 255, 255, 0) if has_alpha else (255, 255, 255)
        canvas = Image.new(im.mode, (cw, chh), bg)
        canvas.paste(scaled, ((cw - scw) // 2, (chh - sch) // 2))
        im = canvas
    elif (scw, sch) != (sw, sh):
        im = im.resize((scw, sch), Image.LANCZOS)
    target = out_format(fmt, kind, has_alpha)
    ext = "jpg" if target == "jpeg" else target
    dest = ctx.output(f"image.{ext}")
    if target == "jpeg":
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        im.save(dest, "JPEG", quality=q, optimize=True, progressive=True)
    elif target == "webp":
        im.save(dest, "WEBP", quality=q, method=4)
    else:
        im.save(dest, "PNG", optimize=True)
    return {"width": im.width, "height": im.height, "format": target}


OPERATIONS = {
    "slides-convert": Operation(
        "slides-convert", "Convert slides and drawings", "file", 1, 1, "converted.pdf", "application/pdf",
        (Option("to", "choice", "Convert to", default="pdf", choices=SLIDES_TARGETS),),
        max_bytes=SLIDES_MAX_BYTES, slow=True, exts=SLIDES_EXTS,
        outputs=tuple(f"converted.{t}" for t in SLIDES_TARGETS if t != "pdf"),
        limits=(("pdf_to_drawing_max_pages", PDF_TO_DRAW_MAX_PAGES),)),
    "image-transform": Operation(
        "image-transform", "Resize, crop and convert an image", "file", 1, 1, "image.jpg", "image/jpeg",
        (Option("w", "int", "Width", default=0, min=0, max=IMAGE_MAX_EDGE),
         Option("h", "int", "Height", default=0, min=0, max=IMAGE_MAX_EDGE),
         Option("fit", "choice", "Fit", default="inside", choices=FITS),
         Option("gravity", "choice", "Crop from", default="center", choices=GRAVITIES),
         Option("format", "choice", "Format", default="auto", choices=FORMATS),
         Option("quality", "int", "Quality", default=82, min=30, max=95),
         Option("area", "text", "Crop area", default="", max_len=40)),
        max_bytes=IMAGE_MAX_BYTES, exts=IMAGE_EXTS, outputs=("image.png", "image.webp"),
        limits=(("max_pixels", IMAGE_MAX_PIXELS), ("max_edge", IMAGE_MAX_EDGE))),
}
HANDLERS = {"slides-convert": slides_convert, "image-transform": image_transform}
