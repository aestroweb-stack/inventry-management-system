"""Request-origin validation and authentication attempt throttling.

Passwords and session cookies are handled by workspace_auth.py. No shared
APP_PASSWORD environment variable is accepted.
"""
from __future__ import annotations
import time
from collections import defaultdict, deque
from threading import Lock
from urllib.parse import urlsplit


def origin_allowed(request) -> bool:
    origin = request.headers.get("origin", "")
    if not origin:
        return True
    parsed = urlsplit(origin)
    host = request.headers.get("host", "").split(":")[0].lower()
    return parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() == host

_failed_logins: dict[str, deque[float]] = defaultdict(deque)
_login_lock = Lock()


def login_allowed(client: str) -> bool:
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
