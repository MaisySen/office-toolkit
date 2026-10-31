"""R86 ops: video and audio (FFmpeg, LGPL build), HEIC photos, e-books."""
import io
import json
import os
import shutil
import struct
import zipfile
from pathlib import Path

import pytest

from office_toolkit import ops_r86 as R
from office_toolkit.opspec import Ctx
from office_toolkit.pdfops import ToolkitError
from office_toolkit.web import engine
from office_toolkit.web.security import sniff_ext
from tests.test_engine import eng, get, signed, submit  # noqa: F401

FX = Path(__file__).parent / "fixtures" / "r86"
HAS_FFMPEG = bool(os.environ.get("TOOLKIT_FFMPEG") or shutil.which("ffmpeg")) and \
    bool(os.environ.get("TOOLKIT_FFPROBE") or shutil.which("ffprobe"))
HAS_SOFFICE = bool(shutil.which("soffice"))
needs_ff = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
needs_lo = pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")


def _ctx(tmp_path):
    w, o = tmp_path / "work", tmp_path / "out"
    w.mkdir()
    o.mkdir()
    return Ctx(work=str(w), out_dir=str(o))


def _run(fn, tmp_path, src, **opts):
    ctx = _ctx(tmp_path)
    facts = fn(ctx, [str(src)], dict(opts), os.path.join(ctx.out_dir, "unused"))
    return ctx, facts, Path(ctx.out_dir) / (ctx.output_name or "unused")


# ------------------------------------------------------------------ listing

def test_ops_listed_with_caps(eng):
    r = get(eng, "/internal/v1/operations")
    ops = {o["slug"]: o for o in r.get_json()["operations"]}
    for slug in ("media-convert", "media-compress", "photo-convert", "ebook-to-pdf"):
        assert slug in ops
    mc = ops["media-convert"]
    assert ".webm" in mc["accepts_ext"] and ".mp3" in mc["accepts_ext"]
    assert {o["name"] for o in mc["outputs"]} >= {"converted.mp4", "converted.gif", "converted.wav"}
    assert mc["limits"]["video_max_seconds"] == R.VIDEO_MAX_SECONDS
    assert ops["ebook-to-pdf"]["accepts_ext"] == [".epub", ".mobi", ".azw", ".prc"]
    # the P1 ops are untouched
    assert ops["compress"]["accepts"] == "pdf" and "accepts_ext" not in ops["compress"]


def test_new_output_types():
    from office_toolkit.opspec import content_type_for
    assert content_type_for("x.mp4") == "video/mp4"
    assert content_type_for("x.m4a") == "audio/mp4"
    assert content_type_for("x.gif") == "image/gif"
    assert content_type_for("x.pdf") == "application/pdf"


# ------------------------------------------------------------------ sniffing

@pytest.mark.parametrize("name,ext", [("clip.mp4", ".mp4"), ("clip.mov", ".mov"), ("clip.webm", ".webm"),
                                      ("tone.mp3", ".mp3"), ("tone.wav", ".wav"), ("photo.heic", ".heic")])
def test_sniff_accepts_real_files(name, ext):
    sniff_ext(str(FX / name), ext)


@pytest.mark.parametrize("ext", [".mp4", ".webm", ".mp3", ".heic", ".mobi", ".epub"])
def test_sniff_refuses_disguised_files(tmp_path, ext):
    p = tmp_path / f"x{ext}"
    # a playlist that would make a demuxer fetch other files
    p.write_bytes(b"#EXTM3U\n#EXT-X-TARGETDURATION:10\nhttp://169.254.169.254/latest\n" + b" " * 100)
    with pytest.raises(ToolkitError):
        sniff_ext(str(p), ext)


