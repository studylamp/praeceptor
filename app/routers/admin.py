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

from app import auth, clock, model_client, models, pipeline, sandbox, tools, tutor_runtime
from app.config import settings
from app.prompts import build_gate_system, build_tutor_system
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
    return clock.today_str()  # configured display zone — same basis as the daily caps


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

def _settings_context(**overrides):
    """Shared context for the settings page (current values + the timezone picker list).
    Overrides let a re-render show just-submitted values instead of the stored ones."""
    ctx = {
        "framing": models.get_setting(models.FRAMING_SETTING_KEY) or "",
        "timezone": models.get_setting(models.TIMEZONE_SETTING_KEY) or "",
        "timezones": clock.available_zone_names(),
        # The .env-set gate model, shown read-only so the parent can see what runs the gate
        # and what to bump on a model release. (tutor_model_default is a template global.)
        "gate_model": settings.gate_model,
        # Always present so the template's `is not none` guard is reliable on EVERY render
        # of settings.html (Jinja's Undefined is not None, so omitting this would show the
        # reset flash on e.g. the bad-timezone error re-render). The reset redirect
        # overrides it with the count of cleared subjects.
        "models_reset": None,
    }
    ctx.update(overrides)
    return ctx


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, models_reset: Optional[int] = None,
                  _: bool = Depends(auth.current_admin)):
    # models_reset is set only when returning from a "reset all to default" POST, carrying
    # the count of subjects whose override was cleared; absent (None) = don't show the flash.
    return templates.TemplateResponse(
        request, "admin/settings.html",
        _settings_context(saved=bool(saved), models_reset=models_reset))


@router.post("/settings")
def settings_save(request: Request, educational_framing: str = Form(""),
                  app_timezone: str = Form(""), _: bool = Depends(auth.current_admin)):
    tz = app_timezone.strip()
    # Empty = server-local (the default). Otherwise it must be a real IANA zone; reject a
    # bad value (only reachable by a tampered POST, since the form is a fixed dropdown)
    # rather than storing something that would silently fall back to server-local. Keep the
    # user's other unsaved edit (framing) on the re-render.
    if tz and tz not in clock.available_zone_names():
        return templates.TemplateResponse(
            request, "admin/settings.html",
            _settings_context(timezone=tz, framing=educational_framing,
                              error=f"Unknown timezone: {tz}"),
            status_code=400)
    models.set_setting(models.FRAMING_SETTING_KEY, _clean_str(educational_framing))
    models.set_setting(models.TIMEZONE_SETTING_KEY, tz or None)
    clock.invalidate()  # take effect on the next request without a restart
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


@router.post("/settings/reset-models")
def settings_reset_models(request: Request, _: bool = Depends(auth.current_admin)):
    """Clear every subject's pinned tutor-model override so all subjects inherit the app
    default (TUTOR_MODEL_DEFAULT) again — the one-click way to move a whole fleet onto a
    freshly released model without editing each subject. Redirects back to Settings with a
    count of how many overrides were cleared."""
    count = models.reset_all_tutor_models()
    return RedirectResponse(f"/admin/settings?models_reset={count}", status_code=303)


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


@router.post("/students/{student_id}/reset-usage")
def student_reset_usage(request: Request, student_id: int, _: bool = Depends(auth.current_admin)):
    """Zero today's message/token counters so the daily caps start counting over —
    for giving a capped student more time today. The cap values themselves are
    unchanged (edit those in the details form)."""
    if models.get_student(student_id) is None:
        return RedirectResponse("/admin", status_code=303)
    models.reset_usage(student_id, _today())
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
    # Blank = the "inherit the app default" sentinel (empty string), resolved live at
    # request time by pipeline.resolve_tutor_model. A non-empty value pins this subject.
    tutor_model = _clean_str(form.get("tutor_model")) or ""
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
        "multi_chat_enabled": 1 if form.get("multi_chat_enabled") else 0,
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
    # tutor_model is left blank so new subjects INHERIT the app default (TUTOR_MODEL_DEFAULT)
    # live — the parent only fills it in to pin an override.
    return _subject_form_response(
        request, student=student,
        vals={"tutor_model": "", "tools_enabled": 1}, creating=True)


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


