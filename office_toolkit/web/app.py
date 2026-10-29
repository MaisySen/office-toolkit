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
# In SSO (production) mode a missing secret would mint per-process
# sessions that silently break across workers and restarts; fail closed.
# The random fallback stays for local development and tests only.
_secret = os.environ.get("TOOLKIT_SECRET_KEY")
if not _secret and os.environ.get("TOOLKIT_OIDC_ISSUER"):
    raise RuntimeError(
        "TOOLKIT_SECRET_KEY is required when TOOLKIT_OIDC_ISSUER is set")
app.secret_key = _secret or secrets.token_hex(32)
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
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "10.0.2.2")


def local_redirect_problems(redirects):
    """The mobile redirect URIs that must never be accepted by a production
    deployment: loopback or emulator hosts and plain http. The default list
    keeps the Metro dev URI for local runs only."""
    from urllib.parse import urlsplit
    bad = []
    for uri in redirects:
        parts = urlsplit(uri)
        host = (parts.hostname or "").lower()
        if (parts.scheme == "http" or host in _LOOPBACK_HOSTS
                or host.startswith("127.") or host.endswith(".localhost")):
            bad.append(uri)
    return bad


# Secure cookies mark a production (HTTPS) deployment (Dockerfile and
# deploy/env.example set them). There the default list drops the Metro
# dev URI, and an explicit list holding a loopback or http redirect stops
# the app from starting, so no local listener can be handed a code.
_PRODUCTION = os.environ.get("TOOLKIT_SECURE_COOKIES", "") == "1"
_mobile_redirects = [r for r in os.environ.get(
    "TOOLKIT_MOBILE_REDIRECTS",
    "officetoolkit://auth" if _PRODUCTION
    else "officetoolkit://auth,http://localhost:8081/auth").split(",") if r]
if _PRODUCTION:
    _bad_redirects = local_redirect_problems(_mobile_redirects)
    if _bad_redirects:
        raise RuntimeError(
            "TOOLKIT_MOBILE_REDIRECTS holds development redirect URIs not "
            "allowed in production: " + ", ".join(_bad_redirects))
auth_store.register_client(MOBILE_CLIENT_ID, _mobile_redirects)

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


def local_auth_enabled() -> bool:
    """Local email/password accounts are a dev-only mode. When a maintained
    IdP is configured (TOOLKIT_OIDC_ISSUER), registration, local login and
    bootstrap are disabled so no prototype path survives as a fallback."""
    return oidc_client is None


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
        return render_template("auth.html", mode="sso", error=msg,
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
    except Exception as e:
        return fail("Sign-in could not be verified. Try again.")
    email = (claims.get("email") or "").strip().lower()
    email_verified = claims.get("email_verified")
    if not email:
        # Many providers return profile claims only from userinfo (OIDC Core
        # default). Fetch it over the verified TLS channel and require the
        # subject to match the ID token, per spec.
        try:
            meta = oidc_client.load_server_metadata()
            ui = oidc_client.get(meta["userinfo_endpoint"], token=token).json()
        except Exception:
            return fail("The identity provider's userinfo endpoint failed.")
        if str(ui.get("sub", "")) != str(claims.get("sub", "")):
            return fail("Userinfo subject does not match the ID token.")
        email = (ui.get("email") or "").strip().lower()
        email_verified = ui.get("email_verified")
    if not email or "@" not in email:
        return fail("The identity provider did not supply a usable email. "
                    "Map the email scope/claim for this client.")
    if email_verified is False:
        return fail("The identity provider reports this email as unverified.")
    try:
        uid = auth_store.find_or_create_oidc_user(email, claims.get("iss", ""),
                                                  claims.get("sub", ""))
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
                           user_email=auth_store.user_email(current_user_id()))


def current_user_id():
    # cookie sessions must exist server-side so logout actually invalidates
    sid = flask_session.get("sid")
    uid = auth_store.web_session_user(sid)
    if uid is not None:
        return uid
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header[7:]
        if local_auth_enabled():
            uid = auth_store.authenticate_bearer(token)
            if uid is not None:
                return uid
        return _idp_bearer_user_id(token)
    return None


