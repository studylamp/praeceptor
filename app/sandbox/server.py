"""Sandbox worker process (`python -m app.sandbox.server`).

Runs in a DEDICATED, network-isolated container (compose `network_mode: none`, no
secrets, no DB mount, non-root). It listens on a Unix-domain socket on a volume shared
only with the app, receives `{code, timeout, mem_mb, stdout_cap}` requests, executes each
via the same local runner the app uses (`executor._run_code_local` → a separate
`python -I -B runner.py` subprocess with rlimits + scrubbed env), and returns the result
dict. See app/sandbox/__init__.py for how this replaces the in-app OS wrapper: here the
CONTAINER is the isolation boundary, so no bubblewrap/user-namespaces are needed and the
app keeps its own seccomp/apparmor guardrails.
"""

import os
import socket
import sys
import threading

from app.config import settings
from app.sandbox.executor import _run_code_local
from app.sandbox.protocol import recv_frame, send_frame


def _err(msg: str) -> dict:
    return {"ok": False, "stdout": "", "error": msg, "traceback": None,
            "timeout": False, "figures": []}


def _clamp_timeout(v):
    """Never let a frame request a longer wall-clock than the configured max (a
    same-uid escaped process could otherwise pin a run slot). None → the default."""
    if v is None:
        return None
    try:
        return max(0.1, min(float(v), float(settings.sandbox_timeout)))
    except (TypeError, ValueError):
        return None


def _clamp_mem(v):
    if v is None:
        return None
    try:
        return max(64, min(int(v), int(settings.sandbox_mem_mb)))
    except (TypeError, ValueError):
        return None


def _clamp_cap(v):
    try:
        return max(0, min(int(v), 200_000))
    except (TypeError, ValueError):
        return 8000


def _handle(conn: socket.socket, sem: threading.BoundedSemaphore,
            conn_sem: threading.BoundedSemaphore) -> None:
    try:
        # Bound each blocking recv/send so a slow or half-open peer can't park this thread
        # forever (slow-loris / blocked-send). Execution happens between recv and send with
        # no socket op in flight, so this only needs to cover the small request read + the
        # result write — 120s is generous. Timeout → exception → thread exits below.
        conn.settimeout(120.0)
        req = recv_frame(conn)
        if not isinstance(req, dict) or "code" not in req:
            send_frame(conn, _err("sandbox worker: malformed request"))
            return
        # Clamp caller-supplied limits to the configured maxima — don't trust the frame
        # (only reachable by the legitimate app, or a same-uid escaped process; the latter
        # must not be able to request an oversized/long-running job).
        with sem:  # bound concurrent EXECUTIONS (fork-storm guard); recv is outside the gate
            res = _run_code_local(
                req.get("code", ""),
                timeout=_clamp_timeout(req.get("timeout")),
                mem_mb=_clamp_mem(req.get("mem_mb")),
                stdout_cap=_clamp_cap(req.get("stdout_cap", 8000)),
            )
        send_frame(conn, res)
    except Exception as e:  # never let one bad connection take down the worker
        try:
            send_frame(conn, _err(f"sandbox worker error: {e}"))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn_sem.release()  # free the connection slot (paired with the acquire in serve())


def serve(path: str, max_concurrent: int) -> None:
    # Fresh socket each start (a stale file from a crash would block bind()).
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    # Only the same uid (both containers run as the image's appuser) may connect.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    srv.listen(64)
    sem = threading.BoundedSemaphore(max_concurrent)
    # Bound TOTAL in-flight connections/threads, not just executions: a flood of slow or
    # half-open connections would otherwise spawn an unbounded number of handler threads
    # (each blocking up to 120s in recv). Acquiring before we spawn provides backpressure —
    # excess connections wait in the listen backlog, then the kernel refuses them.
    conn_sem = threading.BoundedSemaphore(max(8, max_concurrent * 4))
    print(f"[praeceptor-sandbox] listening on {path} (max_concurrent={max_concurrent})",
          file=sys.stderr, flush=True)
    while True:
        conn, _ = srv.accept()
        conn_sem.acquire()
        threading.Thread(target=_handle, args=(conn, sem, conn_sem), daemon=True).start()


def main() -> None:
    path = os.getenv("SANDBOX_SERVER", "/ipc/sandbox.sock")
    max_concurrent = int(os.getenv("SANDBOX_MAX_CONCURRENT", "4"))
    serve(path, max_concurrent)


if __name__ == "__main__":
    main()
