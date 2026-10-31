"""Engine only mode: signed internal job API for a trusted caller.

When TOOLKIT_ENGINE_ONLY=1 the engine answers only /healthz and
/internal/v1/*; the web UI, local accounts, OIDC and /api/run/* are 404.
Jobs run on a small thread pool. Inputs are deleted when the operation
ends; outputs are deleted OUTPUT_TTL_SECONDS after they are made.
"""
from __future__ import annotations

import json
import os
import re
import zipfile
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import importlib
from dataclasses import dataclass, field

from flask import Blueprint, abort, jsonify, request, send_file

from office_toolkit import pdfops
from office_toolkit.pdfops import ToolkitError
from office_toolkit.web import service_auth
from office_toolkit.web.security import page_count_guard, sniff, sniff_ext

OUTPUT_TTL_SECONDS = 60 * 60
POOL_SIZE = 2
QUEUE_CAP = 20
PER_WORKSPACE = 2
MAX_FILES = 20

# Clock seam for tests.
now = time.time


from office_toolkit.opspec import (DOCX, PDF, ZIP, Ctx, Operation, Option,  # noqa: F401
                                    content_type_for)

OCR_MAX_PAGES = 30
OCR_MAX_BYTES = 20 * 1024 * 1024
# R68: OCR in the non Latin scripts reads fewer pages per run (each page is
# about 25 s on the free host, so 10 pages stay near four minutes). The P1
# listing is unchanged; the cap is checked when the job is created.
HEAVY_OCR_MAX_PAGES = 10
HEAVY_OCR_LANGUAGES = {"nep": "Nepali", "hin": "Hindi", "eng+nep": "Nepali", "eng+hin": "Hindi",
                       "chi_sim": "Chinese", "chi_tra": "Chinese", "jpn": "Japanese",
                       "kor": "Korean", "ara": "Arabic", "tha": "Thai"}
JPEG_MAX_PAGES = 100
MAX_IMAGES = 50
# P2 widened the OCR languages; "eng" stays first and the default.
OCR_LANGUAGES = ("eng", "nep", "hin", "chi_sim", "chi_tra", "ara", "vie", "ind", "spa", "fra",
                 "deu", "jpn", "kor", "tha", "fil", "eng+nep", "eng+hin")


_RANGES = Option("ranges", "ranges", "Pages", max_len=200)
OPERATIONS = {
    # R68b: optional level; callers that send none get "medium", which now
    # shrinks pictures (scans) as well as the file structure.
    "compress": Operation("compress", "Compress PDF", "pdf", 1, 1, "compressed.pdf", PDF,
                          (Option("level", "choice", "Level", default="medium",
                                  choices=("light", "medium", "strong")),)),
    "merge": Operation("merge", "Merge PDFs", "pdf", 2, MAX_FILES, "merged.pdf"),
    "convert": Operation("convert", "Convert to PDF", "office", 1, 1, "converted.pdf"),
    "to-jpeg": Operation("to-jpeg", "PDF to images", "pdf", 1, 1, "pages.zip", ZIP,
                         (Option("dpi", "choice", "Quality", default="150",
                                 choices=("72", "150", "300")),),
                         max_pages=JPEG_MAX_PAGES, slow=True),
    "images-to-pdf": Operation("images-to-pdf", "Images to PDF", "image", 1, MAX_IMAGES,
                               "images.pdf"),
    "split": Operation("split", "Split PDF", "pdf", 1, 1, "split.zip", ZIP,
                       (Option("ranges", "ranges", "Pages for each part", required=True),)),
    "rotate": Operation("rotate", "Rotate pages", "pdf", 1, 1, "rotated.pdf", PDF,
                        (Option("angle", "choice", "Turn", default="90",
                                choices=("90", "180", "270")), _RANGES)),
    "scrub": Operation("scrub", "Remove metadata", "pdf", 1, 1, "cleaned.pdf"),
    "ocr": Operation("ocr", "Make searchable (OCR)", "pdf", 1, 1, "searchable.pdf", PDF,
                     (Option("language", "choice", "Language", default="eng",
                             choices=OCR_LANGUAGES),),
                     max_pages=OCR_MAX_PAGES, max_bytes=OCR_MAX_BYTES, slow=True),
    "redact": Operation("redact", "Redact text", "pdf", 1, 1, "redacted.pdf", PDF,
                        (Option("phrases", "phrases", "Words or phrases to black out",
                                required=True, max_len=2000),)),
    "watermark": Operation("watermark", "Watermark", "pdf", 1, 1, "watermarked.pdf", PDF,
                           (Option("text", "text", "Watermark text", required=True,
                                   max_len=80),
                            Option("opacity", "int", "Strength (percent)", default=25,
                                   min=10, max=60))),
    "unlock": Operation("unlock", "Remove password", "pdf", 1, 1, "unlocked.pdf", PDF,
                        (Option("password", "password", "Current password", required=True,
                                max_len=128),)),
    "pdf-to-docx": Operation("pdf-to-docx", "PDF to Word", "pdf", 1, 1, "document.docx",
                             DOCX),
}
_RANGES_RE = re.compile(r"^\s*\d{1,4}(\s*-\s*\d{1,4})?(\s*,\s*\d{1,4}(\s*-\s*\d{1,4})?){0,99}\s*$")
_IMAGE_EXT = (".jpg", ".jpeg", ".png")