def test_wrong_extension_is_refused_by_the_api(eng, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    r = submit(eng, "media-convert", [str(p)], {"to": "mp3"})
    assert r.status_code == 400
    assert "files only" in r.get_json()["message"]


# ------------------------------------------------------------------ media

@needs_ff
@pytest.mark.parametrize("to", ["mp4", "mov", "webm", "mp3", "wav", "m4a", "gif"])
def test_webm_converts_to_every_target(tmp_path, to):
    ctx, facts, out = _run(R.media_convert, tmp_path, FX / "clip.webm", to=to, quality="balanced")
    assert out.name == f"converted.{to}" and out.stat().st_size > 1000
    assert facts["seconds"] == 3
    head = out.read_bytes()[:12]
    if to in ("mp4", "mov", "m4a"):
        assert head[4:8] == b"ftyp"
    if to == "gif":
        assert head.startswith(b"GIF8")
    if to == "wav":
        assert head[8:12] == b"WAVE"


@needs_ff
def test_mp4_to_mov_copies_streams(tmp_path):
    _, facts, out = _run(R.media_convert, tmp_path, FX / "clip.mp4", to="mov")
    assert facts.get("copied") is True and out.stat().st_size > 1000


@needs_ff
def test_audio_only_file_cannot_become_video(tmp_path):
    with pytest.raises(ToolkitError, match="no video picture"):
        _run(R.media_convert, tmp_path, FX / "tone.wav", to="mp4")


@needs_ff
def test_long_video_is_refused_with_a_way_forward(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "VIDEO_MAX_SECONDS", 1)
    with pytest.raises(ToolkitError, match="Trim it first"):
        _run(R.media_convert, tmp_path, FX / "clip.webm", to="mp4")


@needs_ff
def test_progress_is_reported(tmp_path):
    seen = []
    ctx = _ctx(tmp_path)
    ctx.progress = lambda d, t: seen.append((d, t))
    R.media_convert(ctx, [str(FX / "clip.webm")], {"to": "webm", "quality": "small"}, "")
    assert seen and seen[-1][1] == 3


@needs_ff
def test_compress_video_smaller_or_original(tmp_path):
    _, facts, out = _run(R.media_compress, tmp_path, FX / "clip.webm", level="strong")
    assert out.name == "compressed.mp4" and facts["out_bytes"] < facts["in_bytes"]
    _, facts2, out2 = _run(R.media_compress, tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path,
                           FX / "clip.mp4", level="medium")
    assert facts2["out_bytes"] <= facts2["in_bytes"]
    if facts2["already_optimised"]:
        assert out2.read_bytes() == (FX / "clip.mp4").read_bytes()


@needs_ff
def test_compress_recording_to_m4a(tmp_path):
    _, facts, out = _run(R.media_compress, tmp_path, FX / "tone.wav", level="medium")
    assert out.name == "compressed.m4a" and facts["out_bytes"] < facts["in_bytes"]


@needs_ff
def test_heic_to_jpg_and_png(tmp_path):
    _, _, out = _run(R.photo_convert, tmp_path, FX / "photo.heic", to="jpg")
    assert out.read_bytes()[:3] == b"\xff\xd8\xff"
    (tmp_path / "p").mkdir()
    _, _, out = _run(R.photo_convert, tmp_path / "p", FX / "photo.heic", to="png")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@needs_ff
def test_media_job_through_the_api(eng):
    r = submit(eng, "media-convert", [str(FX / "clip.webm")], {"to": "mp3"})
    assert r.status_code == 202, r.get_json()
    jid = r.get_json()["id"]
    j = get(eng, f"/internal/v1/jobs/{jid}").get_json()
    assert j["status"] == "done", j
    assert j["output"]["name"] == "converted.mp3" and j["output"]["content_type"] == "audio/mpeg"


# ------------------------------------------------------------------ e-books

def _epub(path, chapters=2, drm=False):
    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c63f8cfc0f01f0005000201f3a3c5b10000000049454e44ae426082")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        if drm:
            z.writestr("META-INF/encryption.xml", "<encryption><EncryptedData/></encryption>")
        items = "".join(f'<item id="c{i}" href="text/c{i}.xhtml" media-type="application/xhtml+xml"/>'
                        for i in range(chapters))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(chapters))
        z.writestr("OEBPS/book.opf",
                   f'<package><metadata><dc:title>Test Book</dc:title></metadata><manifest>{items}'
                   f'<item id="i" href="img/dot.png" media-type="image/png"/></manifest><spine>{spine}</spine></package>')
        for i in range(chapters):
            z.writestr(f"OEBPS/text/c{i}.xhtml",
                       f'<?xml version="1.0"?><html><head><script>alert(1)</script></head><body>'
                       f'<h1>Chapter {i + 1}</h1><p>Words of chapter {i + 1}.</p>'
                       f'<img src="../img/dot.png"/><img src="http://169.254.169.254/x.png"/></body></html>')
        z.writestr("OEBPS/img/dot.png", png)
    return path