@router.get("/subjects/{subject_id}/copy", response_class=HTMLResponse)
def subject_copy_form(request: Request, subject_id: int, _: bool = Depends(auth.current_admin)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    student = models.get_student(subject["student_id"])
    return templates.TemplateResponse(
        request, "admin/subject_copy.html",
        {"subject": subject, "student": student, "students": models.list_students()})


@router.post("/subjects/{subject_id}/copy")
def subject_copy(request: Request, subject_id: int, _: bool = Depends(auth.current_admin),
                 target_student_id: int = Form(...)):
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    if models.get_student(target_student_id) is None:
        return _redir_student(subject["student_id"], "That student no longer exists.")
    if models.copy_subject(subject_id, target_student_id) is None:
        return RedirectResponse("/admin", status_code=303)  # deleted while confirming
    return RedirectResponse(f"/admin/students/{target_student_id}", status_code=303)


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
                    "— every chat, if it has several — so they start with a clean slate; "
                    "useful at a new chapter or unit. To remove just one chat, open it "
                    "under Conversations and delete it there. The subject and all its "
                    "settings are kept. This cannot be undone.",
         "confirm_label": "Yes, clear history",
         "action": f"/admin/subjects/{subject_id}/clear-history",
         "cancel": f"/admin/students/{subject['student_id']}"})


@router.get("/conversations/{conversation_id}/delete", response_class=HTMLResponse)
def conversation_delete_confirm(request: Request, conversation_id: int,
                                _: bool = Depends(auth.current_admin)):
    conv = models.get_conversation(conversation_id)
    if conv is None:
        return RedirectResponse("/admin/conversations", status_code=303)
    if conv["is_test"]:
        # Test threads have their own wipe path (chat test → New conversation); the
        # transcript page hides delete for them, and the handlers enforce it too.
        return RedirectResponse(f"/admin/conversations/{conversation_id}", status_code=303)
    label = f"“{conv['title']}”" if conv["title"] else "this chat"
    return templates.TemplateResponse(
        request, "admin/confirm_delete.html",
        {"title": f"Delete {label} ({conv['student_name']} · {conv['subject_name']})?",
         "warning": "This permanently deletes this ONE chat thread and its messages. "
                    "Other chats in the subject are kept. This cannot be undone.",
         "confirm_label": "Yes, delete this chat",
         "action": f"/admin/conversations/{conversation_id}/delete",
         "cancel": f"/admin/conversations/{conversation_id}"})


@router.post("/conversations/{conversation_id}/delete")
def conversation_delete(request: Request, conversation_id: int,
                        _: bool = Depends(auth.current_admin)):
    conv = models.get_conversation(conversation_id)
    if conv is not None and conv["is_test"]:
        # Guarded in the handler, not just the template: test threads are wiped via
        # the chat test's own "New conversation", never deleted one-by-one here.
        return RedirectResponse(f"/admin/conversations/{conversation_id}", status_code=303)
    models.delete_conversation(conversation_id)
    return RedirectResponse("/admin/conversations", status_code=303)


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


# --------------------------- prompt transparency ---------------------------

