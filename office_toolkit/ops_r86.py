"""R86 ops (Multies Tools round B): video and audio, HEIC photos, e-books.

Video and audio run through FFmpeg as a separate process: an LGPL build
(no GPL parts: H.264 is written with OpenH264, BSD; VP8 with libvpx, BSD;
Opus with libopus, BSD; MP3 with LAME, LGPL; AAC with FFmpeg's own
encoder). Inputs are hostile: the demuxer is fixed from the sniffed type
(no playlists, no concat, no network protocols), every run has a hard time
limit and one thread, and pictures are scaled down before encoding so the
512 MB host never holds a big frame. E-books (EPUB, MOBI) are read with our
own small readers (zip and PalmDOC), rebuilt as one sanitised HTML file
(pictures inline as data: images, nothing fetched) and printed to PDF with
LibreOffice, the same path as Anything to PDF.
"""
from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import shutil
import struct
import subprocess
import time
import zipfile

from office_toolkit.opspec import Operation, Option
from office_toolkit.pdfops import ToolkitError

# ------------------------------------------------------------------ caps
MB = 1024 * 1024
MEDIA_MAX_BYTES = 100 * MB          # tighter than a plan cap only on the biggest plan
VIDEO_MAX_SECONDS = 3 * 60          # re-encoding a video (the free host has a tenth of a CPU:
                                    # 30 s of 1080p is about 5 to 9 CPU seconds, so 3 minutes stays
                                    # under the time limit)
REMUX_MAX_SECONDS = 3 * 60 * 60     # a container change copies the streams; any length
AUDIO_MAX_SECONDS = 60 * 60
GIF_MAX_SECONDS = 15                # a GIF takes the first 15 seconds
GIF_WIDTH = 480
VIDEO_MAX_W, VIDEO_MAX_H = 1280, 720
FFMPEG_TIMEOUT = 12 * 60            # under the engine's 15 minute stale mark
PROBE_TIMEOUT = 30
PHOTO_MAX_PIXELS = 60_000_000
PHOTO_TIMEOUT = 120
EBOOK_MAX_BYTES = 30 * MB
EBOOK_MAX_HTML = 4 * MB             # all chapters together, after cleaning
EBOOK_MAX_CHAPTERS = 400
EBOOK_MAX_IMAGES = 300
EBOOK_MAX_IMAGE_BYTES = 4 * MB      # one picture
EBOOK_MAX_ALL_IMAGES = 40 * MB
EBOOK_ZIP_MAX_UNCOMPRESSED = 200 * MB
EBOOK_ZIP_MAX_ENTRIES = 5000
EBOOK_SOFFICE_TIMEOUT = 420

VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac")
MEDIA_EXTS = VIDEO_EXTS + AUDIO_EXTS
PHOTO_EXTS = (".heic", ".heif")
EBOOK_EXTS = (".epub", ".mobi", ".azw", ".prc")

MEDIA_TARGETS = ("mp4", "mov", "webm", "mp3", "wav", "m4a", "gif")
AUDIO_TARGETS = ("mp3", "wav", "m4a")

# --------------------------------------------------------------- sniffing
_MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot", b"uuid")
_HEIF_BRANDS = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"heif")


def _head(path: str, n: int = 4096) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def media_kind(path: str) -> str | None:
    """The container a file really is: mov, matroska, avi, wav, mp3, aac, ogg or flac."""
    h = _head(path, 64)
    if len(h) < 12:
        return None
    if h[4:8] in _MP4_BOXES:
        return "mov"
    if h.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    if h.startswith(b"RIFF") and h[8:12] == b"AVI ":
        return "avi"
    if h.startswith(b"RIFF") and h[8:12] == b"WAVE":
        return "wav"
    if h.startswith(b"OggS"):
        return "ogg"
    if h.startswith(b"fLaC"):
        return "flac"
    if h.startswith(b"ID3"):
        return "mp3"
    if h[0] == 0xFF and (h[1] & 0xF6) == 0xF0:
        return "aac"      # ADTS
    if h[0] == 0xFF and (h[1] & 0xE0) == 0xE0:
        return "mp3"      # a bare MPEG audio frame
    return None