# P2 op modules: each exposes OPERATIONS and HANDLERS (see opspec.py). A
# module not in this checkout is skipped; a slug may never replace a P1 op.
P2_MODULES = ("office_toolkit.ops_p2a", "office_toolkit.ops_p2b", "office_toolkit.ops_r86")
P2_HANDLERS: dict = {}


def _load_p2(modname: str) -> None:
    try:
        mod = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name == modname:
            return
        raise
    ops = getattr(mod, "OPERATIONS", {})
    clash = set(ops) & set(OPERATIONS)
    if clash:
        raise RuntimeError(f"{modname} redefines {sorted(clash)}")
    OPERATIONS.update(ops)
    P2_HANDLERS.update(getattr(mod, "HANDLERS", {}))


for _m in P2_MODULES:
    _load_p2(_m)


def parse_options(op: Operation, form) -> dict:
    """Checks the tool's settings; raises ToolkitError with a plain message."""
    out: dict = {}
    for o in op.options:
        raw = form.get(o.name)
        raw = raw if raw is not None and str(raw).strip() != "" else None
        if raw is None:
            if o.required:
                raise ToolkitError(f"{o.label} is required.")
            out[o.name] = o.default
            continue
        raw = str(raw)
        if o.kind in ("text", "password", "ranges", "phrases", "json") and len(raw) > o.max_len:
            raise ToolkitError(f"{o.label} is too long.")
        if o.kind == "choice":
            if raw not in o.choices:
                raise ToolkitError(f"{o.label} must be one of {', '.join(o.choices)}.")
            out[o.name] = raw
        elif o.kind == "int":
            try:
                v = int(raw)
            except ValueError:
                raise ToolkitError(f"{o.label} must be a whole number.")
            if (o.min is not None and v < o.min) or (o.max is not None and v > o.max):
                raise ToolkitError(f"{o.label} must be between {o.min} and {o.max}.")
            out[o.name] = v
        elif o.kind == "ranges":
            if not _RANGES_RE.match(raw):
                raise ToolkitError(f"{o.label}: use page numbers and ranges like 1-3, 5.")
            out[o.name] = raw.replace(" ", "")
        elif o.kind == "phrases":
            items = [p.strip() for p in raw.splitlines() if p.strip()]
            if not items:
                raise ToolkitError(f"{o.label} is required.")
            if len(items) > 20:
                raise ToolkitError("Up to 20 words or phrases at a time.")
            out[o.name] = items
        elif o.kind == "password":
            out[o.name] = raw
        elif o.kind == "json":
            try:
                out[o.name] = json.loads(raw)
            except (ValueError, RecursionError):
                raise ToolkitError(f"{o.label} is not valid.")
        else:
            out[o.name] = raw.strip()
    return out


