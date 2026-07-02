"""Parent/admin console: login, student & subject CRUD, per-student caps, and the
conversation log viewer (oversight of every chat, including blocked attempts).

Auth: a single password (ADMIN_PASSWORD env, no admin table). Every route below
depends on `auth.current_admin`, which re-checks the admin session on each request;
the admin session key is namespaced apart from the student session. Forms are plain
server-rendered POSTs (Post/Redirect/Get); same-site=lax cookies + `form-action 'self'`
CSP cover CSRF for this LAN tool. All user/tutor text is autoescaped; tutor markdown
goes through the sanitizing `tutor_md` filter — never `| safe`.
"""

import html as _html
import json
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app import auth, model_client, models, pipeline, sandbox, tutor_runtime
from app.config import settings
from app.prompts import build_tutor_system
from app.render import render_tutor_markdown
from app.security import hash_secret, verify_secret
from app.seed import DEFAULT_PIN
from app.subject_presets import PRESET_GROUPS
from app.templating import templates

router = APIRouter(prefix="/admin")

# Model strings offered as quick suggestions in the subject form (free-text though —
# any LiteLLM `provider/model` works).
MODEL_SUGGESTIONS = (
    "anthropic/claude-sonnet-5",
    "anthropic/claude-haiku-4-5",
)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")  # local date, matches the caps basis


def _pin_is_default(student) -> bool:
    return verify_secret(student["pin_hash"], DEFAULT_PIN)


# ------------------------------ form parsing ------------------------------

def _clean_str(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return v or None


def _parse_int(v: Optional[str]) -> Optional[int]:
    v = (v or "").strip()
    return int(v) if v else None  # raises ValueError on garbage → caught by caller


def _parse_cap(v: Optional[str]) -> Optional[int]:
    n = _parse_int(v)
    if n is not None and n < 0:
        raise ValueError("caps cannot be negative")
    return n


def _parse_birthdate(birth_month: str, birth_year: str) -> tuple[Optional[int], Optional[int]]:
    """Parse the birth month (1–12) + year fields. Returns (birth_year, birth_month),
    both None when left blank. Requires both-or-neither so age never half-computes;
    raises ValueError (caught by the caller) on anything out of range."""
    m = _parse_int(birth_month)
    y = _parse_int(birth_year)
    if (m is None) != (y is None):
        raise ValueError("set both birth month and year, or leave both blank")
    if m is not None and not (1 <= m <= 12):
        raise ValueError("birth month out of range")
    if y is not None and not (1900 <= y <= datetime.now().year):
        raise ValueError("birth year out of range")
    return y, m


def _valid_pin(pin: str) -> bool:
    pin = (pin or "").strip()
    return pin.isdigit() and 4 <= len(pin) <= 12


# --------------------------------- auth -----------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request, "admin/login.html", {"error": error})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    if auth.admin_is_locked():
        return templates.TemplateResponse(
            request, "admin/login.html",
            {"error": "Too many tries. Wait a minute and try again."}, status_code=429)
    if not auth.check_admin_password(password):
        return templates.TemplateResponse(
            request, "admin/login.html", {"error": "Wrong password."}, status_code=400)
    auth.login_admin(request)
    return RedirectResponse("/admin", status_code=303)


@router.get("/logout")
def logout(request: Request):
    auth.logout_admin(request)
    return RedirectResponse("/admin/login", status_code=303)


# ------------------------------- dashboard --------------------------------

@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, _: bool = Depends(auth.current_admin)):
    today = _today()
    rows = []
    for st in models.list_students():
        subjects = models.list_subjects(st["id"], active_only=False)
        usage = models.get_usage(st["id"], today)
        rows.append({
            "student": st,
            "subject_count": len(subjects),
            "active_subjects": sum(1 for s in subjects if s["active"]),
            "usage": usage,
            "default_pin": _pin_is_default(st),
        })
    any_default = any(r["default_pin"] for r in rows)
    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        {"rows": rows, "today": today, "any_default_pin": any_default,
         "tools_enabled": settings.tools_enabled,
         "sandbox_ok": getattr(request.app.state, "sandbox_ok", None),
         "sandbox_msg": getattr(request.app.state, "sandbox_msg", "")})