def _sniff_media(path: str, ext: str) -> None:
    kind = media_kind(path)
    want = {".mp4": "mov", ".m4v": "mov", ".mov": "mov", ".m4a": "mov", ".webm": "matroska",
            ".mkv": "matroska", ".avi": "avi", ".wav": "wav", ".mp3": "mp3", ".aac": "aac",
            ".ogg": "ogg", ".oga": "ogg", ".opus": "ogg", ".flac": "flac"}[ext]
    ok = kind == want or (ext == ".aac" and kind == "mp3") or (ext == ".mp3" and kind == "aac")
    if not ok:
        raise ToolkitError("That file is not a video or audio file this tool reads "
                           "(its contents say otherwise).")


def _sniff_photo(path: str, ext: str) -> None:
    h = _head(path, 32)
    if not (h[4:8] == b"ftyp" and h[8:12] in _HEIF_BRANDS):
        raise ToolkitError("That file is not a HEIC photo.")


def _sniff_ebook(path: str, ext: str) -> None:
    h = _head(path, 80)
    if ext == ".epub":
        if not h.startswith(b"PK\x03\x04"):
            raise ToolkitError("That file is not an EPUB e-book.")
        return
    if h[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
        raise ToolkitError("That file is not a MOBI e-book.")


SNIFFERS = {**{e: _sniff_media for e in MEDIA_EXTS}, **{e: _sniff_photo for e in PHOTO_EXTS},
            **{e: _sniff_ebook for e in EBOOK_EXTS}}

# ----------------------------------------------------------------- FFmpeg


def ffmpeg_bin(name: str = "ffmpeg") -> str:
    env = os.environ.get("TOOLKIT_" + name.upper())
    p = env or shutil.which(name)
    if not p:
        raise ToolkitError("Video and audio tools are not available on this machine.")
    return p


_SAFE_IN = ["-protocol_whitelist", "file", "-nostdin", "-hide_banner"]


def probe(path: str, fmt: str) -> dict:
    """Duration, size and the first video and audio stream of a file."""
    cmd = ["timeout", "-k", "5", str(PROBE_TIMEOUT), ffmpeg_bin("ffprobe"), "-v", "error",
           "-protocol_whitelist", "file", "-f", fmt, "-show_format", "-show_streams",
           "-of", "json", path]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT + 15, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolkitError("The file took too long to read.")
    if p.returncode != 0:
        raise ToolkitError("This video or audio file can't be read. It may be damaged.")
    try:
        data = json.loads(p.stdout or b"{}")
    except ValueError:
        raise ToolkitError("This video or audio file can't be read. It may be damaged.")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    if not dur:
        for s in (video, audio):
            try:
                dur = max(dur, float((s or {}).get("duration") or 0))
            except ValueError:
                pass
    return {"duration": dur, "video": video, "audio": audio}


def run_ffmpeg(args: list[str], total: float, progress, timeout: int = FFMPEG_TIMEOUT,
               err_path: str | None = None) -> None:
    """Runs FFmpeg with a hard time limit, reporting progress in whole seconds of the output."""
    # One thread for decoding (input option), filters and encoding (output option).
    args = args[:-1] + ["-threads", "1", args[-1]]
    cmd = ["timeout", "-k", "5", str(timeout), ffmpeg_bin(), *_SAFE_IN, "-y", "-filter_threads", "1",
           "-threads", "1", "-progress", "pipe:1", "-nostats", "-loglevel", "error", *args]
    err = open(err_path or os.devnull, "wb")
    t0 = time.time()
    lock = _media_lock()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL)
        total_i = max(1, int(round(total or 0)))
        last = -1
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    secs = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                done = min(total_i, int(secs))
                if done != last:
                    last = done
                    progress(done, total_i)
            if time.time() - t0 > timeout + 20:
                proc.kill()
                break
        rc = proc.wait(timeout=30)
    finally:
        err.close()
        _media_unlock(lock)
    if rc in (124, 137, -9):
        raise ToolkitError("The file took too long to convert. Try a shorter clip.")
    if rc != 0:
        raise ToolkitError("This video or audio file could not be converted. It may be damaged.")


