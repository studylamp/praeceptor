"""Authentication & sessions.

Student and admin identities live under SEPARATE, namespaced session keys so one
flow can never be mistaken for the other (the admin flow is added in Step 5). Every
protected route re-checks its key — presence of a cookie is never trusted on its own.

Includes a small in-memory PIN-attempt lockout for the student login.
"""

import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request, status

from app import models
from app.config import settings

STUDENT_KEY = "student_id"
ADMIN_KEY = "is_admin"  # admin session flag, namespaced apart from STUDENT_KEY

# --- PIN lockout (in-memory; fine for a single-process LAN app) ---
_MAX_FAILS = 5
_LOCK_SECONDS = 60
_fails: dict[int, tuple[int, float]] = {}  # student_id -> (consecutive_fails, locked_until)


def is_locked(student_id: int) -> bool:
    rec = _fails.get(student_id)
    return bool(rec and rec[1] > time.monotonic())


def record_failure(student_id: int) -> None:
    count, _ = _fails.get(student_id, (0, 0.0))
    count += 1
    if count >= _MAX_FAILS:
        _fails[student_id] = (0, time.monotonic() + _LOCK_SECONDS)  # lock + reset counter
    else:
        _fails[student_id] = (count, 0.0)


def clear_failures(student_id: int) -> None:
    _fails.pop(student_id, None)


# --- admin password lockout (single admin → one shared counter) ---
_admin_fail: list = [0, 0.0]  # [consecutive_fails, locked_until]


def admin_is_locked() -> bool:
    return _admin_fail[1] > time.monotonic()


def check_admin_password(password: str) -> bool:
    """Constant-time compare against ADMIN_PASSWORD (no admin table — the env value is
    the single source of truth for the parent/admin password).
    Records failures for the lockout. The caller must check admin_is_locked() first."""
    # Compare as bytes: secrets.compare_digest raises TypeError on non-ASCII str, which
    # would 500 the login on a Unicode password — encoding handles all inputs uniformly.
    ok = bool(settings.admin_password) and secrets.compare_digest(
        (password or "").encode("utf-8"), settings.admin_password.encode("utf-8"))
    if ok:
        _admin_fail[0], _admin_fail[1] = 0, 0.0
    else:
        _admin_fail[0] += 1
        if _admin_fail[0] >= _MAX_FAILS:
            _admin_fail[0], _admin_fail[1] = 0, time.monotonic() + _LOCK_SECONDS
    return ok


# --- session helpers ---
def login_student(request: Request, student_id: int) -> None:
    request.session[STUDENT_KEY] = student_id


def logout_student(request: Request) -> None:
    request.session.pop(STUDENT_KEY, None)


def _redirect_to_login(request: Request) -> HTTPException:
    # For an htmx request, swapping login HTML into the page is wrong — send an
    # HX-Redirect header so the browser does a real client-side navigation instead.
    if request.headers.get("HX-Request"):
        return HTTPException(status_code=status.HTTP_200_OK, headers={"HX-Redirect": "/login"})
    return HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})


def current_student(request: Request):
    """FastAPI dependency: returns the logged-in student row, or redirects to /login."""
    sid: Optional[int] = request.session.get(STUDENT_KEY)
    student = models.get_student(sid) if sid else None
    if student is None:  # not logged in, or account removed since login
        request.session.pop(STUDENT_KEY, None)
        raise _redirect_to_login(request)
    return student


# --- admin session ---
def login_admin(request: Request) -> None:
    request.session[ADMIN_KEY] = True


def logout_admin(request: Request) -> None:
    request.session.pop(ADMIN_KEY, None)


def current_admin(request: Request) -> bool:
    """FastAPI dependency for admin routes: re-checks the admin session on EVERY
    request (a cookie alone is never trusted), redirecting to the admin login if absent.
    Separate key from the student session, so the two flows never cross."""
    if not request.session.get(ADMIN_KEY):
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER,
                            headers={"Location": "/admin/login"})
    return True