@router.post("/sandbox/recheck")
def sandbox_recheck(request: Request, _: bool = Depends(auth.current_admin)):
    """Re-run the sandbox self-test on demand and cache the fresh result, so a parent can
    confirm a host-side fix (e.g. enabling user namespaces) took effect without restarting
    the container. No-op when tools are globally disabled."""
    if settings.tools_enabled:
        ok, msg = sandbox.preflight()
        request.app.state.sandbox_ok, request.app.state.sandbox_msg = ok, msg
    return RedirectResponse("/admin", status_code=303)


# ------------------------------- settings ---------------------------------
# App-wide parent settings. Currently just the optional global educational/worldview
# framing the tutor honors (shapes presentation only — never the gate, scope, age, or
# safety). Stored in the `settings` k/v table; takes effect on the next message.

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, _: bool = Depends(auth.current_admin)):
    framing = models.get_setting(models.FRAMING_SETTING_KEY) or ""
    return templates.TemplateResponse(
        request, "admin/settings.html", {"framing": framing, "saved": bool(saved)})


@router.post("/settings")
def settings_save(request: Request, educational_framing: str = Form(""),
                  _: bool = Depends(auth.current_admin)):
    models.set_setting(models.FRAMING_SETTING_KEY, _clean_str(educational_framing))
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


# ----------------------------- student CRUD -------------------------------

@router.get("/students/new", response_class=HTMLResponse)
def student_new(request: Request, _: bool = Depends(auth.current_admin)):
    # Pre-fill the recommended daily caps (cost control) so they aren't accidentally left
    # off. The parent can change or clear them. An error re-render (student_create) passes
    # the submitted form instead, so a deliberate change/clear sticks.
    return templates.TemplateResponse(
        request, "admin/student_form.html",
        {"vals": {"daily_message_cap": settings.default_daily_message_cap,
                  "daily_token_cap": settings.default_daily_token_cap},
         "error": None, "creating": True})


@router.post("/students")
def student_create(request: Request, _: bool = Depends(auth.current_admin),
                   name: str = Form(...), birth_month: str = Form(""), birth_year: str = Form(""),
                   pin: str = Form(...),
                   daily_message_cap: str = Form(""), daily_token_cap: str = Form("")):
    vals = {"name": name, "birth_month": birth_month, "birth_year": birth_year,
            "daily_message_cap": daily_message_cap, "daily_token_cap": daily_token_cap}

    def err(msg):
        return templates.TemplateResponse(
            request, "admin/student_form.html",
            {"vals": vals, "error": msg, "creating": True}, status_code=400)

    if not _clean_str(name):
        return err("Name is required.")
    if not _valid_pin(pin):
        return err("PIN must be 4–12 digits.")
    try:
        by, bm = _parse_birthdate(birth_month, birth_year)
        msg_cap = _parse_cap(daily_message_cap)
        tok_cap = _parse_cap(daily_token_cap)
    except ValueError:
        return err("Enter a valid birth month + year (or leave both blank) and whole-number "
                   "caps (≥ 0; blank = no cap).")

    sid = models.create_student(_clean_str(name), by, bm, hash_secret(pin.strip()),
                                daily_message_cap=msg_cap, daily_token_cap=tok_cap)
    return RedirectResponse(f"/admin/students/{sid}", status_code=303)


@router.get("/students/{student_id}", response_class=HTMLResponse)
def student_detail(request: Request, student_id: int, _: bool = Depends(auth.current_admin),
                   error: Optional[str] = None):
    student = models.get_student(student_id)
    if student is None:
        return RedirectResponse("/admin", status_code=303)
    subjects = models.list_subjects(student_id, active_only=False)
    conversations = models.list_conversations(student_id)
    usage = models.get_usage(student_id, _today())
    return templates.TemplateResponse(
        request, "admin/student_detail.html",
        {"student": student, "subjects": subjects, "conversations": conversations,
         "usage": usage, "default_pin": _pin_is_default(student), "error": error})


@router.post("/students/{student_id}")
def student_update(request: Request, student_id: int, _: bool = Depends(auth.current_admin),
                   name: str = Form(...), birth_month: str = Form(""), birth_year: str = Form(""),
                   daily_message_cap: str = Form(""), daily_token_cap: str = Form("")):
    if models.get_student(student_id) is None:
        return RedirectResponse("/admin", status_code=303)
    if not _clean_str(name):
        return _redir_student(student_id, "Name is required.")
    try:
        by, bm = _parse_birthdate(birth_month, birth_year)
        fields = {
            "name": _clean_str(name),
            "birth_year": by,
            "birth_month": bm,
            "daily_message_cap": _parse_cap(daily_message_cap),
            "daily_token_cap": _parse_cap(daily_token_cap),
        }
    except ValueError:
        return _redir_student(student_id, "Enter a valid birth month + year (or leave both "
                              "blank) and whole-number caps (≥ 0).")
    models.update_student(student_id, **fields)
    return RedirectResponse(f"/admin/students/{student_id}", status_code=303)