MEDIA_LOCK = os.path.join(os.environ.get("TMPDIR", "/tmp"), "multies-media.lock")
MEDIA_LOCK_WAIT = 10 * 60


def _media_lock():
    """One FFmpeg at a time on this machine (every server process), so two
    video jobs never share the 512 MB host. Waits up to ten minutes."""
    import fcntl
    fh = open(MEDIA_LOCK, "a+")
    end = time.time() + MEDIA_LOCK_WAIT
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() > end:
                fh.close()
                raise ToolkitError("The video tools are busy. Try again in a few minutes.")
            time.sleep(0.5)


def _media_unlock(fh) -> None:
    import fcntl
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def _scale(max_w: int, max_h: int) -> str:
    return (f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2")


_VIDEO_KBPS = {"high": 2500, "balanced": 1500, "small": 800}
_AUDIO_KBPS = {"high": 192, "balanced": 128, "small": 96}
_MP3_Q = {"high": "2", "balanced": "4", "small": "6"}


def _video_args(target: str, quality: str, has_audio: bool, max_w: int = VIDEO_MAX_W,
                max_h: int = VIDEO_MAX_H, kbps: int | None = None) -> list[str]:
    kbps = kbps or _VIDEO_KBPS[quality]
    v = ["-vf", _scale(max_w, max_h), "-pix_fmt", "yuv420p"]
    if target == "webm":
        v += ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-b:v", f"{kbps}k",
              "-auto-alt-ref", "0"]
        a = ["-c:a", "libopus", "-b:a", f"{min(128, _AUDIO_KBPS[quality])}k"] if has_audio else ["-an"]
    else:
        v += ["-c:v", "libopenh264", "-b:v", f"{kbps}k", "-rc_mode", "bitrate",
              "-allow_skip_frames", "1"]
        a = ["-c:a", "aac", "-b:a", f"{_AUDIO_KBPS[quality]}k"] if has_audio else ["-an"]
        v += ["-movflags", "+faststart"] if target in ("mp4", "mov") else []
    return v + a + ["-map_metadata", "-1", "-sn", "-dn"]


def _need(info: dict, kind: str) -> None:
    if kind == "video" and not info["video"]:
        raise ToolkitError("This file has no video picture to convert. Pick an audio format instead.")
    if kind == "audio" and not info["audio"]:
        raise ToolkitError("This file has no sound to convert.")


def _cap(seconds: float, limit: int, what: str) -> None:
    if seconds > limit:
        mins = limit // 60
        raise ToolkitError(f"{what} up to {mins} minutes long can be converted here; "
                           f"this one is {int(seconds // 60)} minutes. Trim it first.")


