"""Environment loading and app-wide defaults.

In dev, values come from a local `.env` (gitignored). In the container they come
from docker-compose `env_file`. Model strings are LiteLLM format
(`provider/model`), so the inference provider is pure configuration.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root if present (no-op in the container).
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


@dataclass(frozen=True)
class Settings:
    # Session cookie signing secret. MUST be overridden in production.
    app_secret: str = os.getenv("APP_SECRET", "dev-insecure-change-me")
    # Parent/admin console password.
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    # LAN bind address used by docker-compose (informational inside the app).
    bind_addr: str = os.getenv("BIND_ADDR", "127.0.0.1")
    # SQLite file location (host-mounted volume in production).
    db_path: str = os.getenv("DB_PATH", str(DATA_DIR / "praeceptor.db"))
    # Model defaults (LiteLLM model strings); per-subject tutor model overrides this.
    gate_model: str = os.getenv("GATE_MODEL", "anthropic/claude-haiku-4-5")
    tutor_model_default: str = os.getenv(
        "TUTOR_MODEL_DEFAULT", "anthropic/claude-sonnet-5"
    )
    # Session cookie lifetime (seconds; default 8h ≈ one school day) and whether to
    # mark it Secure (off for LAN HTTP; flip on if you front it with TLS/VPN).
    session_max_age: int = int(os.getenv("SESSION_MAX_AGE", "28800"))
    session_https_only: bool = os.getenv("SESSION_HTTPS_ONLY", "false").lower() in ("1", "true", "yes")
    # Optional public source-code URL. When set, the admin console shows a small
    # "Source on GitHub" footer link; empty (the default) renders no link, so no broken
    # link ships before the repo is public.
    project_url: str = os.getenv("PROJECT_URL", "")
    # Build provenance shown in the admin footer and /healthz. Baked into the image at
    # build time from `git describe --tags --always --dirty` (see docker-compose build.args
    # and the README update command); "dev" for a plain local run / `docker build` with no
    # arg. The commit SHA is the ground truth of what's deployed for a git-pull appliance.
    app_version: str = os.getenv("APP_VERSION", "dev")

    # --- Tool use / sandboxed code execution (see app/sandbox, app/tools) ---
    # Master switch for the tutor's tools (per-subject `tools_enabled` gates each subject
    # on top of this). Off → the tutor behaves exactly as before.
    tools_enabled: bool = os.getenv("TOOLS_ENABLED", "true").lower() in ("1", "true", "yes")
    # Max agentic tool rounds per tutor turn (bounds latency/cost and stops loops).
    tool_max_rounds: int = int(os.getenv("TOOL_MAX_ROUNDS", "5"))
    # Daily caps pre-filled on the new-student form (the cost-control defaults). A parent
    # can change or clear them per student in admin (blank = no cap). The token cap is the
    # real spend control; the message cap guards against spam/loops.
    default_daily_message_cap: int = int(os.getenv("DEFAULT_DAILY_MESSAGE_CAP", "200"))
    default_daily_token_cap: int = int(os.getenv("DEFAULT_DAILY_TOKEN_CAP", "300000"))
    # Sandbox per-call wall-clock timeout (s) and memory ceiling (MB).
    sandbox_timeout: float = float(os.getenv("SANDBOX_TIMEOUT", "8"))
    sandbox_mem_mb: int = int(os.getenv("SANDBOX_MEM_MB", "1024"))
    # Unix-domain socket of the separate sandbox worker container. When set (the Docker
    # default), the app PROXIES tool execution to that network-isolated `sandbox` container
    # over this socket — the container is the isolation boundary, so no in-app OS wrapper is
    # needed and the app keeps its own seccomp/apparmor guardrails. Empty = run code locally
    # in-process-subprocess (bare-metal/dev), where SANDBOX_WRAPPER provides OS isolation.
    sandbox_server: str = os.getenv("SANDBOX_SERVER", "")
    # Max concurrent executions the worker will run at once (fork-storm guard).
    sandbox_max_concurrent: int = int(os.getenv("SANDBOX_MAX_CONCURRENT", "4"))
    # OS-level isolation wrapper prepended to the LOCAL sandbox command (e.g.
    # "nsjail -C /etc/nsjail.cfg --" or "firejail --quiet --net=none --private"). Only used
    # when SANDBOX_SERVER is empty (local execution). Empty = plain subprocess (fine for a
    # trusted LAN/dev box). See app/sandbox.
    sandbox_wrapper: str = os.getenv("SANDBOX_WRAPPER", "")


settings = Settings()

# Values that must never be used in a running deployment.
_INSECURE_SECRETS = {"", "dev-insecure-change-me", "change-me-to-a-long-random-string"}
_INSECURE_PASSWORDS = {"", "change-me"}


def validate_runtime(s: Settings = settings) -> None:
    """Fail closed at startup if security-critical secrets are missing or default.

    Called from the app lifespan (not at import), so tests/scripts that only import
    the data layer aren't affected — but the server refuses to boot misconfigured.
    """
    problems = []
    if s.app_secret in _INSECURE_SECRETS or len(s.app_secret) < 32:
        problems.append(
            'APP_SECRET must be a random string of at least 32 characters '
            '(generate: python -c "import secrets; print(secrets.token_urlsafe(48))").'
        )
    if s.admin_password in _INSECURE_PASSWORDS:
        problems.append("ADMIN_PASSWORD must be set to a non-default value.")
    # Enforce LAN-only rather than just advising it: BIND_ADDR drives the compose port
    # publish, so an all-interfaces value would expose the app on every interface.
    if s.bind_addr.strip() in ("0.0.0.0", "::"):
        problems.append(
            "BIND_ADDR must be a specific LAN IP (or loopback), not 0.0.0.0/:: — that binds "
            "all interfaces and defeats the LAN-only design. Set it to the server's LAN IP."
        )
    # Code-execution tools need an isolation boundary: EITHER the separate sandbox worker
    # container (SANDBOX_SERVER, the Docker default) OR a local OS wrapper (SANDBOX_WRAPPER,
    # bare-metal). With neither, tool code runs with only process+env+rlimit isolation —
    # safe enough on a trusted loopback/LAN box (warn), but refused on a non-loopback bind.
    if s.tools_enabled and not s.sandbox_server.strip() and not s.sandbox_wrapper.strip():
        if not _is_loopback(s.bind_addr):
            problems.append(
                f"TOOLS_ENABLED is on and BIND_ADDR={s.bind_addr!r} is not loopback, but "
                "neither SANDBOX_SERVER nor SANDBOX_WRAPPER is set. Tool code would run with "
                "no OS-level isolation on a non-local bind. Run the separate `sandbox` "
                "container (set SANDBOX_SERVER — the default docker-compose does this) or "
                "set SANDBOX_WRAPPER, or disable TOOLS_ENABLED."
            )
        else:
            import sys as _sys
            print(
                "WARNING [praeceptor]: tools are enabled with neither SANDBOX_SERVER (the "
                "separate sandbox container) nor SANDBOX_WRAPPER — tool code runs with "
                "process+env+rlimit isolation only. Fine for a trusted LAN/dev box; use the "
                "compose `sandbox` service (or set SANDBOX_WRAPPER) before untrusted or "
                "public exposure. See app/sandbox.", file=_sys.stderr,
            )
    if problems:
        raise RuntimeError(
            "Praeceptor refuses to start — fix these in your .env:\n  - " + "\n  - ".join(problems)
        )


def _is_loopback(addr: str) -> bool:
    return (addr or "").strip().lower() in ("127.0.0.1", "localhost", "::1", "")
