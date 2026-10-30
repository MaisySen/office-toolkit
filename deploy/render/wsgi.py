"""WSGI entry for the proxied deploy.

The app host is reachable on its own address too, so every request must
carry the shared header the Cloudflare Worker adds (DOCTOOL_PROXY_SECRET).
Without it the request gets 403, which keeps the public hostname, its TLS
and its edge rules the only way in. /healthz answers without it so the
host and the Worker cron can check liveness; it reveals nothing.

The Worker also passes the public hostname, so URLs the app builds name
https://doctool.multies.si rather than the host's own address.
"""
import hmac
import os

from office_toolkit.web.app import app as flask_app

_SECRET = os.environ.get("DOCTOOL_PROXY_SECRET", "")
_PUBLIC_HOST = os.environ.get("PUBLIC_HOSTNAME", "doctool.multies.si")


def app(environ, start_response):
    if environ.get("PATH_INFO") == "/healthz":
        start_response("200 OK", [("Content-Type", "text/plain"),
                                  ("Cache-Control", "no-store")])
        return [b"ok"]
    given = environ.get("HTTP_X_DOCTOOL_PROXY", "")
    if not _SECRET or not hmac.compare_digest(given, _SECRET):
        start_response("403 Forbidden", [("Content-Type", "text/plain")])
        return [b"Use https://" + _PUBLIC_HOST.encode() + b"\n"]
    environ.pop("HTTP_X_DOCTOOL_PROXY", None)
    environ["HTTP_HOST"] = _PUBLIC_HOST
    environ["SERVER_NAME"] = _PUBLIC_HOST
    environ["SERVER_PORT"] = "443"
    environ["wsgi.url_scheme"] = "https"
    # The Worker sets X-Real-Client-IP from CF-Connecting-IP; use it as
    # the first X-Forwarded-For hop so per-client rate limits see the
    # real client, not the proxy chain.
    real = environ.pop("HTTP_X_REAL_CLIENT_IP", "")
    if real:
        environ["HTTP_X_FORWARDED_FOR"] = real
    return flask_app(environ, start_response)