def _count_pages(path: str) -> int:
    from pypdf import PdfReader
    try:
        r = PdfReader(path)
        return 0 if r.is_encrypted else len(r.pages)
    except Exception:
        raise ToolkitError("The PDF could not be parsed (corrupt or unsupported).")


@dataclass
class Job:
    id: str
    op: str
    workspace: str
    user: str
    dir: str
    status: str = "queued"     # queued, running, done, failed
    error: str | None = None
    output: str | None = None
    output_name: str | None = None
    output_bytes: int = 0
    result: dict = field(default_factory=dict)
    progress: dict | None = None
    created_at: float = field(default_factory=lambda: now())
    finished_at: float | None = None

    def expires_at(self) -> float | None:
        return self.finished_at + OUTPUT_TTL_SECONDS if self.finished_at else None

    def public(self) -> dict:
        out = {"id": self.id, "op": self.op, "status": self.status}
        if self.error:
            out["error"] = self.error
        if self.progress and self.status in ("queued", "running"):
            out["progress"] = self.progress
        if self.result and self.status == "done":
            out["result"] = self.result
        if self.status == "done":
            out["output"] = {"name": self.output_name, "bytes": self.output_bytes,
                             "content_type": _content_type(self)}
            out["expires_at"] = int(self.expires_at() or 0)
        return out


ROOT = os.environ.get("TOOLKIT_ENGINE_DIR",
                      os.path.join(tempfile.gettempdir(), "toolkit-engine-jobs"))
# Jobs are kept as a small job.json in each job directory, so every server
# process (gunicorn runs several) sees the same jobs. _jobs is this process's
# view, refreshed from disk on every read.
_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="engine")
# Tests can set this to run jobs inline (deterministic).
run_inline = False
# A job still "running" this long after it was made lost its process.
STALE_RUNNING_SECONDS = 15 * 60
_ID_OK = set("0123456789abcdef")


def engine_only() -> bool:
    return os.environ.get("TOOLKIT_ENGINE_ONLY", "") == "1"


def _save(job: Job) -> None:
    if not os.path.isdir(job.dir):
        return
    data = {k: getattr(job, k) for k in ("id", "op", "workspace", "user", "status", "error",
                                         "output", "output_name", "output_bytes",
                                         "result", "progress", "created_at", "finished_at")}
    tmp = os.path.join(job.dir, f".job.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, os.path.join(job.dir, "job.json"))


def _load(jid: str) -> Job | None:
    if len(jid) != 32 or not set(jid) <= _ID_OK:
        return None
    d = os.path.join(ROOT, jid)
    try:
        with open(os.path.join(d, "job.json")) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _jobs.get(jid)
    return Job(dir=d, **data)


def _all_jobs() -> list[Job]:
    seen = {}
    if os.path.isdir(ROOT):
        for name in os.listdir(ROOT):
            j = _load(name)
            if j:
                seen[j.id] = j
    for jid, j in _jobs.items():  # in memory only (tests that seed jobs)
        seen.setdefault(jid, j)
    return list(seen.values())


def _active(ws: str | None = None) -> int:
    return sum(1 for j in _all_jobs() if j.status in ("queued", "running")
               and (ws is None or j.workspace == ws))


def sweep() -> int:
    """Delete expired outputs and their jobs; fail jobs whose process died. Returns how many went."""
    t = now()
    gone = 0
    for j in _all_jobs():
        exp = j.expires_at()
        if exp is not None and exp <= t:
            shutil.rmtree(j.dir, ignore_errors=True)
            _jobs.pop(j.id, None)
            gone += 1
        elif j.status in ("queued", "running") and j.created_at < t - STALE_RUNNING_SECONDS:
            j.status, j.error, j.finished_at = "failed", "The file took too long to process.", t
            shutil.rmtree(os.path.join(j.dir, "in"), ignore_errors=True)
            _save(j)
    if os.path.isdir(ROOT):  # directories without a job file, from an earlier process
        for name in os.listdir(ROOT):
            p = os.path.join(ROOT, name)
            if (os.path.isdir(p) and not os.path.exists(os.path.join(p, "job.json"))
                    and name not in _jobs and os.path.getmtime(p) < t - OUTPUT_TTL_SECONDS):
                shutil.rmtree(p, ignore_errors=True)
    return gone


def _zip(paths: list[str], out: str) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, os.path.basename(p))


