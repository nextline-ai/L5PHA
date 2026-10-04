"""Server-side third-party initData verification; no public Bot Token required.

This module is not an HTTP server. Binding methods must only be exposed as
authenticated, rate-limited POST requests with Origin/CSRF controls.
"""

import base64
import binascii
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qsl

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from veyquant.store import Store

PRODUCTION_KEY = bytes.fromhex("e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d")
SESSION_TTL = 7 * 86400
SESSION_ABSOLUTE_TTL = 30 * 86400


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class Identity:
    user_id: int
    auth_date: int
    fingerprint: str


class TelegramVerifier:
    def __init__(self, bot_id: int, max_age: int = 300, *, public_key: bytes = PRODUCTION_KEY):
        # public_key injection is only for local cryptographic tests, never client input.
        if type(bot_id) is not int or bot_id <= 0 or not 1 <= max_age <= 600:
            raise ValueError("invalid authentication configuration")
        self.bot_id, self.max_age = bot_id, max_age
        self.key = Ed25519PublicKey.from_public_bytes(public_key)

    def verify(self, raw: str, now: int) -> Identity:
        try:
            if not isinstance(raw, str) or not 1 <= len(raw) <= 16384:
                raise ValueError
            if re.search(r"%(?![0-9A-Fa-f]{2})", raw):
                raise ValueError
            pairs = parse_qsl(
                raw, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=64
            )
            fields = dict(pairs)
            if len(fields) != len(pairs) or any("\n" in k or "\r" in k for k in fields):
                raise ValueError
            signature = fields["signature"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", signature):
                raise ValueError
            sig = base64.b64decode(
                signature + "=" * (-len(signature) % 4), altchars=b"-_", validate=True
            )
            checked = "\n".join(
                f"{k}={v}" for k, v in sorted(fields.items()) if k not in {"signature", "hash"}
            )
            message = f"{self.bot_id}:WebAppData\n{checked}".encode()
            self.key.verify(sig, message)
            auth_date = int(fields["auth_date"])
            if now < auth_date:
                raise ValueError
            user = json.loads(fields["user"])
            if not isinstance(user, dict):
                raise ValueError
            user_id = user["id"]
            if type(user_id) is not int or not 0 < user_id < 2**52:
                raise ValueError
            identity = Identity(user_id, auth_date, hashlib.sha256(message + sig).hexdigest())
        except (
            ValueError,
            KeyError,
            TypeError,
            UnicodeError,
            InvalidSignature,
            binascii.Error,
        ) as error:
            raise AuthenticationError("invalid_telegram_authentication") from error
        if now - auth_date > self.max_age:
            raise AuthenticationError("telegram_auth_expired")
        return identity


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerAuth:
    """Persistent one-owner binding and revocable sessions for one deployment."""

    def __init__(self, store: Store, verifier: TelegramVerifier):
        self.store, self.verifier = store, verifier
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS owner (
                id INTEGER PRIMARY KEY CHECK(id=1), telegram_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invitations (
                token_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS auth_replays (
                fingerprint TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
        """)
        if "absolute_expires_at" not in {
            row["name"] for row in self.store.db.execute("PRAGMA table_info(sessions)")
        }:
            self.store.db.execute("ALTER TABLE sessions ADD COLUMN absolute_expires_at INTEGER")

    def issue_invitation(self, now: int) -> str:
        """AWS administrator-only operation; never a public first-visitor endpoint."""
        token = secrets.token_urlsafe(32)
        self.install_invitation_hash(digest(token), now, 600)
        return token

    def install_invitation_hash(self, token_hash: str, now: int, ttl: int = 3600):
        """Trusted local administration: provision a locally generated code by hash only."""
        if (
            len(token_hash) != 64
            or any(c not in "0123456789abcdef" for c in token_hash)
            or type(ttl) is not int
            or not 60 <= ttl <= 3600
        ):
            raise ValueError("invalid_invitation_parameters")
        with self.store.transaction():
            if self.store.db.execute("SELECT 1 FROM owner").fetchone():
                raise AuthenticationError("already_bound")
            self.store.db.execute("DELETE FROM invitations")
            self.store.db.execute(
                "INSERT INTO invitations VALUES (?, ?, 0)", (token_hash, now + ttl)
            )

    def bind(self, invitation: str, raw_init_data: str, now: int) -> str:
        identity = self.verifier.verify(raw_init_data, now)
        with self.store.transaction():
            if self.store.db.execute("SELECT 1 FROM owner").fetchone():
                raise AuthenticationError("already_bound")
            invitation_row = self.store.db.execute(
                "SELECT * FROM invitations WHERE token_hash=?", (digest(invitation),)
            ).fetchone()
            if not invitation_row or invitation_row["used"] or invitation_row["expires_at"] <= now:
                raise AuthenticationError("invalid_invitation")
            self._consume_identity(identity)
            self.store.db.execute("INSERT INTO owner VALUES (1, ?)", (identity.user_id,))
            self.store.db.execute("UPDATE invitations SET used=1")
            return self._session(identity.user_id, now)

    def login(self, raw_init_data: str, now: int) -> str:
        identity = self.verifier.verify(raw_init_data, now)
        with self.store.transaction():
            owner = self.store.db.execute("SELECT telegram_id FROM owner").fetchone()
            if not owner:
                raise AuthenticationError("binding_required")
            if owner[0] != identity.user_id:
                raise AuthenticationError("owner_mismatch")
            self._consume_identity(identity)
            return self._session(identity.user_id, now)

    def _consume_identity(self, identity):
        if self.store.db.execute(
            "SELECT 1 FROM auth_replays WHERE fingerprint=?", (identity.fingerprint,)
        ).fetchone():
            raise AuthenticationError("authentication_replayed")
        self.store.db.execute("INSERT INTO auth_replays VALUES (?)", (identity.fingerprint,))

    def _session(self, user_id: int, now: int) -> str:
        token = secrets.token_urlsafe(32)
        self.store.db.execute(
            "INSERT INTO sessions (token_hash,user_id,expires_at,absolute_expires_at) "
            "VALUES (?, ?, ?, ?)",
            (digest(token), user_id, now + SESSION_TTL, now + SESSION_ABSOLUTE_TTL),
        )
        return token

    def authorize(self, token: str, now: int) -> int:
        row = self.store.db.execute(
            "SELECT s.user_id FROM sessions s JOIN owner o ON s.user_id=o.telegram_id "
            "WHERE s.token_hash=? AND s.expires_at>? "
            "AND (s.absolute_expires_at IS NULL OR s.absolute_expires_at>?)",
            (digest(token), now, now),
        ).fetchone()
        if not row:
            raise AuthenticationError("invalid_session")
        return row[0]

    def renew(self, token: str, now: int) -> int:
        """Extend an active session; never revive expired or revoked credentials."""
        with self.store.transaction():
            self.authorize(token, now)
            row = self.store.db.execute(
                "SELECT absolute_expires_at FROM sessions WHERE token_hash=?", (digest(token),)
            ).fetchone()
            absolute = row[0] if row[0] is not None else now + SESSION_ABSOLUTE_TTL
            expires = min(now + SESSION_TTL, absolute)
            self.store.db.execute(
                "UPDATE sessions SET expires_at=?,absolute_expires_at=? WHERE token_hash=?",
                (expires, absolute, digest(token)),
            )
            return expires - now

    def revoke_sessions(self):
        """Trusted administrator operation; authentication required at HTTP boundary."""
        self.store.db.execute("DELETE FROM sessions")
