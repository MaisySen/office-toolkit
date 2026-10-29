"""Flask web UI for the office toolkit.

Local-first: uploaded files live in a per-job temp dir, are processed in
process, and results are streamed back. Nothing is sent to third parties.
"""
import io
import os
import secrets
import shutil
import tempfile
import threading
import time
import uuid
import zipfile

from flask import Flask, render_template, request, send_file, abort
from flask import redirect as flask_redirect
from flask import session as flask_session

from office_toolkit import pdfops
from office_toolkit.pdfops import ToolkitError
from office_toolkit.web.auth import AuthStore
from office_toolkit.web.security import sniff, page_count_guard

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB per request
app.secret_key = os.environ.get("TOOLKIT_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("TOOLKIT_SECURE_COOKIES", "") == "1",
)
BOOTSTRAP_TOKEN = os.environ.get("TOOLKIT_BOOTSTRAP_TOKEN", "")

AUTH_DB = os.environ.get("TOOLKIT_AUTH_DB",
                         os.path.join(tempfile.gettempdir(), "toolkit-auth.db"))
auth_store = AuthStore(AUTH_DB)
REGISTRATION_OPEN = os.environ.get("TOOLKIT_REGISTRATION_OPEN", "") == "1"
MOBILE_CLIENT_ID = os.environ.get("TOOLKIT_MOBILE_CLIENT_ID", "office-toolkit-mobile")
auth_store.register_client(
    MOBILE_CLIENT_ID,
    [r for r in os.environ.get("TOOLKIT_MOBILE_REDIRECTS",
                               "officetoolkit://auth,http://localhost:8081/auth").split(",") if r])

# Stable base so a restart can recover and sweep orphans from a crashed run.
OIDC_SCOPES = os.environ.get("TOOLKIT_OIDC_SCOPES", "openid email profile")
oidc_client = None
if os.environ.get("TOOLKIT_OIDC_ISSUER"):
    from authlib.integrations.flask_client import OAuth
    _oauth = OAuth(app)
    oidc_client = _oauth.register(
        "oidc",
        client_id=os.environ.get("TOOLKIT_OIDC_CLIENT_ID", ""),
        client_secret=os.environ.get("TOOLKIT_OIDC_CLIENT_SECRET", ""),
        server_metadata_url=os.environ["TOOLKIT_OIDC_ISSUER"].rstrip("/")
        + "/.well-known/openid-configuration",
        client_kwargs={"scope": OIDC_SCOPES, "code_challenge_method": "S256"},
    )


@app.get("/login/oidc")
def oidc_start():
    if oidc_client is None:
        abort(404)
    nonce = secrets.token_urlsafe(24)
    flask_session["oidc_nonce"] = nonce
    redirect_uri = request.url_root.rstrip("/") + "/login/oidc/callback"
    return oidc_client.authorize_redirect(redirect_uri, nonce=nonce)


