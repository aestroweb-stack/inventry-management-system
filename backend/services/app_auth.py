"""Small same-origin operator login for Railway-hosted inventory APIs.

No browser API keys; signed, HttpOnly, SameSite cookies. The deployment is
single-operator unless an external identity provider/reverse proxy is added.
"""
from __future__ import annotations
import hashlib
import hmac
import os
import time
from collections import defaultdict, deque
from threading import Lock
from urllib.parse import urlsplit

COOKIE = "myraa_session"
SESSION_AGE = 12 * 3600


def required() -> bool:
    return bool(os.getenv("APP_PASSWORD", ""))


def deployment_missing_password() -> bool:
    return bool(os.getenv("RAILWAY_ENVIRONMENT") and not required()
                and os.getenv("MYRAA_REQUIRE_AUTH", "1") != "0")


def _secret() -> bytes:
    return (os.getenv("APP_SESSION_SECRET") or os.getenv("APP_PASSWORD") or "nonproduction-local").encode()


def _signature(exp: int) -> str:
    return hmac.new(_secret(), str(exp).encode(), hashlib.sha256).hexdigest()


def create_session() -> str:
    expires = int(time.time()) + SESSION_AGE
    return f"{expires}.{_signature(expires)}"


def verify_session(token: str) -> bool:
    try:
        value, sig = token.split(".", 1)
        expiry = int(value)
        if expiry < int(time.time()) or expiry > int(time.time()) + SESSION_AGE + 60:
            return False
        return hmac.compare_digest(sig, _signature(expiry))
    except (ValueError, AttributeError):
        return False


def verify_password(password: str) -> bool:
    configured = os.getenv("APP_PASSWORD", "")
    return bool(configured and hmac.compare_digest(password.encode(), configured.encode()))


def origin_allowed(request) -> bool:
    """Require same-origin for state-changing cookie-auth API requests."""
    origin = request.headers.get("origin", "")
    if not origin:
        return True  # Non-browser clients must still provide cookie.
    parsed = urlsplit(origin)
    host = request.headers.get("host", "").split(":")[0].lower()
    return parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() == host


_failed_logins: dict[str, deque[float]] = defaultdict(deque)
_login_lock = Lock()


def login_allowed(client: str) -> bool:
    """Limit credential guessing (10 failed attempts per 15 minutes per IP)."""
    now = time.monotonic()
    with _login_lock:
        attempts = _failed_logins[client]
        while attempts and now - attempts[0] > 900:
            attempts.popleft()
        return len(attempts) < 10


def record_failed_login(client: str) -> None:
    with _login_lock:
        _failed_logins[client].append(time.monotonic())
        if len(_failed_logins) > 2000:
            for key in list(_failed_logins)[:500]:
                _failed_logins.pop(key, None)


def clear_failed_logins(client: str) -> None:
    with _login_lock:
        _failed_logins.pop(client, None)