# Bearer tokens minted by the configured IdP (mobile public clients use
# PKCE directly against it). JWKS responses are cached per jwks_uri as
# (jwks, fetched_at, last_forced_refetch_at).
_JWKS_CACHE: dict[str, tuple[dict, float, float]] = {}
_JWKS_TTL = 300.0
# A token naming a key the cache does not hold may mean the provider
# rotated keys, so the JWKS is refetched, but at most once per this many
# seconds per provider: a flood of garbage tokens cannot turn into a
# flood of JWKS requests.
_JWKS_FORCED_REFETCH_INTERVAL = 60.0
# Larger than any real access token; anything bigger is rejected before
# parsing.
_MAX_BEARER_LENGTH = 8192
_ALLOWED_JWT_ALGS = ("RS256",)
EXPECTED_AUDIENCE = os.environ.get("TOOLKIT_OIDC_AUDIENCE", "office-toolkit-api")


def _jwks(jwks_uri: str, force: bool = False):
    """Return the cached JWKS for jwks_uri, fetching on miss, staleness or
    an allowed forced refetch. A failed fetch raises; callers treat that
    as unverifiable."""
    import requests as _requests
    cached = _JWKS_CACHE.get(jwks_uri)
    now = time.time()
    last_forced = cached[2] if cached else 0.0
    if force:
        if now - last_forced < _JWKS_FORCED_REFETCH_INTERVAL:
            return cached[0] if cached else None
        last_forced = now
    elif cached is not None and now - cached[1] <= _JWKS_TTL:
        return cached[0]
    jwks = _requests.get(jwks_uri, timeout=5).json()
    if not isinstance(jwks, dict):
        raise ValueError("JWKS is not a JSON object")
    _JWKS_CACHE[jwks_uri] = (jwks, now, last_forced)
    return jwks


def _jwt_header(token: str) -> dict | None:
    """Cheap structural screen run before any key lookup or network call:
    three non-empty base64url segments, a bounded length, a JSON header
    naming an allowed algorithm and, if present, a short string kid.
    Anything else is not a token this API could ever accept."""
    import base64
    import json
    import re
    if not token or len(token) > _MAX_BEARER_LENGTH:
        return None
    parts = token.split(".")
    if len(parts) != 3 or not all(
            re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
        return None
    try:
        raw = base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4))
        header = json.loads(raw)
    except Exception:
        return None
    if not isinstance(header, dict) or header.get("alg") not in _ALLOWED_JWT_ALGS:
        return None
    kid = header.get("kid")
    if kid is not None and (not isinstance(kid, str) or not kid or len(kid) > 256):
        return None
    return header


def _jwks_has_kid(jwks, kid: str) -> bool:
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    return isinstance(keys, list) and any(
        isinstance(k, dict) and k.get("kid") == kid for k in keys)


def _decode_idp_token(token: str, jwks_uri: str):
    """Decode a JWT against the provider JWKS. Malformed input is rejected
    without touching the network. A kid the cached JWKS lacks (or a
    kid-less token that fails to verify) may mean a key rotation, so the
    JWKS is refetched, rate limited per provider; a token whose kid the
    cache holds but whose signature fails is simply unverifiable."""
    from authlib.jose import JsonWebToken
    header = _jwt_header(token)
    if header is None:
        return None
    jwt = JsonWebToken(list(_ALLOWED_JWT_ALGS))
    kid = header.get("kid")
    try:
        jwks = _jwks(jwks_uri)
        if kid is not None and not _jwks_has_kid(jwks, kid):
            jwks = _jwks(jwks_uri, force=True)
            if not _jwks_has_kid(jwks, kid):
                return None
        try:
            return jwt.decode(token, jwks)
        except Exception:
            if kid is not None:
                return None
            refreshed = _jwks(jwks_uri, force=True)
            if refreshed is jwks:
                return None
            return jwt.decode(token, refreshed)
    except Exception:
        return None