@router.get("/subjects/{subject_id}/prompts", response_class=HTMLResponse)
def subject_prompts(request: Request, subject_id: int, _: bool = Depends(auth.current_admin)):
    """Read-only view of the EXACT prompt text sent to the models for this subject —
    assembled by the same functions the live pipeline calls (build_gate_system /
    build_tutor_system / build_gate_user_message / tool_specs), so the page cannot
    drift from what a real turn sends. Only the student's own words are replaced
    with ‹placeholders›."""
    subject = models.get_subject(subject_id)
    if subject is None:
        return RedirectResponse("/admin", status_code=303)
    student = models.get_student(subject["student_id"])
    enrolled = list(models.list_subjects(student["id"], active_only=True))
    if not subject["active"]:
        # An inactive subject can't take a turn; preview what one WOULD send once
        # reactivated — it rejoins the enrolled list, in the live query's name order.
        enrolled = sorted(enrolled + [subject], key=lambda s: s["name"])
    framing = models.get_setting(models.FRAMING_SETTING_KEY)
    use_tools = tutor_runtime.tools_active(subject)
    gate_user_example = model_client.build_gate_user_message(
        "‹the student's new message›",
        history=[{"role": "user", "content": "‹an earlier student message›"},
                 {"role": "assistant", "content": "‹the tutor's reply to it›"}])
    return templates.TemplateResponse(request, "admin/subject_prompts.html", {
        "subject": subject,
        "student": student,
        "gate_model": settings.gate_model,
        "tutor_model": pipeline.resolve_tutor_model(subject),
        "model_inherited": not (subject["tutor_model"] or "").strip(),
        "subject_tools": bool(subject["tools_enabled"]),
        "use_tools": use_tools,
        "gate_system": build_gate_system(subject, enrolled),
        "gate_user_example": gate_user_example,
        "tutor_system": build_tutor_system(student, subject, tools_enabled=use_tools,
                                           framing=framing),
        "tool_specs_json": json.dumps(tools.tool_specs(), indent=2) if use_tools else None,
        "tool_feedback_notes": tools.MODEL_FEEDBACK_NOTES if use_tools else None,
        "gate_history_turns": model_client.GATE_HISTORY_TURNS,
        "gate_snippet_chars": model_client.GATE_SNIPPET_CHARS,
        "max_history_turns": pipeline.MAX_HISTORY_TURNS,
        # History trims back to MAX_HISTORY_TURNS in whole steps, so between chops the
        # model can see up to this many retained turns — the honest ceiling to publish.
        "max_history_kept": pipeline.MAX_HISTORY_TURNS + pipeline.HISTORY_TRIM_STEP - 1,
        "max_history_tokens": pipeline.MAX_HISTORY_TOKENS,
    })


# -------------------------- conversation log viewer ------------------------

@router.get("/conversations", response_class=HTMLResponse)
def conversations(request: Request, _: bool = Depends(auth.current_admin)):
    return templates.TemplateResponse(
        request, "admin/conversations.html", {"conversations": models.list_conversations()})


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation_detail(request: Request, conversation_id: int, full: bool = False,
                        _: bool = Depends(auth.current_admin)):
    conv = models.get_conversation(conversation_id)
    if conv is None:
        return RedirectResponse("/admin/conversations", status_code=303)
    # Windowed to the most recent turns; a "Load earlier" link pages back (?full=1 is the
    # no-JS fallback that renders the whole thread). Admin sees ALL turns, including
    # blocked ones (visible_only=False).
    limit = None if full else settings.chat_initial_messages
    messages, has_more = models.get_messages_page(conversation_id, limit=limit, visible_only=False)
    return templates.TemplateResponse(
        request, "admin/conversation.html",
        {"conv": conv, "conversation_id": conversation_id, "messages": messages,
         "has_more": has_more, "oldest_id": messages[0]["id"] if messages else None})


@router.get("/conversations/{conversation_id}/history", response_class=HTMLResponse)
def conversation_history(request: Request, conversation_id: int, before: int,
                         _: bool = Depends(auth.current_admin)):
    """One page of older transcript messages (before message id `before`), for the admin
    "Load earlier" link. Same bubble partials as the page, oldest-first, led by a fresh
    loader when still-older messages remain."""
    messages, has_more = models.get_messages_page(
        conversation_id, before_id=before, limit=settings.chat_history_step, visible_only=False)
    return templates.TemplateResponse(
        request, "admin/_transcript_chunk.html",
        {"conversation_id": conversation_id, "messages": messages,
         "has_more": has_more, "oldest_id": messages[0]["id"] if messages else None})


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
        model = pipeline.resolve_tutor_model(subject)
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
                        yield _sse("status", tutor_runtime.status_payload(ev))
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