@app.get("/login/oidc/callback")
def oidc_callback():
    if oidc_client is None:
        abort(404)

    def fail(msg):
        return render_template("auth.html", mode="login", error=msg,
                               disabled=None, next="/"), 400

    if request.args.get("error"):
        return fail("Sign-in was cancelled or failed at the provider.")
    nonce = flask_session.pop("oidc_nonce", None)
    try:
        token = oidc_client.authorize_access_token()
        # authlib verifies the signature against the provider JWKS, and the
        # iss, aud/azp, exp and iat claims; nonce binds the token to this
        # browser session's login attempt.
        claims = oidc_client.parse_id_token(token, nonce=nonce)
    except Exception:
        return fail("Sign-in could not be verified. Try again.")
    email = (claims.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return fail("The identity provider did not supply a usable email. "
                    "Map the email scope/claim for this client.")
    if claims.get("email_verified") is False:
        return fail("The identity provider reports this email as unverified.")
    try:
        uid = auth_store.find_or_create_oidc_user(email, claims.get("iss", ""))
    except ToolkitError as e:
        return fail(str(e))
    flask_session.clear()  # session fixation: fresh session on privilege change
    flask_session["sid"] = auth_store.create_web_session(uid)
    flask_session["_csrf"] = secrets.token_urlsafe(24)
    return flask_redirect("/")


JOBS = os.path.join(tempfile.gettempdir(), "toolkit-jobs")
os.makedirs(JOBS, exist_ok=True)
JOB_TTL_SECONDS = 30 * 60


def sweep_old_jobs() -> None:  # also runs once at startup below
    """Delete job dirs older than JOB_TTL_SECONDS. Runs before each new job."""
    cutoff = time.time() - JOB_TTL_SECONDS
    for name in os.listdir(JOBS):
        p = os.path.join(JOBS, name)
        if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
            shutil.rmtree(p, ignore_errors=True)


sweep_old_jobs.__doc__ = "Delete job dirs older than JOB_TTL_SECONDS."


def jobdir(owner: int | None = None) -> str:
    d = os.path.join(JOBS, uuid.uuid4().hex[:12])
    os.makedirs(d)
    if owner is not None:
        with open(os.path.join(d, ".owner"), "w") as fh:
            fh.write(str(owner))
    # Guarantee expiry independent of future requests; the sweep is a backstop.
    t = threading.Timer(JOB_TTL_SECONDS, shutil.rmtree, args=(d,),
                        kwargs={"ignore_errors": True})
    t.daemon = True
    t.start()
    return d


KIND_BY_OP = {"merge": "pdf", "split": "pdf", "rotate": "pdf", "compress": "pdf",
              "scrub": "pdf", "unlock": "pdf", "watermark": "pdf", "to-jpeg": "pdf",
              "redact": "pdf", "convert": "office", "ocr": "pdf", "replace": "pdf",
              "images-to-pdf": "image", "pdf-to-docx": "pdf"}


def save_upload(field, d, required=True, kind=None):
    f = request.files.get(field)
    if not f or not f.filename:
        if required:
            raise ToolkitError("No file uploaded.")
        return None
    p = os.path.join(d, os.path.basename(f.filename))
    f.save(p)
    if kind:
        sniff(p, kind)
        if kind == "pdf":
            page_count_guard(p)
    return p


def page():
    return render_template("index.html", ops=OPS, ocr_langs=pdfops.list_ocr_languages(),
                           user_email=auth_store.user_email(flask_session.get("user_id")))


def current_user_id():
    # cookie sessions must exist server-side so logout actually invalidates
    sid = flask_session.get("sid")
    uid = auth_store.web_session_user(sid)
    if uid is not None:
        return uid
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return auth_store.authenticate_bearer(header[7:])
    return None


def csrf_token() -> str:
    if "_csrf" not in flask_session:
        flask_session["_csrf"] = secrets.token_urlsafe(24)
    return flask_session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["oidc_enabled"] = lambda: oidc_client is not None


def csrf_ok() -> bool:
    """Cookie-authenticated POSTs need the synchronizer token. Bearer calls
    and the OAuth token endpoint are exempt (no ambient credential)."""
    if request.headers.get("Authorization", "").startswith("Bearer "):
        return True
    if request.path == "/oauth/token":
        return True
    import hmac as _hmac
    token = flask_session.get("_csrf", "")
    return bool(token) and _hmac.compare_digest(request.form.get("_csrf", ""), token)


def require_user():
    uid = current_user_id()
    if uid is None:
        if request.path.startswith("/api/"):
            from flask import jsonify
            return None, (jsonify({"error": "Sign in required."}), 401)
        return None, flask_redirect("/login?next=" + request.path)
    return uid, None


sweep_old_jobs()  # boot-time recovery of orphans from a previous process


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline' 'self'"
    return resp


@app.get("/")
def index():
    return page()


def run(fn):
    uid, deny = require_user()
    if deny:
        return deny
    if not csrf_ok():
        return page_with_error("Form expired. Go back and try again.")
    if rate_limited():
        return page_with_error("Too many requests - wait a minute and try again.")
    sweep_old_jobs()
    d = jobdir(owner=uid)
    try:
        result = fn(d)
    except ToolkitError as e:
        shutil.rmtree(d, ignore_errors=True)
        return page_with_error(str(e))
    except Exception as e:  # unexpected: still clean up, show generic message
        shutil.rmtree(d, ignore_errors=True)
        return page_with_error(f"Unexpected error: {e}")
    return result


def page_with_error(msg):
    return render_template("index.html", ops=OPS, error=msg,
                           ocr_langs=pdfops.list_ocr_languages()), 400


def send_path(path, download_name=None):
    return send_file(path, as_attachment=True,
                     download_name=download_name or os.path.basename(path))


def zip_dir(paths, zip_name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, os.path.basename(p))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=zip_name)


