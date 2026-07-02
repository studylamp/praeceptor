"""Sandbox child process. Runs as `python -I -B runner.py` with code on stdin.

Standalone by design: imports ONLY the standard library and is launched with `-I`
(isolated mode — no PYTHON* env, no user site) so it can't be hijacked via env or a
stray module on the path. It self-applies resource limits, a network speed-bump, and
an audit-hook speed-bump (deny reads of declared secret paths + block process spawning),
runs the user code, and writes a single JSON result envelope (prefixed with a unique
marker so stray prints from the user code can't be confused for it).

The audit hook and socket patch are SPEED BUMPS, not the boundary — they raise the bar
for casual model-generated code under the default (no-wrapper) config, but a determined
escape (ctypes raw syscalls, etc.) is only closed by the OS-level wrapper / container.
See app/sandbox/__init__.py for the full security model.

This file is NOT imported by the app — it's executed as a subprocess.
"""

import contextlib
import io
import json
import os
import sys

# Stray stdout from user code or C extensions must not be mistaken for our result, so
# the envelope is delimited by this marker and the parent reads only what follows the
# LAST occurrence.
RESULT_MARKER = "<<<PRAECEPTOR_SANDBOX_RESULT>>>"

# Hard ceiling on captured output held in memory in THIS process (the parent applies a
# much smaller cap for the model). Stops `print("x"*10**9)` from ballooning either side.
_OUTPUT_LIMIT = 200_000


class _CappedWriter(io.TextIOBase):
    """A stdout/stderr sink that stops accumulating past a byte budget, so a runaway
    print can't exhaust memory in the child (or flood the parent's pipe read)."""

    def __init__(self, limit: int):
        self._parts: list[str] = []
        self._size = 0
        self._limit = limit
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        room = self._limit - self._size
        if room > 0:
            take = s[:room]
            self._parts.append(take)
            self._size += len(take)
            if len(take) < len(s):
                self.truncated = True
        else:
            self.truncated = True
        return len(s)

    def getvalue(self) -> str:
        out = "".join(self._parts)
        if self.truncated:
            out += "\n…[output truncated]"
        return out


def _apply_rlimits(limits: dict) -> None:
    """POSIX resource limits: bound CPU, memory, processes, file size, fds, and core
    dumps so a runaway can't exhaust the host. No-op on platforms without `resource`
    (e.g. Windows dev) — there the wall-clock timeout in the parent is the only bound,
    which is fine for a trusted dev box (production is the Linux container)."""
    try:
        import resource
    except Exception:
        return

    def _set(what, soft):
        try:
            hard = resource.getrlimit(what)[1]
            cap = soft if hard == resource.RLIM_INFINITY else min(soft, hard)
            resource.setrlimit(what, (cap, cap))
        except Exception:
            pass

    cpu = int(limits.get("cpu", 6))
    mem = int(limits.get("mem_mb", 1024)) * 1024 * 1024
    fsize = int(limits.get("fsize_mb", 16)) * 1024 * 1024
    nofile = int(limits.get("nofile", 64))
    nproc = int(limits.get("nproc", 64))
    _set(resource.RLIMIT_CPU, cpu)
    # Address space bounds total memory; matplotlib/numpy/sympy need headroom.
    if hasattr(resource, "RLIMIT_AS"):
        _set(resource.RLIMIT_AS, mem)
    _set(resource.RLIMIT_FSIZE, fsize)
    _set(resource.RLIMIT_NOFILE, nofile)
    # Cap processes/threads to blunt fork bombs. Per-UID, so it's a blunt instrument —
    # the wrapper running as a dedicated UID / PID-cgroup is the real control.
    if hasattr(resource, "RLIMIT_NPROC"):
        _set(resource.RLIMIT_NPROC, nproc)
    if hasattr(resource, "RLIMIT_CORE"):
        _set(resource.RLIMIT_CORE, 0)


def _block_network() -> None:
    """Disable outbound sockets from the Python `socket` wrapper. SPEED BUMP only (the
    C `_socket` module / ctypes can bypass it) — real isolation is the wrapper/netns."""
    try:
        import socket

        def _blocked(*_a, **_k):
            raise OSError("network access is disabled in the sandbox")

        socket.socket = _blocked            # type: ignore[assignment]
        socket.create_connection = _blocked  # type: ignore[assignment]
        if hasattr(socket, "create_server"):
            socket.create_server = _blocked  # type: ignore[assignment]
    except Exception:
        pass


