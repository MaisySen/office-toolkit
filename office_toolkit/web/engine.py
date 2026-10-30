"""Engine only mode: signed internal job API for a trusted caller.

When TOOLKIT_ENGINE_ONLY=1 the engine answers only /healthz and
/internal/v1/*; the web UI, local accounts, OIDC and /api/run/* are 404.
Jobs run on a small thread pool. Inputs are deleted when the operation
ends; outputs are deleted OUTPUT_TTL_SECONDS after they are made.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from flask import Blueprint, abort, jsonify, request, send_file

from office_toolkit import pdfops
from office_toolkit.pdfops import ToolkitError
from office_toolkit.web import service_auth
from office_toolkit.web.security import page_count_guard, sniff

OUTPUT_TTL_SECONDS = 60 * 60
POOL_SIZE = 2
QUEUE_CAP = 20
PER_WORKSPACE = 2
MAX_FILES = 20

# Clock seam for tests.
now = time.time


@dataclass(frozen=True)
class Operation:
    slug: str
    label: str
    kind: str          # pdf or office
    min_files: int
    max_files: int
    output_name: str
    content_type: str = "application/pdf"


OPERATIONS = {
    "compress": Operation("compress", "Compress PDF", "pdf", 1, 1, "compressed.pdf"),
    "merge": Operation("merge", "Merge PDFs", "pdf", 2, MAX_FILES, "merged.pdf"),
    "convert": Operation("convert", "Convert to PDF", "office", 1, 1, "converted.pdf"),
}


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
    created_at: float = field(default_factory=lambda: now())
    finished_at: float | None = None

    def expires_at(self) -> float | None:
        return self.finished_at + OUTPUT_TTL_SECONDS if self.finished_at else None

    def public(self) -> dict:
        out = {"id": self.id, "op": self.op, "status": self.status}
        if self.error:
            out["error"] = self.error
        if self.status == "done":
            out["output"] = {"name": self.output_name, "bytes": self.output_bytes,
                             "content_type": OPERATIONS[self.op].content_type}
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
                                         "created_at", "finished_at")}
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


def _process(job: Job, inputs: list[str]) -> None:
    job.status = "running"
    _save(job)
    op = OPERATIONS[job.op]
    out = os.path.join(job.dir, "out", op.output_name)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        if job.op == "compress":
            pdfops.compress_pdf(inputs[0], out)
        elif job.op == "merge":
            pdfops.merge_pdfs(inputs, out)
        elif job.op == "convert":
            made = pdfops.office_to_pdf(inputs[0], os.path.dirname(out))
            if made != out:
                os.replace(made, out)
        job.output = out
        job.output_bytes = os.path.getsize(out)
        job.output_name = op.output_name
        job.status = "done"
    except ToolkitError as e:
        job.status, job.error = "failed", str(e)
    except Exception:  # never leak internals to the caller
        job.status, job.error = "failed", "The file could not be processed."
    finally:
        shutil.rmtree(os.path.join(job.dir, "in"), ignore_errors=True)
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
         "min_files": o.min_files, "max_files": o.max_files}
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
            p = os.path.join(d, "in", f"{i:02d}{ext}")
            f.save(p)
            sniff(p, op.kind)
            if op.kind == "pdf":
                page_count_guard(p)
            inputs.append(p)
    except ToolkitError as e:
        shutil.rmtree(d, ignore_errors=True)
        return _err("bad_file", str(e), 400)
    job = Job(id=jid, op=op.slug, workspace=c.workspace, user=c.user, dir=d)
    _save(job)
    if run_inline:
        _process(job, inputs)
    else:
        _pool.submit(_process, job, inputs)
    return jsonify(job.public()), 202


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
    return send_file(job.output, mimetype=OPERATIONS[job.op].content_type,
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
