"""Sandboxed execution of untrusted, model-generated Python.

SECURITY MODEL (read before changing anything here):

The tutor can run Python to compute/verify math and (later) draw plots. That code is
shaped by model output, which is in turn shaped by student input — so it is UNTRUSTED.
The security boundary is therefore deliberately NOT in-process: we never `exec()` model
code in the app process with "restricted" builtins (that is trivially escapable and is
the classic mistake that closes the door on ever exposing this publicly). Instead:

  1. Code runs in a SEPARATE Python process (`python -I -B runner.py`, code on stdin —
     never argv/disk), so an escape is contained to a throwaway process.
  2. The child's environment is built from scratch — NO secrets in its ENV (no API
     keys, admin password, app secret, DB path). This is fully enforceable from pure
     Python: code can't read an env var that was never passed in. NOTE the scope: this
     protects secrets in the PARENT'S MEMORY/ENV, NOT secrets on DISK. The `.env` file
     and the SQLite DB sit at fixed absolute paths and `open()` takes absolute paths, so
     disk confinement depends on layer (4) (and the layer-3 audit-hook speed bump).
  3. POSIX resource limits (CPU, address space, processes, file size, open files, core)
     bound a runaway; a wall-clock timeout + process-group kill is the backstop. Plus
     two SPEED BUMPS (not boundaries): the socket monkeypatch and an audit hook that
     denies reads of declared secret paths (.env, data dir) and blocks process spawning.
     Both are bypassable by determined code (raw `_socket`/ctypes syscalls); they raise
     the bar for casual model output under the default config, nothing more.
  4. The OS-level isolation boundary that closes the residual gaps in (1)–(3) — on-disk
     secret reads via raw syscalls, network egress, a `setsid` grandchild escaping the
     process-group kill, fork-bomb pressure. There are TWO interchangeable ways to get it,
     both leaving this interface identical:

     (4a) DEFAULT for Docker — a SEPARATE `sandbox` CONTAINER (`SANDBOX_SERVER`). The app
          proxies each job over a Unix-domain socket to a worker running THIS code in its
          own container with `network_mode: none` (no network at all), NO secrets and NO
          DB mount (nothing on disk to steal), non-root. Docker itself is the boundary, so
          no user namespaces / seccomp / apparmor changes are needed and the app keeps its
          own guardrails. See `server.py` + docker-compose.yml. The worker still runs each
          job through layers (1)–(3) as defence-in-depth. Residual (accepted): the worker's
          own socket sits at `/ipc/sandbox.sock` inside the worker, so model code that
          escaped the runner (raw syscalls, bypassing the socket/audit speed bumps) and runs
          as the same uid could connect to it and self-schedule more sandboxed jobs — but it
          still has NO network, NO secrets and NO DB, so the blast radius stays inside the
          already-untrusted worker. Fully closing it would need the runner to run under a
          different uid than the worker (needs a root→drop worker); not worth reintroducing
          root for a single-family LAN box. The socket path is on the runner deny-list and
          `_handle` bounds slow/partial frames.

     (4b) BARE-METAL alternative — a local OUTER wrapper (`SANDBOX_WRAPPER`, e.g.
          `nsjail`/`firejail`/`bwrap`) prepended to the runner command, giving network/FS
          confinement in a single process. Used only when `SANDBOX_SERVER` is empty. (Note:
          unprivileged `bwrap` inside Docker needs host userns + seccomp/apparmor relaxation,
          which is exactly why 4a is the Docker default.)

Layers (1)+(2)+(3) make this safe enough for a single-family LAN appliance on a loopback
bind (validate_runtime warns when neither 4a nor 4b is configured, and REFUSES to boot if
tools are enabled on a non-loopback bind with no isolation). Adding (4a)/(4b) is what makes
it safe for an untrusted/public deployment — a config choice, not a rewrite.

Public API: `run_code(code) -> dict` and `preflight() -> (ok, message)`. `run_code` proxies
to the worker when `SANDBOX_SERVER` is set, else runs locally. See `executor.py`, `server.py`.
"""

from app.sandbox.executor import preflight, run_code  # noqa: F401