def media_convert(ctx, inputs, opts, out):
    src = inputs[0]
    fmt = media_kind(src) or "mov"
    target = opts.get("to") or "mp4"
    quality = opts.get("quality") or "balanced"
    info = probe(src, fmt)
    dur = info["duration"]
    v, a = info["video"], info["audio"]
    vcodec = (v or {}).get("codec_name")
    acodec = (a or {}).get("codec_name")
    dest = ctx.output(f"converted.{target}")
    err = os.path.join(ctx.work, "ffmpeg.err")
    base = ["-f", fmt, "-i", src]
    facts: dict = {"seconds": int(round(dur))}
    if target in AUDIO_TARGETS:
        _need(info, "audio")
        _cap(dur, AUDIO_MAX_SECONDS, "Recordings")
        if target == "wav":
            args = ["-vn", "-c:a", "pcm_s16le", "-map_metadata", "-1"]
        elif target == "mp3":
            args = ["-vn", "-c:a", "libmp3lame", "-q:a", _MP3_Q[quality], "-map_metadata", "-1"]
        elif acodec == "aac":
            args = ["-vn", "-c:a", "copy", "-map_metadata", "-1"]
            facts["copied"] = True
        else:
            args = ["-vn", "-c:a", "aac", "-b:a", f"{_AUDIO_KBPS[quality]}k", "-map_metadata", "-1"]
        fmt_out = {"wav": "wav", "mp3": "mp3", "m4a": "ipod"}[target]
        run_ffmpeg(base + args + ["-f", fmt_out, dest], dur, ctx.progress, err_path=err)
    elif target == "gif":
        _need(info, "video")
        facts["trimmed"] = dur > GIF_MAX_SECONDS
        flt = (f"fps=10,scale='min({GIF_WIDTH},iw)':-2:flags=lanczos,split[a][b];"
               "[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4")
        run_ffmpeg(base + ["-t", str(GIF_MAX_SECONDS), "-an", "-filter_complex", flt, "-f", "gif", dest],
                   min(dur, GIF_MAX_SECONDS), ctx.progress, err_path=err)
    else:
        _need(info, "video")
        fmt_out = {"mp4": "mp4", "mov": "mov", "webm": "webm"}[target]
        copy_ok = (target in ("mp4", "mov") and vcodec in ("h264", "hevc")
                   and acodec in (None, "aac", "mp3", "alac"))
        if copy_ok and quality != "small":
            _cap(dur, REMUX_MAX_SECONDS, "Videos")
            tag = ["-tag:v", "hvc1"] if vcodec == "hevc" else []
            args = ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", *tag, "-movflags", "+faststart",
                    "-map_metadata", "-1"]
            facts["copied"] = True
        else:
            _cap(dur, VIDEO_MAX_SECONDS, "Videos")
            args = ["-map", "0:v:0", "-map", "0:a:0?"] + _video_args(target, quality, a is not None)
        run_ffmpeg(base + args + ["-f", fmt_out, dest], dur, ctx.progress, err_path=err)
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise ToolkitError("This video or audio file could not be converted. It may be damaged.")
    facts["in_bytes"] = os.path.getsize(src)
    facts["out_bytes"] = os.path.getsize(dest)
    return facts


_COMPRESS = {  # level: (max width, max height, share of the source bitrate, cap kbps, audio kbps)
    "light": (1920, 1080, 0.7, 4000, 128),
    "medium": (1280, 720, 0.45, 1800, 96),
    "strong": (854, 480, 0.25, 700, 64),
}


def media_compress(ctx, inputs, opts, out):
    """Makes a video (as MP4) or a recording (as M4A) smaller. Never returns a bigger file:
    when the result would not be smaller, the source comes back as it was (MP4 or M4A sources),
    and already_optimised says so."""
    src = inputs[0]
    fmt = media_kind(src) or "mov"
    level = opts.get("level") or "medium"
    info = probe(src, fmt)
    dur = info["duration"]
    in_bytes = os.path.getsize(src)
    err = os.path.join(ctx.work, "ffmpeg.err")
    w, h, share, cap, akbps = _COMPRESS[level]
    if info["video"]:
        _cap(dur, VIDEO_MAX_SECONDS, "Videos")
        src_kbps = (in_bytes * 8 / 1000 / dur) if dur else cap
        kbps = int(max(150, min(cap, (src_kbps - akbps) * share)))
        dest = ctx.output("compressed.mp4")
        args = ["-f", fmt, "-i", src, "-map", "0:v:0", "-map", "0:a:0?"] + \
            _video_args("mp4", "balanced", info["audio"] is not None, w, h, kbps)
        args = [x if x != f"{_AUDIO_KBPS['balanced']}k" else f"{akbps}k" for x in args]
        run_ffmpeg(args + ["-f", "mp4", dest], dur, ctx.progress, err_path=err)
        same_kind = os.path.splitext(src)[1].lower() in (".mp4", ".m4v")
    elif info["audio"]:
        _cap(dur, AUDIO_MAX_SECONDS, "Recordings")
        dest = ctx.output("compressed.m4a")
        run_ffmpeg(["-f", fmt, "-i", src, "-vn", "-c:a", "aac", "-b:a", f"{akbps}k",
                    "-map_metadata", "-1", "-f", "ipod", dest], dur, ctx.progress, err_path=err)
        same_kind = os.path.splitext(src)[1].lower() == ".m4a"
    else:
        raise ToolkitError("This file has no picture or sound to compress.")
    out_bytes = os.path.getsize(dest) if os.path.exists(dest) else 0
    if not out_bytes:
        raise ToolkitError("This video or audio file could not be compressed. It may be damaged.")
    already = False
    if out_bytes >= in_bytes:
        if not same_kind:
            raise ToolkitError("This file is already small. Compressing it would not make it any "
                               "smaller, so your original is the best copy.")
        shutil.copyfile(src, dest)
        out_bytes, already = in_bytes, True
    return {"level": level, "in_bytes": in_bytes, "out_bytes": out_bytes,
            "already_optimised": already, "seconds": int(round(dur))}