def _progress(job: Job, done: int, total: int) -> None:
    job.progress = {"done": done, "total": total}
    _save(job)


def _content_type(job: Job) -> str:
    op = OPERATIONS[job.op]
    if job.output_name and job.output_name != op.output_name:
        return content_type_for(job.output_name)
    return op.content_type


def _run_op(job: Job, inputs: list[str], opts: dict, out: str) -> dict:
    """Runs one tool. Returns small facts about the result (never file names)."""
    work = os.path.join(job.dir, "work")
    os.makedirs(work, exist_ok=True)
    op = job.op
    if op in P2_HANDLERS:
        return _run_p2(job, inputs, opts, out, work)
    if op == "compress":
        r = pdfops.compress_pdf(inputs[0], out, opts.get("level") or "medium")
        return {"level": r.level, "in_bytes": r.in_bytes, "out_bytes": r.out_bytes,
                "images_changed": r.images_changed, "already_optimised": r.already_optimised}
    if op == "merge":
        return {"pages": pdfops.merge_pdfs(inputs, out)}
    if op == "convert":
        made = pdfops.office_to_pdf(inputs[0], os.path.dirname(out))
        if made != out:
            os.replace(made, out)
        return {}
    if op == "to-jpeg":
        total = _count_pages(inputs[0])
        _progress(job, 0, total)
        pages = pdfops.pdf_to_jpeg(inputs[0], work, dpi=int(opts["dpi"]), base="page",
                                   on_page=lambda i: _progress(job, i, total))
        _zip(pages, out)
        return {"pages": len(pages)}
    if op == "images-to-pdf":
        return {"pages": pdfops.images_to_pdf(inputs, out)}
    if op == "split":
        parts = pdfops.split_pdf(inputs[0], opts["ranges"], work, base="part")
        _zip(parts, out)
        return {"parts": len(parts)}
    if op == "rotate":
        pdfops.rotate_pdf(inputs[0], out, int(opts["angle"]), opts.get("ranges"))
        return {}
    if op == "scrub":
        pdfops.scrub_metadata(inputs[0], out)
        return {}
    if op == "ocr":
        total = _count_pages(inputs[0])
        _progress(job, 0, total)
        pdfops.ocr_pdf(inputs[0], out, language=opts["language"],
                       on_page=lambda i: _progress(job, i, total))
        return {"pages": total}
    if op == "redact":
        found = pdfops.redact_phrases(inputs[0], out, opts["phrases"])
        return {"matches": found["matches"], "phrases": len(opts["phrases"]),
                "verified": True, "pages_changed": found["pages_rasterised"]}
    if op == "watermark":
        pdfops.watermark_pdf(inputs[0], out, text=opts["text"],
                             opacity=opts["opacity"] / 100)
        return {}
    if op == "unlock":
        pdfops.remove_password(inputs[0], out, opts["password"])
        return {}
    if op == "pdf-to-docx":
        return {"pages_with_text": pdfops.pdf_to_docx_text(inputs[0], out)}
    raise ToolkitError("That tool is not available.")


def _run_p2(job: Job, inputs: list[str], opts: dict, out: str, work: str) -> dict:
    ctx = Ctx(work=work, out_dir=os.path.dirname(out), extra=list(opts.pop("_extra", [])),
              progress=lambda done, total: _progress(job, done, total))
    facts = P2_HANDLERS[job.op](ctx, inputs, opts, out)
    if ctx.output_name and ctx.output_name != OPERATIONS[job.op].output_name:
        if ctx.output_name not in OPERATIONS[job.op].outputs:
            raise RuntimeError("output name not declared")
        job.output_name = ctx.output_name
    return facts