@router.post("/students/{student_id}/pin")
def student_reset_pin(request: Request, student_id: int, _: bool = Depends(auth.current_admin),
                      pin: str = Form(...)):
    if models.get_student(student_id) is None:
        return RedirectResponse("/admin", status_code=303)
    if not _valid_pin(pin):
        return _redir_student(student_id, "PIN must be 4–12 digits.")
    models.update_student(student_id, pin_hash=hash_secret(pin.strip()))
    return RedirectResponse(f"/admin/students/{student_id}", status_code=303)


@router.get("/students/{student_id}/delete", response_class=HTMLResponse)
def student_delete_confirm(request: Request, student_id: int, _: bool = Depends(auth.current_admin)):
    student = models.get_student(student_id)
    if student is None:
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request, "admin/confirm_delete.html",
        {"title": f"Delete {student['name']}?",
         "warning": "This permanently removes the student and ALL their subjects, "
                    "conversations, and message history. This cannot be undone.",
         "action": f"/admin/students/{student_id}/delete",
         "cancel": f"/admin/students/{student_id}"})


@router.post("/students/{student_id}/delete")
def student_delete(request: Request, student_id: int, _: bool = Depends(auth.current_admin)):
    models.delete_student(student_id)
    return RedirectResponse("/admin", status_code=303)


def _redir_student(student_id: int, error: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"/admin/students/{student_id}?error={quote(error)}", status_code=303)


# ----------------------------- subject CRUD -------------------------------

_SUBJECT_FORM_FIELDS = ("name", "grade_level", "curriculum_name", "style",
                        "answer_policy", "gate_scope", "curriculum_context", "tutor_model")


def _subject_form_response(request: Request, *, student, vals: dict, creating: bool,
                           error: Optional[str] = None, subject=None, status_code: int = 200):
    """Render the shared subject form with the preset menu available."""
    return templates.TemplateResponse(
        request, "admin/subject_form.html",
        {"student": student, "subject": subject, "vals": vals, "creating": creating,
         "error": error, "models": MODEL_SUGGESTIONS, "preset_groups": PRESET_GROUPS},
        status_code=status_code)


def _subject_fields_from_form(form: dict) -> dict:
    """Validated subject column values from submitted form data (raises ValueError)."""
    name = _clean_str(form.get("name"))
    if not name:
        raise ValueError("Subject name is required.")
    tutor_model = _clean_str(form.get("tutor_model")) or settings.tutor_model_default
    return {
        "name": name,
        "grade_level": _clean_str(form.get("grade_level")),
        "curriculum_name": _clean_str(form.get("curriculum_name")),
        "style": _clean_str(form.get("style")),
        "answer_policy": _clean_str(form.get("answer_policy")),
        "gate_scope": _clean_str(form.get("gate_scope")),
        "curriculum_context": _clean_str(form.get("curriculum_context")),
        "tutor_model": tutor_model,
        "tools_enabled": 1 if form.get("tools_enabled") else 0,
        "framing_supplement": _clean_str(form.get("framing_supplement")),
    }


@router.get("/students/{student_id}/subjects/new", response_class=HTMLResponse)
def subject_new(request: Request, student_id: int, _: bool = Depends(auth.current_admin)):
    student = models.get_student(student_id)
    if student is None:
        return RedirectResponse("/admin", status_code=303)
    # New subjects default to computation tools ON (the tutor only actually calls them for
    # math/science; they're inert elsewhere). A parent can uncheck it. On a failed-validation
    # re-render, subject_create passes the submitted form instead, so an explicit uncheck sticks.
    return _subject_form_response(
        request, student=student,
        vals={"tutor_model": settings.tutor_model_default, "tools_enabled": 1}, creating=True)


@router.post("/students/{student_id}/subjects")
async def subject_create(request: Request, student_id: int, _: bool = Depends(auth.current_admin)):
    student = models.get_student(student_id)
    if student is None:
        return RedirectResponse("/admin", status_code=303)
    form = dict(await request.form())
    try:
        fields = _subject_fields_from_form(form)
    except ValueError as e:
        return _subject_form_response(request, student=student, vals=form, creating=True,
                                      error=str(e), status_code=400)
    name = fields.pop("name")
    models.create_subject(student_id, name, **fields)
    return RedirectResponse(f"/admin/students/{student_id}", status_code=303)