def photo_convert(ctx, inputs, opts, out):
    """HEIC or HEIF photo to JPG or PNG (the browsers outside Safari cannot open HEIC)."""
    src = inputs[0]
    to = opts.get("to") or "jpg"
    info = probe(src, "mov")
    v = info["video"]
    if not v:
        raise ToolkitError("This HEIC photo can't be opened. It may be damaged.")
    try:
        px = int(v.get("width") or 0) * int(v.get("height") or 0)
    except (TypeError, ValueError):
        px = 0
    if px > PHOTO_MAX_PIXELS:
        raise ToolkitError("Photos up to 60 megapixels can be converted here.")
    dest = ctx.output(f"photo.{to}")
    enc = ["-c:v", "mjpeg", "-q:v", "3", "-pix_fmt", "yuvj420p"] if to == "jpg" else ["-c:v", "png"]
    run_ffmpeg(["-f", "mov", "-i", src, "-frames:v", "1", "-map_metadata", "-1", *enc,
                "-f", "image2", dest], 1, ctx.progress, timeout=PHOTO_TIMEOUT,
               err_path=os.path.join(ctx.work, "ffmpeg.err"))
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise ToolkitError("This HEIC photo can't be opened. It may be damaged.")
    return {"pages": 1}

# ------------------------------------------------------------------ e-books


def _img_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class _Images:
    def __init__(self):
        self.count = 0
        self.total = 0

    def data_uri(self, data: bytes | None) -> str | None:
        if not data or len(data) > EBOOK_MAX_IMAGE_BYTES:
            return None
        kind = _img_mime(data)
        if not kind or self.count >= EBOOK_MAX_IMAGES or self.total + len(data) > EBOOK_MAX_ALL_IMAGES:
            return None
        self.count += 1
        self.total += len(data)
        return f"data:image/{kind};base64," + base64.b64encode(data).decode("ascii")


_BODY_RE = re.compile(r"(?is)<body[^>]*>(.*)</body>")
_STYLE_RE = re.compile(r"(?is)<style[^>]*>.*?</style>")
_HEAD_RE = re.compile(r"(?is)<head[^>]*>.*?</head>")
_IMG_SRC_RE = re.compile(r"""(?is)(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""")
_SVG_IMAGE_RE = re.compile(r"""(?is)<svg\b.*?<image\b[^>]*?(?:xlink:)?href\s*=\s*(["'])(.*?)\1.*?</svg>""")
_XML_DECL_RE = re.compile(r"(?is)<\?xml.*?\?>|<!DOCTYPE[^>]*>")


def _zip_guard(z: zipfile.ZipFile) -> None:
    infos = z.infolist()
    if len(infos) > EBOOK_ZIP_MAX_ENTRIES:
        raise ToolkitError("This e-book has too many parts to convert.")
    if sum(i.file_size for i in infos) > EBOOK_ZIP_MAX_UNCOMPRESSED:
        raise ToolkitError("This e-book is too big to convert here.")


