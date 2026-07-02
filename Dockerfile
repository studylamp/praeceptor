# Praeceptor container image.
# Base images are tag-pinned (not digest-pinned) on purpose: for a single-family LAN
# appliance that's the accepted residual — reproducible enough, and it lets routine
# `slim` security patches land on rebuild without a manual digest bump.
FROM python:3.12-slim

# System packages:
#  - bubblewrap: the OS-level sandbox wrapper for the tutor's compute tools (see the
#    SANDBOX_WRAPPER default below and app/sandbox/__init__.py). This is what gives the
#    untrusted, model-generated code no network + a confined filesystem.
#  - ca-certificates: TLS trust store for outbound calls to the model provider.
# Pin nothing else in; keep the image lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs. Pinned (not :latest) so builds are reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /bin/uv

# Build the venv against the base image's own CPython 3.12 (never a uv-managed download),
# so /app/.venv references an interpreter that's guaranteed to exist in the final image.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies first (cached unless the lockfile changes).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

# Then the application code.
COPY app ./app
COPY static ./static

# SQLite lives on a host-mounted volume in production; declare its path in-image so the
# app and the sandbox deny-list agree on it without relying on the compose env.
ENV DB_PATH=/app/data/praeceptor.db

# Isolation for the compute tools is provided by the separate, network-isolated `sandbox`
# container (docker-compose sets SANDBOX_SERVER=/ipc/sandbox.sock and runs this same image
# as `python -m app.sandbox.server`). So no in-image OS wrapper is set by default: the
# CONTAINER is the boundary, and the app keeps its own seccomp/apparmor guardrails. The
# optional local SANDBOX_WRAPPER (bubblewrap, installed above) is only for a bare-metal /
# single-container deployment — see app/sandbox/__init__.py and the README.

# Run as an unprivileged user, not root. /app/data (SQLite volume) and /ipc (the shared
# app↔sandbox socket volume) must be writable by it; the app code + venv stay world-
# readable and root-owned (no recursive chown — that would rewrite the whole venv into a
# huge, slow layer). Creating /ipc here so the named volume inherits appuser ownership.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /ipc \
    && chown appuser:appuser /app/data /ipc
USER appuser

# Build provenance: a `git describe --tags --always --dirty` string passed at build time
# (docker-compose build.args / the README update command). Baked into the image env so it
# shows in the admin footer and /healthz. Declared LATE so bumping it per build doesn't
# invalidate the dependency/code layers above. Defaults to "dev" for a plain `docker build`.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

EXPOSE 8000

# Bind 0.0.0.0 INSIDE the container; the LAN-only restriction is enforced by the
# compose port mapping (BIND_ADDR:8000:8000), not here.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