def _process(job: Job, inputs: list[str], opts: dict | None = None) -> None:
    job.status = "running"
    _save(job)
    op = OPERATIONS[job.op]
    out = os.path.join(job.dir, "out", op.output_name)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        job.result = _run_op(job, inputs, opts or {}, out)
        job.output_name = job.output_name or op.output_name
        job.output = os.path.join(os.path.dirname(out), job.output_name)
        job.output_bytes = os.path.getsize(job.output)
        job.status = "done"
    except ToolkitError as e:
        job.status, job.error = "failed", str(e)
    except Exception:  # never leak internals to the caller
        job.status, job.error = "failed", "The file could not be processed."
    finally:
        opts = None  # the password, if any, is not kept
        shutil.rmtree(os.path.join(job.dir, "in"), ignore_errors=True)
        shutil.rmtree(os.path.join(job.dir, "work"), ignore_errors=True)
        job.progress = None
        if job.status == "failed":
            job.output_name = None
        job.finished_at = now()
        if job.status == "failed":
            shutil.rmtree(os.path.join(job.dir, "out"), ignore_errors=True)
        _save(job)


bp = Blueprint("engine", __name__)


def _err(code: str, msg: str, status: int):
    return jsonify({"error": code, "message": msg}), status


def caller() -> service_auth.Caller:
    keys = service_auth.configured_keys()
    if not keys:
        abort(404)
    qs = request.query_string.decode()
    path_qs = request.path + ("?" + qs if qs else "")
    try:
        return service_auth.verify(request.method, path_qs, request.headers,
                                   request.content_length, keys=keys)
    except service_auth.AuthError:
        abort(401)


@bp.get("/internal/v1/operations")
def operations():
    caller()
    return jsonify({"operations": [
        {"slug": o.slug, "label": o.label, "accepts": o.kind,
         "min_files": o.min_files, "max_files": o.max_files,
         "output": {"name": o.output_name, "content_type": o.content_type},
         "options": [x.public() for x in o.options],
         "max_pages": o.max_pages, "max_bytes": o.max_bytes, "slow": o.slow,
         **o.extra_public()}
        for o in OPERATIONS.values()]})