def op_merge(d):
    files = request.files.getlist("files")
    if len(files) < 2:
        raise ToolkitError("Upload at least two PDFs to merge.")
    paths = []
    for f in files:
        p = os.path.join(d, os.path.basename(f.filename))
        f.save(p)
        sniff(p, "pdf"); page_count_guard(p)
        paths.append(p)
    out = os.path.join(d, "merged.pdf")
    pdfops.merge_pdfs(paths, out)
    return send_path(out)


def op_split(d):
    src = save_upload("file", d, kind="pdf")
    outs = pdfops.split_pdf(src, request.form.get("ranges", "1"), d)
    return zip_dir(outs, "split-pages.zip")


def op_rotate(d):
    src = save_upload("file", d, kind="pdf")
    out = os.path.join(d, "rotated.pdf")
    spec = request.form.get("ranges") or None
    pdfops.rotate_pdf(src, out, int(request.form.get("angle", "90")), spec)
    return send_path(out)


def op_compress(d):
    src = save_upload("file", d, kind="pdf")
    out = os.path.join(d, "compressed.pdf")
    res = pdfops.compress_pdf(src, out)
    return send_path(out)


def op_scrub(d):
    src = save_upload("file", d, kind="pdf")
    out = os.path.join(d, "metadata-removed.pdf")
    pdfops.scrub_metadata(src, out)
    return send_path(out)


def op_unlock(d):
    src = save_upload("file", d, kind="pdf")
    out = os.path.join(d, "unlocked.pdf")
    pdfops.remove_password(src, out, request.form.get("password", ""))
    return send_path(out)


def op_watermark(d):
    src = save_upload("file", d, kind="pdf")
    text = request.form.get("text") or None
    img = save_upload("mark_image", d, required=False, kind="image")
    out = os.path.join(d, "watermarked.pdf")
    pdfops.watermark_pdf(src, out, text=text, image=img,
                         opacity=float(request.form.get("opacity", "0.25")))
    return send_path(out)


def op_to_jpeg(d):
    src = save_upload("file", d, kind="pdf")
    outs = pdfops.pdf_to_jpeg(src, d, dpi=int(request.form.get("dpi", "150")))
    return zip_dir(outs, "pages-jpeg.zip")


def op_redact(d):
    src = save_upload("file", d, kind="pdf")
    needle = request.form.get("find", "").strip()
    if not needle:
        raise ToolkitError("Enter the exact text to redact.")
    from pypdf import PdfReader
    if not any((p.extract_text() or "").strip() for p in PdfReader(src).pages):
        raise ToolkitError("This PDF has no text layer (it looks like a scan). "
                           "Run OCR first, then redact.")
    rects = pdfops.find_text_rects(src, needle)
    if not rects:
        raise ToolkitError(f"No occurrence of '{needle}' found in the text layer.")
    out = os.path.join(d, "redacted.pdf")
    pdfops.redact_pdf(src, out, rects, dpi=int(request.form.get("dpi", "200")),
                      verify_needle=needle)
    return send_path(out)


def op_convert(d):
    src = save_upload("file", d, kind="office")
    out = pdfops.office_to_pdf(src, d)
    return send_path(out)


