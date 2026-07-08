"""Tutor tool registry — the pluggable foundation for giving the tutor deterministic
capabilities (exact math now; plots and more later).

A tool = an LLM function schema (advertised to the model) + a handler that runs when
the model calls it. Handlers are SYNC and may block (the sandbox spawns a subprocess);
async callers wrap `run_tool` in a threadpool. All execution of model-generated code
goes through app.sandbox — see its security model.

Design: this module knows nothing about the inference layer. model_client takes the
specs + an executor callback, so inference stays provider-agnostic and decoupled from
the sandbox.
"""

import re
from typing import Any

from app import sandbox

# Result-size guard for what we feed BACK to the model (keeps the next round's input
# bounded). The sandbox already caps raw stdout; this is a second belt.
_MODEL_OUTPUT_CAP = 6000

# Strip host filesystem paths out of anything we feed back to the model — a traceback or
# raw stderr can carry absolute container/venv paths, and the model could relay them to
# the student. The error TYPE and MESSAGE are enough to self-correct; paths are not.
_POSIX_PATH = re.compile(r"(/[\w.\-]+){2,}")
_WIN_PATH = re.compile(r"[A-Za-z]:\\[\w.\-\\]+")
_FILE_FRAME = re.compile(r'^\s*File "[^"]*", ', re.MULTILINE)


def _scrub_paths(text: str) -> str:
    text = _FILE_FRAME.sub('File "<sandbox>", ', text)
    text = _WIN_PATH.sub("<path>", text)
    text = _POSIX_PATH.sub("<path>", text)
    return text


# Fixed, app-authored strings we inject into tool RESULTS the model reads — named so
# the admin prompt-transparency page can show them verbatim (see MODEL_FEEDBACK_NOTES
# at the bottom; add any new one there too). Everything else in a tool result is the
# code's own output/error, path-scrubbed.
FIGURE_NOTE = ("\n[{n} figure(s) rendered and shown to the student. Refer to "
               "them naturally; do not describe SVG.]")
NO_TEXT_NOTE = "(no text output)"
NO_OUTPUT_NOTE = "(ran successfully but produced no output — use print() to show results)"
VERIFY_UNCHECKABLE_NOTE = ("could not check that claim — re-state the operands as plain "
                           "math expressions")
VERIFY_FAILED_NOTE = "the claim did not hold"


def _python_handler(args: dict) -> dict:
    code = args.get("code") if isinstance(args, dict) else None
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "the 'code' argument is required and must be Python source"}
    res = sandbox.run_code(code)
    # Figures (matplotlib SVGs) are server-side: the model gets a NOTE that figures were
    # shown, not the raw SVG (which would bloat context). model_client extracts them.
    figures = res.get("figures") or []
    if res.get("ok"):
        out = (res.get("stdout") or "").strip()
        note = FIGURE_NOTE.format(n=len(figures)) if figures else ""
        body = out[:_MODEL_OUTPUT_CAP] or (NO_TEXT_NOTE if figures else NO_OUTPUT_NOTE)
        return {"ok": True, "output": body + note, "figures": figures}
    # Failure: hand back the exception type + message so the model can self-correct, but
    # NOT the raw traceback (it carries host paths/internals that could leak via the
    # model's reply). Scrub any stray paths from the message too.
    err = (res.get("error") or "execution failed").strip()
    return {"ok": False, "error": _scrub_paths(err)[:_MODEL_OUTPUT_CAP],
            "timeout": bool(res.get("timeout")), "figures": figures}


# ------------------------------- verify tool --------------------------------
# A deterministic answer-checker. The model supplies a STRUCTURED claim (operands as
# strings); the SERVER builds the sympy check (the model never writes the check logic,
# so it can't hand itself a bogus pass) and runs it in the sandbox. This is the
# "confirm the answer before showing it" control — the tutor is told to gate final
# answers on a PASS.
_VERIFY_TYPES = ("equal", "solves", "derivative", "integral", "value")