@bp.post("/internal/v1/jobs")
def create_job():
    c = caller()
    op = OPERATIONS.get(request.args.get("op", ""))
    if not op:
        return _err("unknown_op", "That tool is not available.", 400)
    if request.content_length is None:
        return _err("length_required", "Content length is required.", 411)
    if request.content_length > c.max_bytes:
        return _err("too_large", "The upload is larger than your plan allows.", 413)
    if op.max_bytes and request.content_length > op.max_bytes + 64 * 1024:
        return _err("too_large", f"{op.label} takes files up to {op.max_bytes // (1024 * 1024)} MB.", 413)
    sweep()
    with _lock:
        if _active() >= QUEUE_CAP:
            return _err("busy", "The tools are busy. Try again in a minute.", 429)
        if _active(c.workspace) >= PER_WORKSPACE:
            return _err("workspace_busy", "Two tools are already running for this workspace.", 429)
    files = request.files.getlist("files")
    if not (op.min_files <= len(files) <= op.max_files):
        need = (f"exactly {op.min_files} file" if op.min_files == op.max_files
                else f"{op.min_files} to {op.max_files} files")
        return _err("file_count", f"This tool needs {need}.", 400)
    jid = uuid.uuid4().hex
    d = os.path.join(ROOT, jid)
    os.makedirs(os.path.join(d, "in"))
    inputs = []
    try:
        for i, f in enumerate(files):
            ext = ".pdf" if op.kind == "pdf" else os.path.splitext(f.filename or "")[1].lower()
            if op.kind == "office" and ext not in (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"):
                raise ToolkitError("Word, Excel, PowerPoint or OpenDocument files only.")
            if op.kind == "image" and ext not in _IMAGE_EXT:
                raise ToolkitError("JPEG or PNG images only.")
            if op.kind == "file" and ext not in op.exts:
                raise ToolkitError(f"{op.label} takes {_ext_words(op.exts)} files only.")
            p = os.path.join(d, "in", f"{i:02d}{ext}")
            f.save(p)
            if op.kind == "file":
                sniff_ext(p, ext)
            else:
                sniff(p, op.kind)
            if op.kind == "pdf" or (op.kind == "file" and ext == ".pdf"):
                page_count_guard(p)
                if op.max_pages and _count_pages(p) > op.max_pages:
                    raise ToolkitError(f"{op.label} takes PDFs up to {op.max_pages} pages.")
            inputs.append(p)
        opts = parse_options(op, request.form)
        _check_heavy_ocr(op.slug, opts, inputs)
        if op.extra:
            opts["_extra"] = _save_extra(op, d)
    except ToolkitError as e:
        shutil.rmtree(d, ignore_errors=True)
        return _err("bad_file", str(e), 400)
    job = Job(id=jid, op=op.slug, workspace=c.workspace, user=c.user, dir=d)
    _save(job)
    if run_inline:
        _process(job, inputs, opts)
    else:
        _pool.submit(_process, job, inputs, opts)
    return jsonify(job.public()), 202


def _check_heavy_ocr(slug: str, opts: dict, inputs: list[str]) -> None:
    """Refuses, with a plain way forward, OCR runs in a heavy language that
    are longer than HEAVY_OCR_MAX_PAGES (PDF pages or scan photos)."""
    lang = opts.get("language")
    name = HEAVY_OCR_LANGUAGES.get(lang or "")
    if not name:
        return
    cap = HEAVY_OCR_MAX_PAGES
    if slug == "ocr" and inputs:
        n = _count_pages(inputs[0])
        if n > cap:
            raise ToolkitError(
                f"OCR in {name} reads up to {cap} pages at a time and this PDF has {n} pages. "
                f"Split it into parts of {cap} pages with Split PDF, then run OCR on each part.")
    elif slug == "scan-clean" and opts.get("ocr") == "yes" and len(inputs) > cap:
        raise ToolkitError(
            f"Searchable text in {name} works on up to {cap} photos at a time and you added "
            f"{len(inputs)}. Run it in smaller batches, or turn off searchable text.")


def _ext_words(exts: tuple[str, ...]) -> str:
    names = sorted({e.lstrip(".").upper() for e in exts})
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _save_extra(op: Operation, d: str) -> list[str]:
    """Saves the op's second field of files (in order) beside the inputs."""
    ex = op.extra
    files = request.files.getlist(ex.field)
    if not (ex.min <= len(files) <= ex.max):
        raise ToolkitError(f"{ex.label}: send {ex.min} to {ex.max} files.")
    paths = []
    for i, f in enumerate(files):
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in ex.exts:
            raise ToolkitError(f"{ex.label}: {_ext_words(ex.exts)} files only.")
        p = os.path.join(d, "in", f"x{i:02d}{ext}")
        f.save(p)
        if ex.max_bytes_each and os.path.getsize(p) > ex.max_bytes_each:
            raise ToolkitError(f"{ex.label}: each file must be under "
                               f"{max(1, ex.max_bytes_each // 1024)} KB.")
        sniff_ext(p, ext)
        paths.append(p)
    return paths


def _owned(jid: str, c: service_auth.Caller) -> Job:
    sweep()
    job = _load(jid)
    if not job or job.workspace != c.workspace:
        abort(404)
    return job


@bp.get("/internal/v1/jobs/<jid>")
def job_status(jid):
    return jsonify(_owned(jid, caller()).public())


@bp.get("/internal/v1/jobs/<jid>/output")
def job_output(jid):
    job = _owned(jid, caller())
    if job.status != "done" or not job.output or not os.path.exists(job.output):
        abort(404)
    return send_file(job.output, mimetype=_content_type(job),
                     as_attachment=True, download_name=job.output_name)


@bp.delete("/internal/v1/jobs/<jid>")
def job_delete(jid):
    job = _owned(jid, caller())
    _jobs.pop(jid, None)
    shutil.rmtree(job.dir, ignore_errors=True)
    return "", 204


@bp.get("/healthz")
def healthz():
    return jsonify({"ok": True, "engine_only": engine_only(),
                    "active": _active(), "queue_cap": QUEUE_CAP})


ENGINE_PATHS = ("/internal/v1/", "/healthz")


def install(app) -> None:
    app.register_blueprint(bp)

    @app.before_request
    def _engine_only_gate():
        if engine_only() and not request.path.startswith(ENGINE_PATHS):
            abort(404)
