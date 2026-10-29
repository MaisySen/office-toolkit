"""Authentication and per-user isolation.

Self-hosted, no third-party service: accounts live in a local SQLite file,
passwords are argon2id hashes, and the mobile app signs in with the standard
OAuth2 authorization-code flow with PKCE (S256), implemented with authlib
primitives. Web UI uses a signed session cookie. Access tokens are opaque,
stored hashed, and expire; refresh tokens rotate.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from office_toolkit.pdfops import ToolkitError

ACCESS_TTL = 3600            # 1 hour
REFRESH_TTL = 30 * 86400     # 30 days
CODE_TTL = 600               # 10 minutes, single use

ph = PasswordHasher()  # argon2id, library defaults

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    kind TEXT NOT NULL,            -- access | refresh
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS web_sessions (
    sid TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    email TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    redirect_uris TEXT NOT NULL,   -- comma-separated, exact match
    public INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS oidc_states (
    state TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthStore:
    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = [r["name"] for r in self.db.execute("PRAGMA table_info(users)")]
        if "provider" not in cols:
            self.db.execute("ALTER TABLE users ADD COLUMN provider "
                            "TEXT NOT NULL DEFAULT 'local'")
        self.db.commit()

    # ------------------------------------------------------------ accounts

    def register(self, email: str, password: str) -> int:
        email = email.strip().lower()
        if not email or "@" not in email:
            raise ToolkitError("A valid email address is required.")
        if len(password) < 8:
            raise ToolkitError("Password needs at least 8 characters.")
        try:
            cur = self.db.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
                (email, ph.hash(password), time.time()))
            self.db.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            raise ToolkitError("That email is already registered.")

    MAX_FAILURES = 5
    LOCK_SECONDS = 300

    def verify_login(self, email: str, password: str) -> int:
        email = email.strip().lower()
        row = self.db.execute("SELECT failures, locked_until FROM login_attempts "
                              "WHERE email=?", (email,)).fetchone()
        if row and row["locked_until"] > time.time():
            raise ToolkitError("Too many failed attempts. Try again in a few minutes.")
        user = self.db.execute(
            "SELECT id, password_hash, provider FROM users WHERE email=?",
            (email,)).fetchone()
        if not user:
            # equalise timing against user-enumeration via response time
            ph.hash("dummy-password")
            self._record_failure(email)
            raise ToolkitError("Wrong email or password.")
        if user["provider"] != "local":
            ph.hash("dummy-password")
            raise ToolkitError("This account uses single sign-on. "
                               "Use the SSO button on the login page.")
        try:
            ph.verify(user["password_hash"], password)
        except (VerificationError, VerifyMismatchError):
            self._record_failure(email)
            raise ToolkitError("Wrong email or password.")
        self.db.execute("DELETE FROM login_attempts WHERE email=?", (email,))
        self.db.commit()
        return user["id"]

    def _record_failure(self, email: str) -> None:
        self.db.execute(
            "INSERT INTO login_attempts (email, failures, locked_until) VALUES (?, 1, 0) "
            "ON CONFLICT(email) DO UPDATE SET failures = failures + 1", (email,))
        self.db.execute(
            "UPDATE login_attempts SET locked_until=? WHERE email=? AND failures>=?",
            (time.time() + self.LOCK_SECONDS, email, self.MAX_FAILURES))
        self.db.commit()

    def user_email(self, user_id: int) -> str | None:
        row = self.db.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        return row["email"] if row else None

    # ------------------------------------------------------------- clients

    def register_client(self, client_id: str, redirect_uris: list[str]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO clients (client_id, redirect_uris, public) VALUES (?,?,1)",
            (client_id, ",".join(redirect_uris)))
        self.db.commit()

    # ------------------------------------------------------ code + PKCE flow

    def issue_code(self, user_id: int, client_id: str, redirect_uri: str,
                   code_challenge: str, method: str = "S256") -> str:
        if method != "S256":
            raise ToolkitError("PKCE method must be S256.")
        client = self.db.execute("SELECT redirect_uris FROM clients WHERE client_id=?",
                                 (client_id,)).fetchone()
        if not client:
            raise ToolkitError("Unknown client.")
        if redirect_uri not in client["redirect_uris"].split(","):
            raise ToolkitError("Redirect URI is not registered for this client.")
        code = secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO auth_codes (code, user_id, client_id, redirect_uri, "
            "code_challenge, expires_at) VALUES (?,?,?,?,?,?)",
            (code, user_id, client_id, redirect_uri, code_challenge,
             time.time() + CODE_TTL))
        self.db.commit()
        return code

    def exchange_code(self, code: str, client_id: str, redirect_uri: str,
                      code_verifier: str) -> dict:
        row = self.db.execute("SELECT * FROM auth_codes WHERE code=?", (code,)).fetchone()
        if not row or row["used"] or row["expires_at"] < time.time():
            raise ToolkitError("Code is invalid, used or expired.")
        if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
            raise ToolkitError("Code was not issued to this client and redirect.")
        digest = hashlib.sha256(code_verifier.encode()).digest()
        import base64
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if not hmac.compare_digest(computed, row["code_challenge"]):
            raise ToolkitError("PKCE verification failed.")
        self.db.execute("UPDATE auth_codes SET used=1 WHERE code=?", (code,))
        self.db.commit()
        return self._issue_token_pair(row["user_id"], client_id)

    def _issue_token_pair(self, user_id: int, client_id: str) -> dict:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(40)
        now = time.time()
        self.db.execute(
            "INSERT INTO tokens (token_hash, user_id, client_id, kind, expires_at) "
            "VALUES (?,?,?,?,?)", (_hash(access), user_id, client_id, "access",
                                   now + ACCESS_TTL))
        self.db.execute(
            "INSERT INTO tokens (token_hash, user_id, client_id, kind, expires_at) "
            "VALUES (?,?,?,?,?)", (_hash(refresh), user_id, client_id, "refresh",
                                   now + REFRESH_TTL))
        self.db.commit()
        return {"access_token": access, "token_type": "Bearer",
                "expires_in": ACCESS_TTL, "refresh_token": refresh}

    def refresh(self, refresh_token: str, client_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM tokens WHERE token_hash=? AND kind='refresh'",
            (_hash(refresh_token),)).fetchone()
        if row and row["expires_at"] >= time.time() and row["client_id"] == client_id:
            # normal rotation: old refresh token dies immediately
            self.db.execute("DELETE FROM tokens WHERE token_hash=?", (row["token_hash"],))
            return self._issue_token_pair(row["user_id"], client_id)
        if row and row["expires_at"] >= time.time() and row["client_id"] != client_id:
            # token presented to a different client: treat as replay, kill the family
            self.revoke_user_tokens(row["user_id"])
            raise ToolkitError("Refresh token misuse; all sessions for this account were revoked.")
        raise ToolkitError("Refresh token is invalid or expired.")

    # ------------------------------------------------------------ oidc

    def find_or_create_oidc_user(self, email: str, issuer: str) -> int:
        # Resolve an SSO login to a local account. Never links silently: a
        # pre-existing local password account with the same email stays
        # password-only, so a misconfigured provider cannot take it over.
        # Account linking is a deliberate manual step.
        provider = "oidc:" + issuer.rstrip("/")
        email = email.strip().lower()
        row = self.db.execute("SELECT id, provider FROM users WHERE email=?",
                              (email,)).fetchone()
        if row:
            if row["provider"] == provider:
                return row["id"]
            if row["provider"] == "local":
                raise ToolkitError(
                    "An account with this email already exists. "
                    "Sign in with your password instead.")
            raise ToolkitError("This email is registered with a different "
                               "identity provider.")
        cur = self.db.execute(
            "INSERT INTO users (email, password_hash, created_at, provider) "
            "VALUES (?,?,?,?)", (email, "", time.time(), provider))
        self.db.commit()
        return cur.lastrowid

    OIDC_STATE_TTL = 600  # 10 minutes, single use

    def save_oidc_state(self, state: str, nonce: str, code_verifier: str) -> None:
        self.db.execute("DELETE FROM oidc_states WHERE expires_at<?", (time.time(),))
        self.db.execute(
            "INSERT INTO oidc_states (state, nonce, code_verifier, expires_at) "
            "VALUES (?,?,?,?)", (state, nonce, code_verifier,
                                 time.time() + self.OIDC_STATE_TTL))
        self.db.commit()

    def pop_oidc_state(self, state: str) -> dict | None:
        # Consume a state row. Single use: a replayed state finds nothing.
        row = self.db.execute("SELECT nonce, code_verifier, expires_at "
                              "FROM oidc_states WHERE state=?", (state,)).fetchone()
        self.db.execute("DELETE FROM oidc_states WHERE state=?", (state,))
        self.db.commit()
        if not row or row["expires_at"] < time.time():
            return None
        return {"nonce": row["nonce"], "code_verifier": row["code_verifier"]}

    # ------------------------------------------------------- web sessions


    def create_web_session(self, user_id: int) -> str:
        sid = secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO web_sessions (sid, user_id, created_at) "
                        "VALUES (?,?,?)", (sid, user_id, time.time()))
        self.db.commit()
        return sid

    def web_session_user(self, sid: str | None) -> int | None:
        if not sid:
            return None
        row = self.db.execute("SELECT user_id FROM web_sessions WHERE sid=?",
                              (sid,)).fetchone()
        return row["user_id"] if row else None

    def drop_web_session(self, sid: str | None) -> None:
        if sid:
            self.db.execute("DELETE FROM web_sessions WHERE sid=?", (sid,))
            self.db.commit()

    # ------------------------------------------------------ bearer tokens

    def authenticate_bearer(self, token: str | None) -> int | None:
        if not token:
            return None
        row = self.db.execute(
            "SELECT user_id, expires_at FROM tokens WHERE token_hash=? AND kind='access'",
            (_hash(token),)).fetchone()
        if not row or row["expires_at"] < time.time():
            return None
        return row["user_id"]

    def revoke_user_tokens(self, user_id: int) -> None:
        self.db.execute("DELETE FROM tokens WHERE user_id=?", (user_id,))
        self.db.commit()