@router.get("/subjects/{subject_id}/edit", response_class=HTMLResponse)
def subject_edit(request: Request, subject_id: int, _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    student = models.get_student(subject["student_id"])
    return _subject_form_response(request, student=student, subject=subject,
                                  vals=dict(subject), creating=False)


@router.post("/subjects/{subject_id}")
async def subject_update(request: Request, subject_id: int, _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    student = models.get_student(subject["student_id"])
    form = dict(await request.form())
    try:
        fields = _subject_fields_from_form(form)
    except ValueError as e:
        return _subject_form_response(request, student=student, subject=subject, vals=form,
                                      creating=False, error=str(e), status_code=400)
    fields["active"] = 1 if form.get("active") else 0
    models.update_subject(subject_id, **fields)
    return RedirectResponse(f"/admin/students/{subject['student_id']}", status_code=303)


@router.post("/subjects/{subject_id}/active")
def subject_toggle_active(request: Request, subject_id: int, _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    models.update_subject(subject_id, active=0 if subject["active"] else 1)
    return RedirectResponse(f"/admin/students/{subject['student_id']}", status_code=303)


@router.get("/subjects/{subject_id}/delete", response_class=HTMLResponse)
def subject_delete_confirm(request: Request, subject_id: int, _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request, "admin/confirm_delete.html",
        {"title": f"Delete subject “{subject['name']}”?",
         "warning": "This permanently removes the subject and its conversation history. "
                    "To keep the logs, deactivate it instead.",
         "action": f"/admin/subjects/{subject_id}/delete",
         "cancel": f"/admin/students/{subject['student_id']}"})


@router.post("/subjects/{subject_id}/delete")
def subject_delete(request: Request, subject_id: int, _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    student_id = subject["student_id"]
    models.delete_subject(subject_id)
    return RedirectResponse(f"/admin/students/{student_id}", status_code=303)


@router.get("/subjects/{subject_id}/clear-history", response_class=HTMLResponse)
def subject_clear_history_confirm(request: Request, subject_id: int,
                                  _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request, "admin/confirm_delete.html",
        {"title": f"Clear conversation history for “{subject['name']}”?",
         "warning": "This permanently deletes the student’s chat history for this subject "
                    "so they start with a clean slate — useful at a new chapter or unit. "
                    "The subject and all its settings are kept. This cannot be undone.",
         "confirm_label": "Yes, clear history",
         "action": f"/admin/subjects/{subject_id}/clear-history",
         "cancel": f"/admin/students/{subject['student_id']}"})


@router.post("/subjects/{subject_id}/clear-history")
def subject_clear_history(request: Request, subject_id: int,
                          _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    student_id = subject["student_id"]
    models.clear_subject_conversation(subject_id)
    return RedirectResponse(f"/admin/students/{student_id}", status_code=303)


# ------------------------- curriculum context (fast path) ------------------
# Curriculum context is the one field a parent updates often (weekly/daily), so it
# gets a dedicated hub + a focused single-field editor — no need to open the full
# subject editor each time.

@router.get("/curriculum", response_class=HTMLResponse)
def curriculum_hub(request: Request, _: bool = Depends(auth.current_admin)):
    groups = []
    for st in models.list_students():
        subs = models.list_subjects(st["id"], active_only=False)
        if subs:
            groups.append({"student": st, "subjects": subs})
    return templates.TemplateResponse(request, "admin/curriculum.html", {"groups": groups})


@router.get("/subjects/{subject_id}/context", response_class=HTMLResponse)
def subject_context(request: Request, subject_id: int, saved: int = 0,
                    _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin/curriculum", status_code=303)
    student = models.get_student(subject["student_id"])
    return templates.TemplateResponse(
        request, "admin/subject_context.html",
        {"subject": subject, "student": student, "saved": bool(saved)})


@router.post("/subjects/{subject_id}/context")
def subject_context_save(request: Request, subject_id: int,
                         curriculum_context: str = Form(""),
                         _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin/curriculum", status_code=303)
    models.update_subject(subject_id, curriculum_context=_clean_str(curriculum_context))
    # Stay on the focused editor (with a saved flag) so the parent can keep working.
    return RedirectResponse(f"/admin/subjects/{subject_id}/context?saved=1", status_code=303)


# -------------------------- conversation log viewer ------------------------

@router.get("/conversations", response_class=HTMLResponse)
def conversations(request: Request, _: bool = Depends(auth.current_admin)):
    return templates.TemplateResponse(
        request, "admin/conversations.html", {"conversations": models.list_conversations()})


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation_detail(request: Request, conversation_id: int, _: bool = Depends(auth.current_admin)):
    conv = models.get_conversation(conversation_id)
    if conv is None:
        return RedirectResponse("/admin/conversations", status_code=303)
    messages = models.get_messages(conversation_id, include_blocked=True)
    return templates.TemplateResponse(
        request, "admin/conversation.html", {"conv": conv, "messages": messages})


# ------------------------------- chat test page ----------------------------
# Admin-only sandbox to exercise the tutor's rich output (LaTeX, SVG diagrams)
# WITHOUT the gate, caps, or persistence — for tuning prompts and checking rendering.

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _debug_sse(decision: "pipeline.GateDecision", *, history_turns: int, latency_ms: int,
               model: str, tutor: Optional[dict] = None, reply_chars: Optional[int] = None) -> str:
    """A per-turn diagnostic record for the chat-test page (admin-only; never sent to
    students). Carries the gate verdict and, on an on-subject turn, the tutor's
    finish_reason / token counts / partial flag — enough to explain a short or
    truncated reply. The client renders this via textContent (it holds raw text)."""
    data = {
        "verdict": decision.verdict,
        "suggested_subject": decision.suggested_subject,
        "gate_reason": decision.reason,
        "gate_tokens": decision.gate_tokens,
        "history_turns": history_turns,
        "latency_ms": latency_ms,
        "model": model,
    }
    if tutor is not None:
        data["tutor"] = {
            "finish_reason": tutor.get("finish_reason"),
            "prompt_tokens": tutor.get("prompt_tokens"),
            "completion_tokens": tutor.get("completion_tokens"),
            # total_tokens is the charged amount (cached reads discounted); cache_read /
            # cache_creation show prefix reuse — reads should be non-zero on turns 2+.
            "total_tokens": tutor.get("tokens"),
            "cache_read": tutor.get("cache_read"),
            "cache_creation": tutor.get("cache_creation"),
            "partial": tutor.get("partial"),
            "tool_rounds": tutor.get("tool_rounds"),
            "tool_log": tutor.get("tool_log"),
        }
    if reply_chars is not None:
        data["reply_chars"] = reply_chars
    return _sse("debug", data)


def _student_notice_sse(result: "pipeline.PipelineResult") -> str:
    """A notice event rendered through the SAME template the student sees, so the test
    mirrors the real other_subject / off_topic / error notices (no switch link here)."""
    html = templates.env.get_template("student/_response.html").render(result=result, switch_id=None)
    return _sse("notice", {"html": html})


def _plain_notice_sse(msg: str) -> str:
    return _sse("notice", {"html": f'<div class="bubble notice">{_html.escape(msg)}</div>'})


@router.get("/test-chat", response_class=HTMLResponse)
def test_chat_page(request: Request, _: bool = Depends(auth.current_admin)):
    options = []
    for st in models.list_students():
        for s in models.list_subjects(st["id"], active_only=False):
            options.append({"id": s["id"], "label": f"{st['name']} · {s['name']}"})
    return templates.TemplateResponse(request, "admin/test_chat.html", {"options": options})


@router.post("/test-chat/stream")
async def test_chat_stream(request: Request, subject_id: int = Form(...),
                           message: str = Form(...),
                           _: bool = Depends(auth.current_admin)):
    """A faithful dry run of the full student turn path — gate -> branch -> tutor — over
    the chosen subject. Turns are PERSISTED to a SEPARATE test thread (is_test=1) so a
    parent (and the maintainer) can review exactly what the model produced — e.g. the raw
    SVG of a bad diagram. Still NO caps and NO usage charged; history comes from that test
    thread, mirroring the student pipeline. "New conversation" wipes the test thread."""
    text = message.strip()
    subject = models.get_subject(subject_id)

    async def gen():
        if subject is None or not text:
            yield _plain_notice_sse("Pick a subject and type a message.")
            return
        student = models.get_student(subject["student_id"])
        if student is None:  # defensive — FK cascade makes an orphaned subject unreachable
            yield _plain_notice_sse("That subject is no longer available.")
            return
        student_id = student["id"]
        enrolled = models.list_subjects(student_id, active_only=True)
        model = subject["tutor_model"]
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        # Reuse the student persistence wheel: a dedicated is_test thread per (student,
        # subject). Prior turns become the tutor's history (off the event loop).
        conv_id = await run_in_threadpool(
            models.get_or_create_conversation, student_id, subject_id, True)
        history = await run_in_threadpool(pipeline._history_for_tutor, conv_id)

        # Full path: gate + branch (off the event loop). No caps.
        decision = await run_in_threadpool(pipeline.classify_turn, subject, enrolled, text, history)

        # Blocked / notice branches: log the (blocked) student turn so it's reviewable,
        # surface the gate decision in the debug log, then send the student-facing notice.
        if decision.verdict in ("other_subject", "off_topic", "error"):
            await run_in_threadpool(
                models.add_message, conv_id, "student", text,
                blocked=(decision.verdict == "off_topic"),
                gate_verdict=decision.verdict, gate_reason=decision.reason,
                token_count=decision.gate_tokens)
            if decision.verdict == "other_subject":
                result = pipeline.PipelineResult(
                    status="other_subject", suggested_subject=decision.suggested_subject)
            elif decision.verdict == "off_topic":
                result = pipeline.PipelineResult(status="off_topic", message=pipeline.OFF_TOPIC_MSG)
            else:
                result = pipeline.PipelineResult(
                    status="error", message=pipeline.error_message(decision.error_kind))
            yield _debug_sse(decision, history_turns=len(history), latency_ms=elapsed_ms(),
                             model=model)
            yield _student_notice_sse(result)
            return

        # On subject -> stream the tutor with the conversation history.
        use_tools = tutor_runtime.tools_active(subject)
        framing = await run_in_threadpool(models.get_setting, models.FRAMING_SETTING_KEY)
        system = build_tutor_system(student, subject, tools_enabled=use_tools, framing=framing)
        tutor_history = history + [{"role": "user", "content": text}]
        chunks: list[str] = []
        meta: dict = {}
        try:
            async for ev in tutor_runtime.tutor_stream(model, system, tutor_history, use_tools=use_tools):
                if isinstance(ev, str):
                    chunks.append(ev)
                    yield _sse("delta", {"text": ev})
                elif isinstance(ev, dict):
                    if "status" in ev:
                        yield _sse("status", {"tool": ev.get("tool")})
                    else:
                        meta = ev
        except model_client.ModelError:
            # Log the attempt as an error turn (kept out of future tutor context).
            await run_in_threadpool(
                models.add_message, conv_id, "student", text,
                gate_verdict="error", gate_reason="tutor unavailable",
                token_count=decision.gate_tokens)
            yield _debug_sse(decision, history_turns=len(history), latency_ms=elapsed_ms(),
                             model=model, tutor={"finish_reason": "error"}, reply_chars=0)
            yield _plain_notice_sse("The model call failed — check the model and API key.")
            return
        # The tools path carries the full reply (incl. embedded figure fences) in meta;
        # the plain path has none, so fall back to the streamed text.
        reply = (meta.get("reply") if meta.get("reply") is not None else "".join(chunks)).strip()

        # Persist the completed turn (raw reply stored verbatim, incl. any ```svg source)
        # so it can be reviewed later. No usage is charged (test thread).
        if reply:
            await run_in_threadpool(
                models.add_message, conv_id, "student", text,
                gate_verdict="on_subject", gate_reason=decision.reason,
                token_count=decision.gate_tokens)
            await run_in_threadpool(
                models.add_message, conv_id, "tutor", reply,
                token_count=meta.get("tokens", 0) or 0)

        yield _debug_sse(decision, history_turns=len(history), latency_ms=elapsed_ms(),
                         model=model, tutor=meta, reply_chars=len(reply))
        if not reply:
            yield _plain_notice_sse("The model returned an empty reply.")
            return
        yield _sse("done", {"html": str(render_tutor_markdown(reply))})

    async def safe_gen():
        # Last-resort guard: any unhandled error still closes with a notice so the test
        # chat never hangs on a stuck "thinking" bubble.
        try:
            async for ev in gen():
                yield ev
        except Exception:  # noqa: BLE001
            yield _plain_notice_sse(
                "Something went wrong running that — check the server logs, model, and API key.")

    return StreamingResponse(
        safe_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/test-chat/clear")
async def test_chat_clear(request: Request, _: bool = Depends(auth.current_admin)):
    """Wipe all admin chat-test threads — backs the "New conversation" button. Real
    student conversations are untouched."""
    await run_in_threadpool(models.clear_test_conversations)
    return Response(status_code=204)