def test_epub_reader_builds_clean_html(tmp_path):
    title, body, n = R.epub_html(str(_epub(tmp_path / "b.epub", 3)))
    assert title == "Test Book" and n == 3
    assert "Chapter 3" in body and "data:image/png;base64," in body
    assert "169.254" not in body


def test_epub_with_drm_is_refused(tmp_path):
    with pytest.raises(ToolkitError, match="DRM"):
        R.epub_html(str(_epub(tmp_path / "d.epub", drm=True)))


def test_epub_zip_bomb_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "EBOOK_ZIP_MAX_UNCOMPRESSED", 100)
    with pytest.raises(ToolkitError, match="too big"):
        R.epub_html(str(_epub(tmp_path / "b.epub")))


def _palm_literal(data: bytes) -> bytes:
    """A valid PalmDOC stream with no back references (literals and 1 to 8 byte runs)."""
    out = bytearray()
    for b in data:
        if b == 0 or 0x09 <= b <= 0x7F:
            out.append(b)
        else:
            out += bytes([1, b])
    return bytes(out)


def _mobi(path, text: bytes, comp=2, crypt=0):
    recs = [text[i:i + 4096] for i in range(0, len(text), 4096)]
    body = [_palm_literal(r) if comp == 2 else r for r in recs]
    r0 = struct.pack(">HHIHHH", comp, 0, len(text), len(recs), 4096, crypt) + b"\0\0"
    mobi = bytearray(b"MOBI" + struct.pack(">I", 0xE8) + struct.pack(">II", 2, 65001) + b"\0" * (0xE8 - 16))
    r0 += bytes(mobi)
    records = [r0] + body
    n = len(records)
    head = bytearray(b"Test".ljust(32, b"\0") + b"\0" * 28 + b"BOOKMOBI" + b"\0" * 8)
    head += struct.pack(">H", n)
    off = 78 + 8 * n + 2
    table = b""
    for i, r in enumerate(records):
        table += struct.pack(">II", off, i)
        off += len(r)
    Path(path).write_bytes(bytes(head) + table + b"\0\0" + b"".join(records))
    return path


def test_palmdoc_back_reference():
    # "abcabc": 3 literals, then a copy of distance 3, length 3
    pair = 0x8000 | (3 << 3) | (3 - 3)
    assert R.palmdoc_decompress(b"abc" + struct.pack(">H", pair)) == b"abcabc"
    assert R.palmdoc_decompress(b"a\xe2") == b"a b"


def test_mobi_reader(tmp_path):
    html = "<html><body><p>Ünïcode words</p><mbp:pagebreak/><p>Next</p></body></html>".encode()
    _, body, n = R.mobi_html(str(_mobi(tmp_path / "b.mobi", html)))
    assert "Ünïcode words" in body and "page-break-before" in body


def test_mobi_with_drm_is_refused(tmp_path):
    with pytest.raises(ToolkitError, match="DRM"):
        R.mobi_html(str(_mobi(tmp_path / "b.mobi", b"<p>x</p>", crypt=2)))


def test_mobi_huffman_is_refused_plainly(tmp_path):
    with pytest.raises(ToolkitError, match="EPUB version"):
        R.mobi_html(str(_mobi(tmp_path / "b.mobi", b"<p>x</p>", comp=17480)))


@needs_lo
def test_epub_and_mobi_to_pdf(tmp_path):
    for name, src in (("e", _epub(tmp_path / "b.epub", 2)),
                      ("m", _mobi(tmp_path / "b.mobi", b"<html><body><p>Hello book</p></body></html>"))):
        d = tmp_path / name
        d.mkdir()
        _, facts, _ = _run(R.ebook_to_pdf, d, src)
        out = Path(d / "out" / "unused")
        assert out.read_bytes()[:5] == b"%PDF-" and facts["pages"] >= 1