def _configured_issuer() -> str:
    """The issuer this deployment trusts: the configured env value, or the
    metadata URL the client was explicitly registered with (tests). Never
    the issuer a fetched discovery document claims."""
    configured = os.environ.get("TOOLKIT_OIDC_ISSUER", "").rstrip("/")
    if configured:
        return configured
    meta_url = (getattr(oidc_client, "server_metadata_url", None)
                or getattr(oidc_client, "_server_metadata_url", "") or "")
    suffix = "/.well-known/openid-configuration"
    if meta_url.endswith(suffix):
        meta_url = meta_url[: -len(suffix)]
    return meta_url.rstrip("/")


def _idp_bearer_user_id(token: str) -> int | None:
    """Validate a bearer JWT from the configured IdP. Signature against
    the provider JWKS; issuer pinned to the CONFIGURED issuer (not merely
    to what discovery returns); expiry required; the API audience must be
    in aud independently of the authorized party, and the authorized
    party must be the registered mobile client; email is accepted only
    when the IdP marks it verified. Identity resolves by issuer+subject,
    never by email alone. Anything unverifiable means anonymous."""
    if oidc_client is None:
        return None
    import requests as _requests
    try:
        meta = oidc_client.load_server_metadata()
    except Exception:
        return None
    claims = _decode_idp_token(token, meta["jwks_uri"])
    if claims is None:
        return None
    if claims.get("exp") is None:
        return None
    try:
        claims.validate()
    except Exception:
        return None
    configured = _configured_issuer()
    if not configured or claims.get("iss", "").rstrip("/") != configured:
        return None
    aud = claims.get("aud") or []
    if isinstance(aud, str):
        aud = [aud]
    if EXPECTED_AUDIENCE not in aud:
        return None
    if claims.get("azp") != MOBILE_CLIENT_ID:
        return None
    sub = claims.get("sub")
    if not sub:
        return None
    email = ""
    token_ev = claims.get("email_verified")
    if token_ev is False:
        return None
    if token_ev is True and claims.get("email"):
        email = (claims["email"] or "").strip().lower()
    if not email:
        try:
            ui = _requests.get(meta["userinfo_endpoint"], timeout=5,
                               headers={"Authorization": f"Bearer {token}"}).json()
        except Exception:
            return None
        if str(ui.get("sub", "")) != str(sub):
            return None
        if ui.get("email_verified") is not True:
            return None
        email = (ui.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return None
    try:
        return auth_store.find_or_create_oidc_user(email, configured, str(sub))
    except ToolkitError:
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
    if not local_auth_enabled():
        abort(404)  # accounts live at the configured IdP in SSO mode
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
    if not local_auth_enabled():
        if request.method == "POST":
            abort(404)  # no local password login in SSO mode
        return render_template("auth.html", mode="sso", error=None,
                               disabled=None, next=request.args.get("next", "/"))
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
        # local paths only: "//host" is protocol-relative and browsers
        # also normalise "/\host" to it, so both forms are external
        if (not nxt.startswith("/") or nxt.startswith("//")
                or nxt.startswith("/\\")):
            nxt = "/"
        return flask_redirect(nxt)
    return render_template("auth.html", mode="login", error=None, disabled=None,
                           next=request.args.get("next", "/"))


@app.route("/logout", methods=["GET", "POST"])
def logout():
    # Signing out changes state, so it is a POST carrying the CSRF token.
    # A GET only shows the confirm button: a cross-site link or image
    # pointing here can no longer end someone's session.
    if request.method == "GET":
        return render_template("auth.html", mode="logout", error=None,
                               disabled=None)
    if not csrf_ok():
        return render_template("auth.html", mode="logout",
                               error="Form expired. Try again.",
                               disabled=None), 400
    auth_store.drop_web_session(flask_session.get("sid"))
    flask_session.clear()
    return flask_redirect("/login")


# ------------------------------------------------------------- OAuth2 + PKCE

@app.route("/oauth/authorize", methods=["GET", "POST"])
def oauth_authorize():
    if not local_auth_enabled():
        return "Not found", 404
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
    if not local_auth_enabled():
        return jsonify({"error": "not_found"}), 404
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
