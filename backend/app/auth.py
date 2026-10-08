"""Optional password login. Only active when APP_PASSWORD is set. Kept in one module so it
is easy to switch on later (or replace)."""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request

from .errors import AppError

COOKIE = "bnm_session"
TTL_SECONDS = 12 * 3600


def _key(password: str) -> bytes:
    return hashlib.sha256(("bnm-session:" + password).encode()).digest()


def make_token(password: str, now: float | None = None) -> str:
    exp = str(int((now or time.time()) + TTL_SECONDS))
    sig = hmac.new(_key(password), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def token_ok(password: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(_key(password), exp.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        return float(exp) > time.time()
    except ValueError:
        return False


def auth_required(request: Request) -> bool:
    return bool(request.app.state.settings.app_password)


def is_authenticated(request: Request) -> bool:
    pw = request.app.state.settings.app_password
    return (not pw) or token_ok(pw, request.cookies.get(COOKIE))


def require_auth(request: Request) -> None:
    """Dependency for every /api route except login."""
    if not is_authenticated(request):
        raise AppError("UNAUTHORIZED", "Sign in to continue", 401)


def check_password(request: Request, candidate: str) -> bool:
    pw = request.app.state.settings.app_password
    return bool(pw) and hmac.compare_digest(candidate.encode(), pw.encode())
