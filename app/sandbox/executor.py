"""Parent side of the sandbox: launch the runner in a separate process, feed it code
on stdin, enforce a wall-clock timeout with a process-group kill, and parse the result.

`run_code` is BLOCKING (subprocess); callers in async code must wrap it in a threadpool.
See app/sandbox/__init__.py for the security model.
"""

import json
import os
import platform
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile

from app.config import BASE_DIR, DATA_DIR, settings
from app.sandbox.runner import RESULT_MARKER

_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner.py")
_IS_WINDOWS = platform.system() == "Windows"


def _deny_paths() -> list[str]:
    """Host paths the child must not read (audit-hook speed bump): the secrets file and
    the database/data directory. The venv/site-packages live elsewhere, so imports are
    unaffected. (The real confinement is the OS-level wrapper; this raises the bar for
    the default config.)"""
    # The .env file and the data directory (default DB location) as prefixes, plus the
    # DB's exact file path in case DB_PATH points elsewhere. Deny the DB as a FILE, not
    # its directory, so a DB_PATH in a broad location can't over-block the work dir.
    paths = [str(BASE_DIR / ".env"), str(DATA_DIR), os.path.abspath(settings.db_path)]
    # In the worker container the app↔worker socket lives on disk at SANDBOX_SERVER; deny it
    # too so tool code can't read/abuse the boundary socket (belt-and-suspenders with the
    # runner's socket-block speed bump). No-op in local mode where sandbox_server is empty.
    if settings.sandbox_server.strip():
        paths.append(os.path.abspath(settings.sandbox_server.strip()))
    out: list[str] = []
    for p in paths:
        ap = os.path.abspath(p)
        if ap not in out:
            out.append(ap)
    return out


def _cap(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated, {len(text) - limit} more chars]"


def _minimal_env(work_home: str, limits: dict) -> dict:
    """A from-scratch environment for the child. Critically excludes every secret the
    app process holds in its ENV (API keys, ADMIN_PASSWORD, APP_SECRET, DB_PATH, …): the
    child inherits NOTHING, so untrusted code can't read a secret that isn't passed in.
    (On-disk secrets like .env are a separate concern — see _deny_paths + the wrapper.)"""
    env = {
        # Fixed minimal PATH rather than the parent's, so the child isn't handed the
        # host's full executable list. (On Windows, keep the inherited PATH so the
        # interpreter can locate its DLLs — dev-only; the Linux container is minimal.)
        "PATH": os.environ.get("PATH", "") if _IS_WINDOWS else "/usr/local/bin:/usr/bin:/bin",
        "SANDBOX_LIMITS": json.dumps(limits),
        "SANDBOX_DENY": json.dumps(_deny_paths()),
        "MPLBACKEND": "Agg",            # headless matplotlib (used in M2)
        "HOME": work_home,
        "TMPDIR": work_home,
        "TEMP": work_home,
        "TMP": work_home,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLCONFIGDIR": work_home,
    }
    # On Windows the child python (and matplotlib's font lookup) need a few non-secret
    # system vars to start; pass them through explicitly. (Linux/prod needs none of this.)
    if _IS_WINDOWS:
        for var in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS", "PATHEXT"):
            if os.environ.get(var):
                env[var] = os.environ[var]
    return env


def run_code(code: str, *, timeout: float | None = None, mem_mb: int | None = None,
             stdout_cap: int = 8000) -> dict:
    """Execute `code` in the sandbox and return a result dict:

        {"ok": bool, "stdout": str, "error": str|None, "traceback": str|None,
         "timeout": bool, "figures": list[str]}

    Two isolation modes, chosen by config (see app/sandbox/__init__.py):
      - SANDBOX_SERVER set → proxy to the dedicated, network-isolated `sandbox` container
        over its Unix-domain socket (the Docker default; the container is the boundary).
      - unset → run locally in a `runner.py` subprocess (bare-metal/dev), with the optional
        SANDBOX_WRAPPER providing OS-level isolation.
    Never raises for ordinary execution/transport failures — they come back as data.
    """
    if settings.sandbox_server.strip():
        return _run_code_remote(code, timeout=timeout, mem_mb=mem_mb, stdout_cap=stdout_cap)
    return _run_code_local(code, timeout=timeout, mem_mb=mem_mb, stdout_cap=stdout_cap)


def _normalize_result(res: dict) -> dict:
    """Fill any missing keys so callers always get the full result shape."""
    res.setdefault("ok", False)
    res.setdefault("stdout", "")
    res.setdefault("error", None)
    res.setdefault("traceback", None)
    res.setdefault("timeout", False)
    if not isinstance(res.get("figures"), list):
        res["figures"] = []
    return res


def _remote_fail(msg: str) -> dict:
    return {"ok": False, "stdout": "", "error": f"Execution failed: {msg}",
            "traceback": None, "timeout": False, "figures": []}