# Operands are embedded as Python string LITERALS via !r (repr) so a malformed/hostile
# operand can't break out of the string into the code structure. sympify() still EVAL-
# parses them inside the sandbox (contained — and the audit hook blocks process spawning),
# but a model-controlled operand CAN print to stdout — so the PASS/FAIL marker is tagged
# with a per-call random NONCE the model never sees. A forged `print("...PASS")` can't
# reproduce the nonce, so it can't spoof a verified result. No literal { } except the
# escaped set literal {{_exp}} (str.format-safe).
_VERIFY_CODE = '''\
import sympy
from sympy import sympify, simplify, diff, solve, symbols, Eq
var = symbols({var!r})
ct = {ct!r}
N = {nonce!r}
try:
    if ct == "equal":
        ok = simplify(sympify({expr!r}) - sympify({expected!r})) == 0
    elif ct == "derivative":
        ok = simplify(diff(sympify({expr!r}), var) - sympify({expected!r})) == 0
    elif ct == "integral":
        ok = simplify(diff(sympify({expected!r}), var) - sympify({expr!r})) == 0
    elif ct == "value":
        ok = simplify(sympify({expr!r}).subs(var, sympify({point!r})) - sympify({expected!r})) == 0
    elif ct == "solves":
        _eq = {equation!r}
        if "=" in _eq:
            _l, _r = _eq.split("=", 1)
            _e = Eq(sympify(_l), sympify(_r))
        else:
            _e = sympify(_eq)
        _got = set(solve(_e, var))
        _exp = sympify({expected!r})
        _exp = set(_exp) if hasattr(_exp, "__iter__") else {{_exp}}
        ok = _got == _exp
        if not ok:
            print("DETAIL solver got:", sorted(_got, key=str))
    else:
        ok = None
    print("VERIFY", N, "PASS" if ok else ("FAIL" if ok is not None else "UNKNOWN"))
except Exception as e:
    print("VERIFY", N, "ERROR", type(e).__name__, str(e)[:200])
'''


def _verify_handler(args: dict) -> dict:
    import secrets
    ct = (args.get("claim_type") or "").strip()
    if ct not in _VERIFY_TYPES:
        return {"ok": False, "verified": False,
                "error": f"claim_type must be one of {', '.join(_VERIFY_TYPES)}"}
    point = str(args.get("point") or "").strip()
    if ct == "value" and not point:
        # Don't default the point (defaulting to 0 would silently 'verify' at x=0).
        return {"ok": False, "verified": False,
                "error": "claim_type 'value' requires 'point' (the value to substitute for the variable)"}
    nonce = secrets.token_hex(8)
    fields = {
        "var": (args.get("variable") or "x").strip() or "x",
        "ct": ct,
        "nonce": nonce,
        "expr": str(args.get("expr") or ""),
        "expected": str(args.get("expected") or ""),
        "equation": str(args.get("equation") or ""),
        "point": point or "0",
    }
    res = sandbox.run_code(_VERIFY_CODE.format(**fields))
    out = (res.get("stdout") or "")
    if not res.get("ok"):
        return {"ok": False, "verified": False,
                "error": _scrub_paths((res.get("error") or "verification failed"))[:_MODEL_OUTPUT_CAP]}
    # Only a marker carrying THIS call's nonce is trusted — a forged print can't match it.
    if f"VERIFY {nonce} PASS" in out:
        return {"ok": True, "verified": True}
    detail = ""
    for line in out.splitlines():
        if line.startswith("DETAIL") or f"VERIFY {nonce} ERROR" in line:
            # Same posture as the error paths: scrub any path a sympy exception
            # message might carry before it goes back to the model.
            detail = _scrub_paths(line.replace(nonce, "").replace("VERIFY  ERROR", "error:").strip())
            break
    if f"VERIFY {nonce} ERROR" in out:
        return {"ok": True, "verified": False,
                "error": VERIFY_UNCHECKABLE_NOTE,
                "detail": detail[:500]}
    return {"ok": True, "verified": False, "detail": detail[:500] or VERIFY_FAILED_NOTE}