def op_ocr(d):
    src = save_upload("file", d, kind="pdf")
    out = os.path.join(d, "searchable.pdf")
    pdfops.ocr_pdf(src, out, language=request.form.get("language", "eng"))
    return send_path(out)


def op_images_to_pdf(d):
    files = request.files.getlist("files")
    if not files:
        raise ToolkitError("Upload at least one image.")
    paths = []
    for f in files:
        p = os.path.join(d, os.path.basename(f.filename))
        f.save(p)
        sniff(p, "image")
        paths.append(p)
    out = os.path.join(d, "images.pdf")
    pdfops.images_to_pdf(paths, out)
    return send_path(out)


def op_pdf_to_docx(d):
    src = save_upload("file", d, kind="pdf")
    out = os.path.join(d, "document.docx")
    pdfops.pdf_to_docx_text(src, out)
    return send_path(out)


def op_replace(d):
    src = save_upload("file", d, kind="pdf")
    out = os.path.join(d, "edited.pdf")
    n = pdfops.replace_text(src, out, request.form.get("old", ""),
                            request.form.get("new", ""))
    if n == 0:
        raise ToolkitError("The text was not found as a simple string. "
                           "This tool only handles short, simple replacements.")
    return send_path(out)


OPS = [
    ("merge", "Merge PDFs", op_merge),
    ("split", "Split / extract pages", op_split),
    ("rotate", "Rotate pages", op_rotate),
    ("compress", "Compress PDF", op_compress),
    ("scrub", "Remove metadata", op_scrub),
    ("unlock", "Remove password (with the password)", op_unlock),
    ("watermark", "Watermark PDF", op_watermark),
    ("to-jpeg", "PDF to JPEG", op_to_jpeg),
    ("redact", "Redact text permanently", op_redact),
    ("convert", "Office file to PDF", op_convert),
    ("ocr", "Make scanned PDF searchable (OCR)", op_ocr),
    ("replace", "Replace short text", op_replace),
    ("images-to-pdf", "Images to PDF", op_images_to_pdf),
    ("pdf-to-docx", "PDF to Word (text only)", op_pdf_to_docx),
]

# --- naive per-IP sliding-window rate limit (in-memory, per process) ---
_RATE: dict[str, list[float]] = {}
RATE_LIMIT = 30          # processing requests
RATE_WINDOW = 60.0       # seconds


def rate_limited() -> bool:
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    now = time.time()
    hits = [t for t in _RATE.get(ip, []) if now - t < RATE_WINDOW]
    if len(hits) >= RATE_LIMIT:
        _RATE[ip] = hits
        return True
    hits.append(now)
    _RATE[ip] = hits
    return False


# ------------------------------------------------------------------- auth UI

@app.route("/register", methods=["GET", "POST"])
def register():
    user_count = auth_store.db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if not REGISTRATION_OPEN and user_count > 0:
        return render_template("auth.html", mode="register", error=None,
                               disabled="Registration is closed. Ask the owner for an account."), 403
    if not REGISTRATION_OPEN and user_count == 0 and not BOOTSTRAP_TOKEN:
        return render_template("auth.html", mode="register", error=None,
                               disabled="This install has no accounts yet. The owner must set "
                                        "TOOLKIT_BOOTSTRAP_TOKEN before the first account can be created."), 403
    if request.method == "POST":
        if not REGISTRATION_OPEN and user_count == 0:
            import hmac as _hmac
            if not _hmac.compare_digest(request.form.get("bootstrap_token", ""), BOOTSTRAP_TOKEN):
                return render_template("auth.html", mode="register",
                                       error="Wrong bootstrap token.", disabled=None,
                                       bootstrap=True), 403
        try:
            uid = auth_store.register(request.form.get("email", ""),
                                      request.form.get("password", ""))
        except ToolkitError as e:
            return render_template("auth.html", mode="register", error=str(e), disabled=None), 400
        flask_session.clear()  # session fixation: fresh session on privilege change
        flask_session["sid"] = auth_store.create_web_session(uid)
        flask_session["_csrf"] = secrets.token_urlsafe(24)
        return flask_redirect("/")
    return render_template("auth.html", mode="register", error=None, disabled=None,
                           bootstrap=(not REGISTRATION_OPEN and user_count == 0))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not csrf_ok():
            return render_template("auth.html", mode="login",
                                   error="Form expired. Try again.", disabled=None), 400
        try:
            uid = auth_store.verify_login(request.form.get("email", ""),
                                          request.form.get("password", ""))
        except ToolkitError as e:
            return render_template("auth.html", mode="login", error=str(e), disabled=None), 400
        flask_session.clear()  # session fixation: fresh session on privilege change
        flask_session["sid"] = auth_store.create_web_session(uid)
        flask_session["_csrf"] = secrets.token_urlsafe(24)
        nxt = request.args.get("next") or request.form.get("next") or "/"
        if not nxt.startswith("/"):
            nxt = "/"
        return flask_redirect(nxt)
    return render_template("auth.html", mode="login", error=None, disabled=None,
                           next=request.args.get("next", "/"))


