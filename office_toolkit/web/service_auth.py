"""Service to service request signing for engine only mode.

A trusted caller (the Multies Worker) signs every call with HMAC SHA256 over
a canonical string. Users never reach the engine directly.

Canonical string, one field per line, joined with "\n":
    v1, METHOD, path with query, X-Multies-Ts, X-Multies-Ws, X-Multies-User,
    X-Multies-Max-Bytes, content length ("0" when there is no body)

Headers sent by the caller: X-Multies-Key (key id), X-Multies-Ts (unix
seconds), X-Multies-Ws, X-Multies-User, X-Multies-Max-Bytes, X-Multies-Sig
(lowercase hex). Keys come from TOOLKIT_SERVICE_KEYS, either a JSON object
{"stg-1": "secret"} or "stg-1:secret,prod-1:secret".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass

MAX_SKEW_SECONDS = 300
MIN_KEY_LENGTH = 32


@dataclass(frozen=True)
class Caller:
    key_id: str
    workspace: str
    user: str
    max_bytes: int


class AuthError(Exception):
    """Signature missing, stale, replayed or wrong. Always answered as 401."""


def parse_keys(raw: str | None) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        data = json.loads(raw)
        keys = {str(k): str(v) for k, v in data.items()}
    else:
        keys = {}
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            kid, _, secret = part.partition(":")
            keys[kid.strip()] = secret.strip()
    return {k: v for k, v in keys.items() if k and len(v) >= MIN_KEY_LENGTH}


def configured_keys() -> dict[str, str]:
    return parse_keys(os.environ.get("TOOLKIT_SERVICE_KEYS"))


def canonical(method: str, path_qs: str, ts: str, ws: str, user: str,
              max_bytes: str, content_length: str) -> str:
    return "\n".join(["v1", method.upper(), path_qs, ts, ws, user,
                      max_bytes, content_length or "0"])


def sign(secret: str, method: str, path_qs: str, ts: str, ws: str, user: str,
         max_bytes: str, content_length: str) -> str:
    msg = canonical(method, path_qs, ts, ws, user, max_bytes, content_length)
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class ReplayCache:
    """Remembers signatures for twice the skew window; a repeat is refused."""

    def __init__(self, window: int = 2 * MAX_SKEW_SECONDS, cap: int = 50_000):
        self.window = window
        self.cap = cap
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, sig: str, now: float) -> bool:
        with self._lock:
            if len(self._seen) > self.cap or len(self._seen) % 512 == 0:
                cutoff = now - self.window
                self._seen = {k: t for k, t in self._seen.items() if t > cutoff}
            if sig in self._seen:
                return False
            self._seen[sig] = now
            return True


REPLAY = ReplayCache()


def verify(method: str, path_qs: str, headers, content_length: int | None,
           keys: dict[str, str] | None = None, now: float | None = None,
           replay: ReplayCache | None = None) -> Caller:
    keys = configured_keys() if keys is None else keys
    now = time.time() if now is None else now
    replay = REPLAY if replay is None else replay
    kid = headers.get("X-Multies-Key", "")
    ts = headers.get("X-Multies-Ts", "")
    ws = headers.get("X-Multies-Ws", "")
    user = headers.get("X-Multies-User", "")
    max_bytes = headers.get("X-Multies-Max-Bytes", "")
    sig = headers.get("X-Multies-Sig", "")
    if not (kid and ts and ws and user and max_bytes and sig):
        raise AuthError("missing signature headers")
    secret = keys.get(kid)
    if not secret:
        raise AuthError("unknown key")
    try:
        ts_i = int(ts)
        max_i = int(max_bytes)
    except ValueError:
        raise AuthError("bad header")
    if abs(now - ts_i) > MAX_SKEW_SECONDS:
        raise AuthError("stale")
    want = sign(secret, method, path_qs, ts, ws, user, max_bytes,
                str(content_length or 0))
    if not hmac.compare_digest(want, sig.lower()):
        raise AuthError("bad signature")
    if not replay.check_and_add(sig.lower(), now):
        raise AuthError("replayed")
    return Caller(key_id=kid, workspace=ws, user=user, max_bytes=max_i)
