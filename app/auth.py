"""Login: scrypt password hashes, server-side sessions (cookie holds a random token), failed-login throttling.

Standard library only. Every user can see and drive every task (this is a single-tenant tool), so an account is
full access to the machine through Codex; there are no roles.
"""
import base64
import binascii
import hashlib
import hmac
import secrets
import time
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Request

from .database import Database
from .models import timestamp

COOKIE_NAME = "codex_gui_session"
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256  # scrypt on a megabyte-long "password" is a cheap way to burn CPU
MAX_USERNAME_LENGTH = 64

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


class AuthError(Exception):
    """Bad credentials / weak password / unknown user. The message is safe to show."""


class AuthRequired(Exception):
    """Raised by require_login; main.py turns it into a 401 (API) or a redirect to /login (pages)."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=n, r=r, p=p,
                                dklen=len(expected), maxmem=128 * n * r * 2 + (1 << 20))
    except (ValueError, binascii.Error, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the username does not exist, so "no such user" and "wrong password" take the same time.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def validate_username(username: str) -> str:
    username = username.strip()
    if not username or len(username) > MAX_USERNAME_LENGTH or any(c.isspace() or ord(c) < 32 for c in username):
        raise AuthError(f"username must be 1-{MAX_USERNAME_LENGTH} characters without spaces")
    return username


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")


def safe_next(target: Optional[str]) -> str:
    """Where to go after login: only a path on this site (never another host, never a //host or backslash trick)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or parts.path in ("/login", "/logout"):
        return "/"
    return target


class LoginThrottle:
    """Failed-login limiter, in memory: `limit` failures within `window` seconds locks that key out for the rest of the
    window. Keyed by client address + username (stops guessing one account) and by address alone (stops spraying)."""

    def __init__(self, limit: int = 5, window: float = 900.0, ip_limit: int = 30, clock=time.monotonic):
        self.limit, self.window, self.ip_limit, self._clock = limit, window, ip_limit, clock
        self._failures: dict[tuple, list[float]] = {}

    def _recent(self, key: tuple) -> list[float]:
        cutoff = self._clock() - self.window
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def retry_after(self, ip: str, username: str) -> int:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        wait = 0.0
        for key, limit in (((ip, username.lower()), self.limit), ((ip,), self.ip_limit)):
            recent = self._recent(key)
            if len(recent) >= limit:
                wait = max(wait, recent[0] + self.window - self._clock())
        return int(wait) + 1 if wait > 0 else 0

    def failed(self, ip: str, username: str) -> None:
        for key in ((ip, username.lower()), (ip,)):
            self._failures.setdefault(key, []).append(self._clock())
        if len(self._failures) > 10000:  # bounded: drop everything that has aged out
            for key in list(self._failures):
                self._recent(key)

    def succeeded(self, ip: str, username: str) -> None:
        self._failures.pop((ip, username.lower()), None)


class AuthService:
    def __init__(self, db: Database, session_hours: float = 168.0):
        self.db = db
        self.session_seconds = session_hours * 3600
        self.throttle = LoginThrottle()

    # ----- accounts -----

    def create_user(self, username: str, password: str) -> dict:
        username = validate_username(username)
        validate_password(password)
        try:
            return self.db.create_user(username, hash_password(password), timestamp())
        except ValueError as e:
            raise AuthError(str(e))

    def set_password(self, username: str, password: str) -> None:
        validate_password(password)
        user = self.db.get_user_by_name(username)
        if not user:
            raise AuthError(f"no such user: {username}")
        self.db.set_password_hash(user["id"], hash_password(password))
        self.db.delete_user_sessions(user["id"])  # a changed password signs every browser out

    def change_password(self, user_id: int, current: str, new: str, keep_token: Optional[str]) -> None:
        user = self.db.get_user(user_id)
        if not user or not verify_password(current, user["password_hash"]):
            raise AuthError("current password is incorrect")
        validate_password(new)
        self.db.set_password_hash(user_id, hash_password(new))
        self.db.delete_user_sessions(user_id, keep_token_hash=_token_hash(keep_token) if keep_token else None)

    def authenticate(self, username: str, password: str) -> Optional[dict]:
        user = self.db.get_user_by_name(username.strip()) if len(username) <= MAX_USERNAME_LENGTH * 2 else None
        ok = verify_password(password[:MAX_PASSWORD_LENGTH + 1], user["password_hash"] if user else _DUMMY_HASH)
        return user if user and ok else None

    # ----- sessions -----

    def start_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self.db.purge_sessions(timestamp())
        self.db.add_session(_token_hash(token), user_id, timestamp(), timestamp(self.session_seconds))
        return token

    def user_for_token(self, token: Optional[str]) -> Optional[dict]:
        return self.db.get_session_user(_token_hash(token), timestamp()) if token else None

    def end_session(self, token: Optional[str]) -> None:
        if token:
            self.db.delete_session(_token_hash(token))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def require_login(request: Request) -> Optional[dict]:
    """Dependency for every page and API route except the login page. Disabled auth = open (loopback-only use)."""
    settings = request.app.state.settings
    if not settings.auth_enabled:
        request.state.user = None
        return None
    user = request.app.state.auth.user_for_token(request.cookies.get(COOKIE_NAME))
    if not user:
        raise AuthRequired()
    request.state.user = user
    return user