# name -> {spec, handler}
_REGISTRY: dict[str, dict[str, Any]] = {
    "verify": {
        "handler": _verify_handler,
        "spec": {
            "type": "function",
            "function": {
                "name": "verify",
                "description": (
                    "Deterministically CHECK a math result BEFORE you state it to the "
                    "student — the server runs the check exactly, so use it to confirm a "
                    "final answer rather than trusting your own arithmetic. claim_type: "
                    "'equal' (expr == expected), 'solves' (equation's solutions == expected "
                    "list), 'derivative' (d/dvar expr == expected), 'integral' (expected is "
                    "an antiderivative of expr), 'value' (expr at variable=point == "
                    "expected). Give operands as plain math strings, e.g. expr='x**2', "
                    "expected='2*x'. Returns whether it holds; if it does not, fix your "
                    "answer before replying."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "claim_type": {"type": "string", "enum": list(_VERIFY_TYPES)},
                        "expr": {"type": "string", "description": "Main expression (e.g. 'x**2')."},
                        "expected": {"type": "string",
                                     "description": "Candidate result; for 'solves' a list like '[-3, 3]'."},
                        "equation": {"type": "string",
                                     "description": "For 'solves': the equation, e.g. 'x**2 = 9'."},
                        "variable": {"type": "string", "description": "Variable (default 'x')."},
                        "point": {"type": "string", "description": "For 'value': the point to substitute."},
                    },
                    "required": ["claim_type"],
                    "additionalProperties": False,
                },
            },
        },
    },
    "python": {
        "handler": _python_handler,
        "spec": {
            "type": "function",
            "function": {
                "name": "python",
                "description": (
                    "Run Python to compute or VERIFY math exactly — never do nontrivial "
                    "arithmetic, algebra, or calculus in your head. Pre-imported nothing; "
                    "`import` what you need. Available: sympy (exact/symbolic algebra & "
                    "calculus), numpy, and the math module. Use print() to return results "
                    "(only stdout comes back). No network, no file/disk access, a few "
                    "seconds of CPU. Use it to check an equation, solve, differentiate, "
                    "integrate, factor, or confirm a student's answer before responding."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Python source to execute. print() what you want to see.",
                        }
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        },
    },
}


# The recurring coaching strings above, with when each is sent — rendered verbatim by
# the admin prompt-transparency page. One-off validation/failure notices (bad tool
# arguments, timeouts, sandbox refusals, verify's "DETAIL solver got:" line) are
# described there in prose instead. Keep in step with the handlers.
MODEL_FEEDBACK_NOTES: tuple[tuple[str, str], ...] = (
    ("python — the code drew figures", FIGURE_NOTE.format(n="N").strip()),
    ("python — the code printed nothing", NO_OUTPUT_NOTE),
    ("python — figures but no printed text", NO_TEXT_NOTE),
    ("verify — the claim couldn't be checked", VERIFY_UNCHECKABLE_NOTE),
    ("verify — the claim is false (and there is no more specific detail to report)",
     VERIFY_FAILED_NOTE),
)


def tool_specs(names: list[str] | None = None) -> list[dict]:
    """LLM function schemas for the named tools (default: all registered)."""
    names = names if names is not None else list(_REGISTRY)
    return [_REGISTRY[n]["spec"] for n in names if n in _REGISTRY]


def run_tool(name: str, args: dict) -> dict:
    """Execute a tool by name; returns a JSON-serializable result for the model.
    Never raises for ordinary failures — unknown tools and bad args come back as data."""
    entry = _REGISTRY.get(name)
    if entry is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    return entry["handler"](args if isinstance(args, dict) else {})