@app.get("/logout")
def logout():
    auth_store.drop_web_session(flask_session.get("sid"))
    flask_session.clear()
    return flask_redirect("/login")


# ------------------------------------------------------------- OAuth2 + PKCE

@app.route("/oauth/authorize", methods=["GET", "POST"])
def oauth_authorize():
    client_id = request.values.get("client_id", "")
    redirect_uri = request.values.get("redirect_uri", "")
    challenge = request.values.get("code_challenge", "")
    method = request.values.get("code_challenge_method", "")
    state = request.values.get("state", "")
    uid = current_user_id()
    if not uid:
        return flask_redirect("/login?next=" + request.full_path.rstrip("?"))
    try:
        code = auth_store.issue_code(uid, client_id, redirect_uri, challenge, method)
    except ToolkitError as e:
        return render_template("auth.html", mode="login", error=str(e), disabled=None), 400
    sep = "&" if "?" in redirect_uri else "?"
    return flask_redirect(f"{redirect_uri}{sep}code={code}&state={state}")


@app.post("/oauth/token")
def oauth_token():
    from flask import jsonify
    grant = request.form.get("grant_type")
    try:
        if grant == "authorization_code":
            pair = auth_store.exchange_code(
                request.form.get("code", ""), request.form.get("client_id", ""),
                request.form.get("redirect_uri", ""), request.form.get("code_verifier", ""))
        elif grant == "refresh_token":
            pair = auth_store.refresh(request.form.get("refresh_token", ""),
                                      request.form.get("client_id", ""))
        else:
            return jsonify({"error": "unsupported_grant_type"}), 400
    except ToolkitError as e:
        return jsonify({"error": "invalid_grant", "error_description": str(e)}), 400
    return jsonify(pair)


@app.get("/api/me")
def api_me():
    from flask import jsonify
    uid, deny = require_user()
    if deny:
        return deny
    return jsonify({"user_id": uid, "email": auth_store.user_email(uid)})


def api_run(fn):
    from flask import jsonify
    uid, deny = require_user()
    if deny:
        return deny
    if rate_limited():
        return jsonify({"error": "Too many requests - wait a minute and try again."}), 429
    sweep_old_jobs()
    d = jobdir(owner=uid)
    try:
        return fn(d)
    except ToolkitError as e:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.get("/api/operations")
def api_operations():
    from flask import jsonify
    return jsonify({"operations": [{"slug": s, "label": l} for s, l, _ in OPS]})


for slug, _label, fn in OPS:
    app.add_url_rule(f"/run/{slug}", endpoint=f"run_{slug}",
                     view_func=(lambda f: (lambda: run(f)))(fn), methods=["POST"])
    app.add_url_rule(f"/api/run/{slug}", endpoint=f"api_run_{slug}",
                     view_func=(lambda f: (lambda: api_run(f)))(fn), methods=["POST"])