def _read(z: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        return None
    with z.open(info) as fh:
        return fh.read(limit + 1)[:limit]


def _attr(tag: str, name: str) -> str | None:
    m = re.search(r"""(?is)\b%s\s*=\s*(["'])(.*?)\1""" % re.escape(name), tag)
    return m.group(2) if m else None


def epub_html(path: str) -> tuple[str, str, int]:
    """Returns (title, html body, chapters) of an EPUB."""
    from html import unescape
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ToolkitError("This EPUB can't be opened. It may be damaged.")
    with z:
        _zip_guard(z)
        enc = _read(z, "META-INF/encryption.xml", 1 * MB)
        if enc and re.search(rb"EncryptedData", enc) and not re.search(
                rb"http://www\.idpf\.org/2008/embedding|http://ns\.adobe\.com/pdf/enc#RC", enc):
            raise ToolkitError("This e-book is protected (DRM), so it can't be converted.")
        cont = _read(z, "META-INF/container.xml", 1 * MB) or b""
        m = re.search(rb"""full-path\s*=\s*["']([^"']+)["']""", cont)
        if not m:
            raise ToolkitError("This EPUB can't be opened. It may be damaged.")
        opf_path = unescape(m.group(1).decode("utf-8", "replace"))
        opf = (_read(z, opf_path, 4 * MB) or b"").decode("utf-8", "replace")
        if not opf:
            raise ToolkitError("This EPUB can't be opened. It may be damaged.")
        root = posixpath.dirname(opf_path)
        tm = re.search(r"(?is)<dc:title[^>]*>(.*?)</dc:title>", opf)
        title = unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip() if tm else ""
        items = {}
        for tag in re.findall(r"(?is)<item\b[^>]*>", opf):
            iid, href = _attr(tag, "id"), _attr(tag, "href")
            if iid and href:
                items[iid] = (posixpath.normpath(posixpath.join(root, unescape(href).split("#")[0])),
                              (_attr(tag, "media-type") or "").lower())
        spine = [_attr(t, "idref") for t in re.findall(r"(?is)<itemref\b[^>]*>", opf)]
        css = []
        for p, mt in items.values():
            if mt == "text/css":
                c = _read(z, p, 256 * 1024)
                if c:
                    css.append(c.decode("utf-8", "replace"))
        imgs = _Images()
        parts, size = [], 0
        chapters = [items[i] for i in spine if i in items][:EBOOK_MAX_CHAPTERS]
        if not chapters:
            raise ToolkitError("This EPUB has no chapters to convert.")
        for p, mt in chapters:
            if mt not in ("application/xhtml+xml", "text/html", "application/xml", ""):
                continue
            raw = _read(z, p, 8 * MB)
            if raw is None:
                continue
            text = raw.decode("utf-8", "replace")
            text = _XML_DECL_RE.sub("", text)
            bm = _BODY_RE.search(text)
            body = bm.group(1) if bm else _HEAD_RE.sub("", text)
            base = posixpath.dirname(p)

            def _svg(m, base=base):
                ref = posixpath.normpath(posixpath.join(base, unescape(m.group(2))))
                uri = imgs.data_uri(_read(z, ref, EBOOK_MAX_IMAGE_BYTES))
                return f'<img src="{uri}" alt="">' if uri else ""
            body = _SVG_IMAGE_RE.sub(_svg, body)

            def _img(m, base=base):
                src = unescape(m.group(3))
                if src.startswith("data:"):
                    return m.group(0)
                ref = posixpath.normpath(posixpath.join(base, src.split("#")[0]))
                uri = imgs.data_uri(_read(z, ref, EBOOK_MAX_IMAGE_BYTES))
                return f'{m.group(1)}"{uri or ""}"'
            body = _IMG_SRC_RE.sub(_img, body)
            size += len(body)
            if size > EBOOK_MAX_HTML:
                raise ToolkitError("This e-book is too long to convert here.")
            parts.append(f'<div style="page-break-before:always">{body}</div>' if parts else f"<div>{body}</div>")
    style = "<style>" + "\n".join(css)[:256 * 1024] + "</style>" if css else ""
    return title, style + "".join(parts), len(parts)

# MOBI (MOBI 6 text, the part every .mobi, .azw and .prc carries; PalmDOC compression).


def palmdoc_decompress(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        i += 1
        if c == 0 or 0x09 <= c <= 0x7F:
            out.append(c)
        elif c <= 0x08:
            out += data[i:i + c]
            i += c
        elif c >= 0xC0:
            out.append(0x20)
            out.append(c ^ 0x80)
        else:
            if i >= n:
                break
            pair = (c << 8) | data[i]
            i += 1
            dist = (pair >> 3) & 0x07FF
            length = (pair & 7) + 3
            if dist == 0 or dist > len(out):
                raise ToolkitError("This MOBI e-book can't be opened. It may be damaged.")
            for _ in range(length):
                out.append(out[-dist])
    return bytes(out)


def _trailing_entry_size(data: bytes) -> int:
    num = 0
    for v in data[-4:]:
        if v & 0x80:
            num = 0
        num = (num << 7) | (v & 0x7F)
    return num


def _trailing_size(data: bytes, flags: int) -> int:
    size = 0
    for bit in range(15, 0, -1):
        if flags & (1 << bit):
            if len(data) - size <= 0:
                break
            size += _trailing_entry_size(data[:len(data) - size])
    if flags & 1 and len(data) - size > 0:
        size += (data[len(data) - size - 1] & 0x3) + 1
    return size


def mobi_html(path: str) -> tuple[str, str, int]:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 86:
        raise ToolkitError("This MOBI e-book can't be opened. It may be damaged.")
    n = struct.unpack(">H", data[76:78])[0]
    offs = [struct.unpack(">I", data[78 + 8 * i:82 + 8 * i])[0] for i in range(n)]
    if not offs or any(o > len(data) for o in offs):
        raise ToolkitError("This MOBI e-book can't be opened. It may be damaged.")
    offs.append(len(data))
    rec = lambda i: data[offs[i]:offs[i + 1]]  # noqa: E731
    r0 = rec(0)
    comp = struct.unpack(">H", r0[0:2])[0]
    nrec = struct.unpack(">H", r0[8:10])[0]
    crypt = struct.unpack(">H", r0[12:14])[0]
    if crypt:
        raise ToolkitError("This e-book is protected (DRM), so it can't be converted.")
    if comp not in (1, 2):
        raise ToolkitError("This MOBI e-book uses a kind of compression Multies can't read yet. "
                           "Try the EPUB version of the book.")
    encoding, flags, first_img, title = "cp1252", 0, None, ""
    if r0[16:20] == b"MOBI":
        hlen = struct.unpack(">I", r0[20:24])[0]
        enc = struct.unpack(">I", r0[28:32])[0]
        encoding = "utf-8" if enc == 65001 else "cp1252"
        if len(r0) >= 112:
            first_img = struct.unpack(">I", r0[108:112])[0]
        if len(r0) >= 92:
            to, tl = struct.unpack(">II", r0[84:92])
            if 0 < tl < 1024 and to + tl <= len(r0):
                title = r0[to:to + tl].decode(encoding, "replace")
        if hlen >= 0xE4 and len(r0) >= 0xF4:
            flags = struct.unpack(">H", r0[0xF2:0xF4])[0]
    chunks, size = [], 0
    for i in range(1, min(nrec, n - 1) + 1):
        raw = rec(i)
        cut = _trailing_size(raw, flags) if flags else 0
        raw = raw[:len(raw) - cut] if cut else raw
        piece = palmdoc_decompress(raw) if comp == 2 else raw
        size += len(piece)
        if size > EBOOK_MAX_HTML * 2:
            raise ToolkitError("This e-book is too long to convert here.")
        chunks.append(piece)
    text = b"".join(chunks).decode(encoding, "replace")
    imgs = _Images()
    cache: dict[int, str | None] = {}

    def _img(m):
        idx = int(m.group(1))
        if idx not in cache:
            j = (first_img or 0) + idx - 1
            cache[idx] = imgs.data_uri(rec(j)) if first_img is not None and 0 < j < n else None
        return f'<img src="{cache[idx]}" alt="">' if cache[idx] else ""
    text = re.sub(r"""(?is)<img\b[^>]*?recindex\s*=\s*["']?(\d+)["']?[^>]*>""", _img, text)
    text = re.sub(r"(?is)<mbp:pagebreak\s*/?>", '<div style="page-break-before:always"></div>', text)
    bm = _BODY_RE.search(text)
    body = bm.group(1) if bm else text
    if len(body) > EBOOK_MAX_HTML:
        raise ToolkitError("This e-book is too long to convert here.")
    return title, body, max(1, body.lower().count("page-break-before"))


def ebook_to_pdf(ctx, inputs, opts, out):
    from office_toolkit import ops_p2b as B
    src = inputs[0]
    if os.path.getsize(src) > EBOOK_MAX_BYTES:
        raise ToolkitError("E-books up to 30 MB can be converted here.")
    ext = os.path.splitext(src)[1].lower()
    title, body, chapters = epub_html(src) if ext == ".epub" else mobi_html(src)
    import html as _h
    head = f"<title>{_h.escape(title)}</title>" if title else ""
    doc, blocked = B.sanitise_html(
        f"<!DOCTYPE html><html><head><meta charset=\"utf-8\">{head}{B._BASE_CSS}</head><body>{body}</body></html>")
    if len(re.sub(r"<[^>]+>", "", doc).strip()) < 1 and "<img" not in doc:
        raise ToolkitError("This e-book has no text to convert.")
    ctx.progress(0, 1)
    src_html = os.path.join(ctx.work, "book.html")
    with open(src_html, "w", encoding="utf-8") as fh:
        fh.write(doc)
    made = B.soffice_convert([src_html], os.path.join(ctx.work, "pdf"), timeout=EBOOK_SOFFICE_TIMEOUT,
                             infilter="HTML (StarWriter)")[0]
    shutil.move(made, out)
    ctx.progress(1, 1)
    return {"pages": B._pdf_pages(out), "chapters": chapters}

# ------------------------------------------------------------------ listing

_Q = Option("quality", "choice", "Quality", default="balanced", choices=("high", "balanced", "small"))
OPERATIONS = {
    "media-convert": Operation(
        "media-convert", "Convert video or audio", "file", 1, 1, "converted.mp4", "video/mp4",
        (Option("to", "choice", "Convert to", default="mp4", choices=MEDIA_TARGETS), _Q),
        max_bytes=MEDIA_MAX_BYTES, slow=True, exts=MEDIA_EXTS,
        outputs=tuple(f"converted.{t}" for t in MEDIA_TARGETS if t != "mp4"),
        limits=(("video_max_seconds", VIDEO_MAX_SECONDS), ("audio_max_seconds", AUDIO_MAX_SECONDS),
                ("gif_max_seconds", GIF_MAX_SECONDS), ("max_width", VIDEO_MAX_W),
                ("max_height", VIDEO_MAX_H))),
    "media-compress": Operation(
        "media-compress", "Compress video or audio", "file", 1, 1, "compressed.mp4", "video/mp4",
        (Option("level", "choice", "Level", default="medium", choices=("light", "medium", "strong")),),
        max_bytes=MEDIA_MAX_BYTES, slow=True, exts=MEDIA_EXTS, outputs=("compressed.m4a",),
        limits=(("video_max_seconds", VIDEO_MAX_SECONDS), ("audio_max_seconds", AUDIO_MAX_SECONDS))),
    "photo-convert": Operation(
        "photo-convert", "HEIC photo to JPG or PNG", "file", 1, 1, "photo.jpg", "image/jpeg",
        (Option("to", "choice", "Convert to", default="jpg", choices=("jpg", "png")),),
        max_bytes=50 * MB, exts=PHOTO_EXTS, outputs=("photo.png",),
        limits=(("max_pixels", PHOTO_MAX_PIXELS),)),
    "ebook-to-pdf": Operation(
        "ebook-to-pdf", "E-book to PDF", "file", 1, 1, "book.pdf", "application/pdf", (),
        max_bytes=EBOOK_MAX_BYTES, slow=True, exts=EBOOK_EXTS,
        limits=(("max_chapters", EBOOK_MAX_CHAPTERS), ("max_images", EBOOK_MAX_IMAGES),
                ("max_text_bytes", EBOOK_MAX_HTML))),
}
HANDLERS = {"media-convert": media_convert, "media-compress": media_compress,
            "photo-convert": photo_convert, "ebook-to-pdf": ebook_to_pdf}