def _install_audit_hook(deny_paths: list[str]) -> None:
    """Audit-hook SPEED BUMP: deny opening the host's secret paths (.env, the DB/data
    dir) and block process spawning / network connects from ordinary stdlib calls.
    Defense-in-depth for the default no-wrapper config — NOT a boundary (raw ctypes
    syscalls bypass audit hooks). The wrapper's filesystem/PID/net namespaces are."""
    deny = []
    for d in deny_paths:
        try:
            deny.append(os.path.realpath(d))
        except Exception:
            pass
    deny_t = tuple(deny)

    # Block process spawning. (Not winreg — reading the registry holds none of our
    # secrets and matplotlib's Windows font lookup needs it; irrelevant on Linux/prod.)
    _SPAWN = ("os.system", "subprocess.Popen", "os.exec", "os.spawn", "os.posix_spawn",
              "pty.spawn")
    _NET = ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
            "socket.sethostname")

    def _hook(event, args):
        if event.startswith(_SPAWN):
            raise PermissionError(f"'{event}' is disabled in the sandbox")
        if event in _NET:
            raise PermissionError("network access is disabled in the sandbox")
        if deny_t and event in ("open", "os.open"):
            target = args[0] if args else None
            if isinstance(target, str):
                try:
                    # realpath (not abspath) so a symlink to a denied file is resolved
                    # to its real target before the prefix check — closes the obvious
                    # `ln -s .env x; open(x)` bypass of this speed bump.
                    p = os.path.realpath(target)
                except Exception:
                    return
                for d in deny_t:
                    if p == d or p.startswith(d + os.sep):
                        raise PermissionError("access to that path is disabled in the sandbox")

    try:
        sys.addaudithook(_hook)
    except Exception:
        pass


# Plot capture bounds: a tutoring answer shouldn't emit a wall of figures, and a single
# huge SVG would bloat the reply/context.
_MAX_FIGURES = 6
_MAX_FIGURE_BYTES = 400_000


def _capture_figures() -> list[str]:
    """If the user's code used matplotlib (pyplot), render each open figure to a vector
    SVG string. Returns [] when matplotlib was never imported (so the import cost is
    only paid when actually plotting). The Agg backend (env MPLBACKEND=Agg) keeps it
    headless. Errors here never fail the run — figures are best-effort."""
    if "matplotlib" not in sys.modules:
        return []
    figures: list[str] = []
    try:
        import matplotlib.pyplot as plt
        for num in plt.get_fignums():
            if len(figures) >= _MAX_FIGURES:
                break
            buf = io.StringIO()
            try:
                plt.figure(num).savefig(buf, format="svg", bbox_inches="tight")
            except Exception:
                continue
            svg = buf.getvalue()
            if svg and len(svg) <= _MAX_FIGURE_BYTES:
                figures.append(svg)
        plt.close("all")
    except Exception:
        return figures
    return figures


def main() -> int:
    try:
        limits = json.loads(os.environ.get("SANDBOX_LIMITS", "{}"))
        if not isinstance(limits, dict):
            limits = {}
    except Exception:
        limits = {}
    try:
        deny_paths = json.loads(os.environ.get("SANDBOX_DENY", "[]"))
        if not isinstance(deny_paths, list):
            deny_paths = []
    except Exception:
        deny_paths = []

    _apply_rlimits(limits)
    _block_network()
    _install_audit_hook(deny_paths)

    code = sys.stdin.read()

    real_stdout = sys.stdout
    captured = _CappedWriter(_OUTPUT_LIMIT)
    error = None
    tb = None
    try:
        compiled = compile(code, "<sandbox>", "exec")
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            # NOTE: this globals dict is NOT a security boundary (escapable). Isolation
            # is provided by the separate process + scrubbed env + rlimits + wrapper.
            exec(compiled, {"__name__": "__main__"})
    except SystemExit:
        pass
    except BaseException as exc:  # noqa: BLE001 — report any failure as data, never crash
        import traceback
        error = f"{type(exc).__name__}: {exc}"
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=6))

    envelope = {
        "ok": error is None,
        "stdout": captured.getvalue(),
        "error": error,
        "traceback": tb,
        "figures": _capture_figures(),
    }
    real_stdout.write(RESULT_MARKER)
    real_stdout.write(json.dumps(envelope))
    real_stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