def _run_code_remote(code: str, *, timeout: float | None = None, mem_mb: int | None = None,
                     stdout_cap: int = 8000) -> dict:
    """Send the job to the sandbox worker over its Unix-domain socket and return its
    result. The worker enforces the real execution timeout; the socket timeout here is
    only a transport backstop for a hung/absent worker."""
    from app.sandbox.protocol import recv_frame, send_frame

    path = settings.sandbox_server.strip()
    wall = float(timeout if timeout is not None else settings.sandbox_timeout)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except (AttributeError, OSError) as e:  # AF_UNIX unavailable (shouldn't happen on Linux)
        return _remote_fail(f"AF_UNIX socket unavailable: {e}")
    try:
        sock.settimeout(wall + 15.0)
        sock.connect(path)
        send_frame(sock, {"code": code, "timeout": timeout, "mem_mb": mem_mb,
                          "stdout_cap": stdout_cap})
        res = recv_frame(sock)
    except (OSError, ConnectionError, ValueError) as e:
        return _remote_fail(f"sandbox worker unreachable at {path}: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if not isinstance(res, dict):
        return _remote_fail("malformed response from sandbox worker")
    return _normalize_result(res)


def _run_code_local(code: str, *, timeout: float | None = None, mem_mb: int | None = None,
                    stdout_cap: int = 8000) -> dict:
    """Run `code` in a local `runner.py` subprocess (optionally wrapped by SANDBOX_WRAPPER).
    This is the worker's executor and the bare-metal/dev path. Never raises for ordinary
    execution failures (errors come back as data); only misconfiguration (e.g. an unusable
    wrapper command) would propagate.
    """
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "stdout": "", "error": "no code provided",
                "traceback": None, "timeout": False}

    timeout = float(timeout if timeout is not None else settings.sandbox_timeout)
    mem_mb = int(mem_mb if mem_mb is not None else settings.sandbox_mem_mb)
    # CPU rlimit a touch under the wall clock so a CPU-bound loop is caught by the
    # cheaper signal first; the wall-clock kill is the backstop for sleeps/blocking.
    limits = {"cpu": max(1, int(timeout)), "mem_mb": mem_mb, "fsize_mb": 16,
              "nofile": 64, "nproc": 64}

    wrapper = shlex.split(settings.sandbox_wrapper) if settings.sandbox_wrapper else []
    cmd = [*wrapper, sys.executable, "-I", "-B", _RUNNER]

    work_home = tempfile.mkdtemp(prefix="prae_sbx_")
    env = _minimal_env(work_home, limits)
    popen_kwargs: dict = dict(
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=work_home, env=env, text=True,
    )
    # Put the child in its own process group/session so a timeout kills the whole tree
    # (the runner, the wrapper, and anything they spawned), not just the direct child.
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = None
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            out, err = proc.communicate(input=code, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()  # reap
            return {"ok": False, "stdout": "", "error": f"Execution timed out after {timeout:g}s.",
                    "traceback": None, "timeout": True}
        return _parse_output(out or "", err or "", proc.returncode, stdout_cap)
    finally:
        if proc is not None and proc.poll() is None:
            _kill_tree(proc)
        shutil.rmtree(work_home, ignore_errors=True)


def preflight() -> tuple[bool, str]:
    """Boot-time self-test: run a trivial snippet through the sandbox (wrapper included)
    to confirm tool calls will actually work, and return (ok, message). The common
    failure on a fresh deploy is the OS wrapper being unable to create its namespaces
    (Ubuntu 23.10+ restricts unprivileged user namespaces), which we detect and turn into
    an actionable fix rather than a cryptic per-turn error. Never raises — a preflight
    must not be able to crash startup."""
    try:
        res = run_code("print('sandbox_ok')", timeout=min(8.0, float(settings.sandbox_timeout)))
    except Exception as e:  # unusable wrapper command, etc. — surface, don't crash boot
        return False, f"sandbox preflight could not run: {e}"
    if res.get("ok") and "sandbox_ok" in (res.get("stdout") or ""):
        return True, "sandbox preflight OK — compute tools are functional."
    err = (res.get("error") or "sandbox self-test failed").strip()
    low = err.lower()
    # Keep the "sandbox preflight" prefix on BOTH the OK and FAILED messages so one grep
    # (`grep -i "sandbox preflight"`) surfaces either state in the logs.
    msg = (
        "sandbox preflight FAILED — compute tools are ENABLED but tool calls will not "
        f"work until this is fixed:\n    {err}"
    )
    if "unreachable" in low or "af_unix" in low:
        msg += (
            "\n  The sandbox worker isn't reachable. Is the `sandbox` container running? "
            "Check `docker compose ps` (it should be healthy) and `docker compose logs "
            "sandbox`. The app and sandbox must share the `sandbox_ipc` volume."
        )
    elif "namespace" in low or "bwrap" in low or "permission" in low:
        msg += (
            "\n  This looks like the host blocking unprivileged user namespaces "
            "(default on Ubuntu 23.10+). This only affects the optional in-app "
            "SANDBOX_WRAPPER mode — the default Docker deployment runs a separate "
            "`sandbox` container instead (set SANDBOX_SERVER). Or set TOOLS_ENABLED=false "
            "to run without compute tools. See the README 'Compute-tool sandbox' section."
        )
    return False, msg


def _kill_tree(proc: "subprocess.Popen") -> None:
    try:
        if _IS_WINDOWS:
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _parse_output(out: str, err: str, returncode: int, stdout_cap: int) -> dict:
    idx = out.rfind(RESULT_MARKER)
    if idx == -1:
        # The child died before emitting a result — almost always an rlimit kill
        # (CPU/memory), a segfault, or the wrapper rejecting the command.
        detail = _cap(err.strip(), 1200) or f"sandbox exited with code {returncode} and no result"
        return {"ok": False, "stdout": "", "error": f"Execution failed: {detail}",
                "traceback": None, "timeout": False}
    try:
        env_obj = json.loads(out[idx + len(RESULT_MARKER):])
    except Exception:
        return {"ok": False, "stdout": "", "error": "sandbox produced an unreadable result",
                "traceback": None, "timeout": False}
    env_obj.setdefault("ok", False)
    env_obj.setdefault("error", None)
    env_obj.setdefault("traceback", None)
    env_obj["timeout"] = False
    env_obj["stdout"] = _cap(env_obj.get("stdout") or "", stdout_cap)
    if env_obj.get("traceback"):
        env_obj["traceback"] = _cap(env_obj["traceback"], 2000)
    figs = env_obj.get("figures")
    env_obj["figures"] = [f for f in figs if isinstance(f, str)] if isinstance(figs, list) else []
    return env_obj
