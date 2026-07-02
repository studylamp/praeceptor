"""FastAPI application entrypoint.

Mounts middleware, static assets, and the student + admin routers. API docs are
disabled — this is a LAN appliance for kids, not a public API.
"""

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import sandbox
from app.config import BASE_DIR, settings, validate_runtime
from app.db import init_db
from app.routers import admin, student

# Strict CSP: everything from our own origin only. Scripts stay locked to 'self'
# (HTMX + KaTeX are vendored, app.js is external — no inline scripts / eval).
# style-src allows 'unsafe-inline' because KaTeX sets inline styles on the math it
# renders; this loosens STYLES only, not scripts.
_CSP = (
    "default-src 'self'; img-src 'self' data:; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime()  # refuse to boot with missing/default secrets
    init_db()  # schema only — a fresh install starts EMPTY (parent creates data in admin)
    # Sandbox health, surfaced both in the logs and on the admin dashboard. None = tools
    # off (nothing to check); True/False = the boot self-test result. The admin can
    # re-run it live (POST /admin/sandbox/recheck) after fixing the host, no restart.
    app.state.sandbox_ok = None
    app.state.sandbox_msg = ""
    if settings.tools_enabled:
        # Self-test the sandbox so a broken OS wrapper (e.g. blocked user namespaces)
        # surfaces at boot with a fix, not as a cryptic error on the first tool call.
        ok, msg = sandbox.preflight()
        app.state.sandbox_ok, app.state.sandbox_msg = ok, msg
        # flush=True so this single line reaches `docker compose logs` immediately —
        # stderr is block-buffered when Docker captures it through a pipe.
        print(f"{'INFO' if ok else 'WARNING'} [praeceptor]: {msg}", file=sys.stderr, flush=True)
    yield


app = FastAPI(
    title="Praeceptor",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret,
    same_site="lax",
    https_only=settings.session_https_only,  # config-driven; default off for LAN HTTP
    max_age=settings.session_max_age,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(student.router)
app.include_router(admin.router)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe. `version` is the baked build string (git describe / "dev")."""
    return JSONResponse({"status": "ok", "app": "praeceptor", "version": settings.app_version})
