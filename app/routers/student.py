"""Student-facing routes: login (name + PIN), subject picker, and the HTMX chat.

The chat send runs the pipeline in a threadpool under a per-student lock, which
serializes a student's turns — preventing double-submit races and making the
cap check-and-increment effectively atomic per student.
"""

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app import auth, model_client, models, pipeline, tutor_runtime
from app.config import settings
from app.prompts import build_tutor_system
from app.render import render_tutor_markdown
from app.security import verify_secret
from app.templating import templates

router = APIRouter()

_locks: dict[int, asyncio.Lock] = {}


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event. json.dumps escapes any newlines in the payload,
    so each event stays a single well-formed `data:` line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _render_response(result: "pipeline.PipelineResult", switch_id: Optional[int]) -> str:
    """Render the response partial (notice/other-subject/error bubble) to a string,
    reusing the same template the no-JS path uses."""
    return templates.env.get_template("student/_response.html").render(
        result=result, switch_id=switch_id
    )


def _switch_id_for(student_id: int, result: "pipeline.PipelineResult") -> Optional[int]:
    """For an other_subject result, the id of the enrolled subject to switch to."""
    if result.status == "other_subject" and result.suggested_subject:
        return next((s["id"] for s in models.list_subjects(student_id, active_only=True)
                     if s["name"] == result.suggested_subject), None)
    return None


def _login_page(request: Request, error: Optional[str] = None, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "student/login.html",
        {"students": models.list_students(), "error": error},
        status_code=status_code,
    )


@router.get("/", response_class=HTMLResponse)
def root(request: Request):
    dest = "/subjects" if request.session.get(auth.STUDENT_KEY) else "/login"
    return RedirectResponse(dest, status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _login_page(request)


@router.post("/login")
def login(request: Request, student_id: int = Form(...), pin: str = Form(...)):
    student = models.get_student(student_id)
    if student is None:
        return _login_page(request, "Please pick your name and enter your PIN.", 400)
    if auth.is_locked(student_id):
        return _login_page(request, "Too many tries. Wait a minute and try again.", 429)
    if not verify_secret(student["pin_hash"], pin):
        auth.record_failure(student_id)
        return _login_page(request, "That PIN didn't match. Try again.", 400)
    auth.clear_failures(student_id)
    auth.login_student(request, student_id)
    return RedirectResponse("/subjects", status_code=303)


@router.get("/logout")
def logout(request: Request):
    auth.logout_student(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/subjects", response_class=HTMLResponse)
def subjects(request: Request, student=Depends(auth.current_student)):
    return templates.TemplateResponse(
        request, "student/subjects.html",
        {"student": student, "subjects": models.list_subjects(student["id"], active_only=True)},
    )


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, student=Depends(auth.current_student)):
    """Student help: how to use the tutor, and how to type math questions."""
    return templates.TemplateResponse(request, "student/help.html", {"student": student})


def _owned_subject(student, subject_id: int):
    sub = models.get_subject(subject_id)
    if sub is None or sub["student_id"] != student["id"] or not sub["active"]:
        return None
    return sub


def _owned_chat(student, subject_id: int, conversation_id: int):
    """The conversation row, only if it's this student's own REAL chat under this
    subject (never the admin test thread); None otherwise."""
    conv = models.get_conversation(conversation_id)
    if (conv is None or conv["student_id"] != student["id"]
            or conv["subject_id"] != subject_id or conv["is_test"]):
        return None
    return conv


# Chat titles are student-typed free text shown in their own picker and the admin log;
# collapse whitespace and bound the length (the input has the same maxlength).
MAX_CHAT_TITLE = 60


def _clean_title(raw: Optional[str]) -> Optional[str]:
    t = " ".join((raw or "").split())
    return t[:MAX_CHAT_TITLE] or None


def _resolve_chat(student, sub, chat: Optional[int]):
    """Pick the conversation the chat page/history should show. Multi-chat subjects
    honor an explicit owned `?chat=` id and fall back to the current thread; single-chat
    subjects ignore the param entirely. Returns (conv_id, chats) where `chats` is the
    switcher list (empty for single-chat subjects)."""
    multi = bool(sub["multi_chat_enabled"])
    if multi and chat is not None and _owned_chat(student, sub["id"], chat) is not None:
        conv_id = chat
    else:
        conv_id = models.get_or_create_conversation(student["id"], sub["id"])
    chats = models.list_subject_chats(student["id"], sub["id"]) if multi else []
    return conv_id, chats


def _notice(request: Request, student_text: str, notice: str) -> HTMLResponse:
    """A 200 chat partial (student bubble + notice) so htmx always has something to
    swap in — raw 4xx responses are silently dropped and leave the kid with nothing."""
    result = pipeline.PipelineResult(status="error", message=notice)
    return templates.TemplateResponse(
        request, "student/_exchange.html",
        {"message": student_text, "result": result, "switch_id": None},
    )


@router.get("/chat/{subject_id}", response_class=HTMLResponse)
def chat_page(request: Request, subject_id: int, full: bool = False,
              chat: Optional[int] = None, student=Depends(auth.current_student)):
    sub = _owned_subject(student, subject_id)
    if sub is None:
        return RedirectResponse("/subjects", status_code=303)
    conv_id, chats = _resolve_chat(student, sub, chat)
    current_chat = next((c for c in chats if c["id"] == conv_id), None)
    # Show the real dialogue: tutor replies + on-subject student turns (blocked
    # off-topic / other-subject / error turns were never answered → hidden). Windowed to
    # the most recent messages; a "Load earlier" link pages back (?full=1 is the no-JS
    # fallback that renders the whole thread). Display only — the tutor's context is
    # unaffected (see pipeline._history_for_tutor).
    limit = None if full else settings.chat_initial_messages
    history, has_more = models.get_messages_page(conv_id, limit=limit, visible_only=True)
    return templates.TemplateResponse(
        request, "student/chat.html",
        {"student": student, "subject": sub, "history": history,
         "has_more": has_more, "oldest_id": history[0]["id"] if history else None,
         "multi": bool(sub["multi_chat_enabled"]), "chats": chats,
         "current_chat": current_chat,
         "chat_param": conv_id if sub["multi_chat_enabled"] else None},
    )


@router.get("/chat/{subject_id}/history", response_class=HTMLResponse)
def chat_history(request: Request, subject_id: int, before: int,
                 chat: Optional[int] = None, student=Depends(auth.current_student)):
    """One page of older messages (before message id `before`), for the "Load earlier"
    link. Returns the same bubble partials the page uses, oldest-first, led by a fresh
    loader when still-older messages remain. Display only."""
    sub = _owned_subject(student, subject_id)
    if sub is None:
        return RedirectResponse("/subjects", status_code=303)
    if chat is not None and sub["multi_chat_enabled"]:
        if _owned_chat(student, subject_id, chat) is None:
            # The chat vanished mid-session (e.g. the parent deleted it). Return an
            # empty chunk — no rows, no fresh loader — rather than silently splicing
            # ANOTHER conversation's older messages into the stale page.
            return templates.TemplateResponse(
                request, "student/_history_chunk.html",
                {"subject": sub, "history": [], "has_more": False,
                 "oldest_id": None, "chat_param": None})
        conv_id = chat
    else:
        conv_id = models.get_or_create_conversation(student["id"], subject_id)
    history, has_more = models.get_messages_page(
        conv_id, before_id=before, limit=settings.chat_history_step, visible_only=True)
    return templates.TemplateResponse(
        request, "student/_history_chunk.html",
        {"subject": sub, "history": history,
         "has_more": has_more, "oldest_id": history[0]["id"] if history else None,
         "chat_param": conv_id if sub["multi_chat_enabled"] else None},
    )


@router.post("/chat/{subject_id}/new")
async def chat_new(request: Request, subject_id: int, title: str = Form(""),
                   student=Depends(auth.current_student)):
    """Start a new chat (multi-chat subjects only). A blank name is fine — the picker
    falls back to a date-based label, and the student can rename later. Takes the same
    per-student lock as sending: an in-flight streamed turn persists only when it
    finishes, so until then its chat still LOOKS empty and models.create_chat would
    wrongly reuse it — the lock makes "New chat" wait the turn out."""
    sub = _owned_subject(student, subject_id)
    if sub is None:
        return RedirectResponse("/subjects", status_code=303)
    if not sub["multi_chat_enabled"]:
        return RedirectResponse(f"/chat/{subject_id}", status_code=303)
    lock = _locks.get(student["id"]) or _locks.setdefault(student["id"], asyncio.Lock())
    async with lock:
        conv_id = await run_in_threadpool(models.create_chat, student["id"], subject_id,
                                          _clean_title(title))
    return RedirectResponse(f"/chat/{subject_id}?chat={conv_id}", status_code=303)


@router.post("/chat/{subject_id}/chats/{conversation_id}/rename")
def chat_rename(request: Request, subject_id: int, conversation_id: int,
                title: str = Form(""), student=Depends(auth.current_student)):
    sub = _owned_subject(student, subject_id)
    if sub is None:
        return RedirectResponse("/subjects", status_code=303)
    if not sub["multi_chat_enabled"] or _owned_chat(student, subject_id, conversation_id) is None:
        return RedirectResponse(f"/chat/{subject_id}", status_code=303)
    cleaned = _clean_title(title)
    if cleaned:
        models.rename_conversation(conversation_id, cleaned)
    # Whitespace-only input skips the rename but still returns to the SAME chat —
    # bouncing to the default thread would silently switch the student's context.
    return RedirectResponse(f"/chat/{subject_id}?chat={conversation_id}", status_code=303)


@router.post("/chat/{subject_id}/chats/{conversation_id}/archive")
def chat_archive(request: Request, subject_id: int, conversation_id: int,
                 student=Depends(auth.current_student)):
    """Toggle a chat's archived flag. Archiving only hides it from the picker's main
    list (it stays continuable and admin-visible); archiving the open chat lands the
    student back on their current thread."""
    sub = _owned_subject(student, subject_id)
    if sub is None:
        return RedirectResponse("/subjects", status_code=303)
    conv = _owned_chat(student, subject_id, conversation_id)
    if not sub["multi_chat_enabled"] or conv is None:
        return RedirectResponse(f"/chat/{subject_id}", status_code=303)
    now_archived = not conv["archived"]
    models.set_conversation_archived(conversation_id, now_archived)
    if not now_archived:
        return RedirectResponse(f"/chat/{subject_id}?chat={conversation_id}", status_code=303)
    # Archiving the last visible chat would otherwise drop the student straight back
    # into it (the current-thread pick falls through to archived chats when nothing
    # else exists) — start a fresh unnamed chat so Archive visibly puts the old one away.
    chats = models.list_subject_chats(student["id"], subject_id)
    if not any(c["archived"] == 0 for c in chats):
        models.create_chat(student["id"], subject_id, None)
    return RedirectResponse(f"/chat/{subject_id}", status_code=303)


@router.post("/chat/{subject_id}/send", response_class=HTMLResponse)
async def chat_send(request: Request, subject_id: int, message: str = Form(...),
                    conversation_id: Optional[int] = Form(None),
                    student=Depends(auth.current_student)):
    text = message.strip()
    sub = _owned_subject(student, subject_id)
    if sub is None:
        return _notice(request, text, "This subject isn't available anymore — go back to Subjects.")
    if not text:
        return _notice(request, "", "Type a message first.")

    lock = _locks.get(student["id"]) or _locks.setdefault(student["id"], asyncio.Lock())
    async with lock:
        # conversation_id (the multi-chat page's hidden field) is ownership-checked in
        # pipeline.prepare; None = the subject's current thread (single-chat behavior).
        result = await run_in_threadpool(pipeline.process_message, student["id"], subject_id,
                                         text, conversation_id)

    return templates.TemplateResponse(
        request, "student/_exchange.html",
        {"message": text, "result": result, "switch_id": _switch_id_for(student["id"], result)},
    )


@router.post("/chat/{subject_id}/stream")
async def chat_send_stream(request: Request, subject_id: int, message: str = Form(...),
                           conversation_id: Optional[int] = Form(None),
                           student=Depends(auth.current_student)):
    """Streaming counterpart of /send. Emits Server-Sent Events: `delta` per token
    chunk while the tutor writes, then `done` (with the rendered markdown+KaTeX HTML
    to swap in); non-on_subject outcomes come back as a single `notice` event. The
    per-student lock is held for the whole turn, so caps and double-submit stay
    serialized exactly as in the non-streaming path."""
    text = message.strip()
    sub = _owned_subject(student, subject_id)
    sid = student["id"]

    async def gen():
        lock = _locks.get(sid) or _locks.setdefault(sid, asyncio.Lock())
        async with lock:
            conv_id = None
            gate_tokens = 0
            tutor_done = False  # set once finalize begins, so the outer except doesn't
                                # double-write a turn that was already (partly) persisted
            try:
                if sub is None:
                    res = pipeline.PipelineResult(
                        status="error", message="This subject isn't available anymore — go back to Subjects.")
                    yield _sse("notice", {"html": _render_response(res, None)})
                    return
                if not text:
                    res = pipeline.PipelineResult(status="error", message="Type a message first.")
                    yield _sse("notice", {"html": _render_response(res, None)})
                    return

                # Caps + gate + branch (sync/DB/Haiku work → threadpool, off the loop).
                # conversation_id (multi-chat hidden field) is ownership-checked inside.
                prep = await run_in_threadpool(pipeline.prepare, sid, subject_id, text,
                                               conversation_id)
                if prep.result is not None:
                    yield _sse("notice", {"html": _render_response(prep.result, _switch_id_for(sid, prep.result))})
                    return
                conv_id = prep.conv_id
                gate_tokens = prep.gate_tokens

                # On subject → stream the tutor, accumulating the full reply for persistence.
                use_tools = tutor_runtime.tools_active(prep.subject)
                framing = await run_in_threadpool(models.get_setting, models.FRAMING_SETTING_KEY)
                system = build_tutor_system(prep.student, prep.subject,
                                            tools_enabled=use_tools, framing=framing)
                chunks: list[str] = []
                tokens = 0
                meta: dict = {}
                try:
                    async for ev in tutor_runtime.tutor_stream(
                        prep.tutor_model, system, prep.history, use_tools=use_tools
                    ):
                        if isinstance(ev, str):
                            chunks.append(ev)
                            yield _sse("delta", {"text": ev})
                        elif isinstance(ev, dict):
                            if "status" in ev:
                                yield _sse("status", tutor_runtime.status_payload(ev))
                            else:
                                meta = ev
                                tokens = ev.get("tokens", 0) or 0
                except model_client.ModelError:
                    await run_in_threadpool(pipeline.record_tutor_error, prep.conv_id, sid, text, prep.gate_tokens)
                    res = pipeline.PipelineResult(status="error", message=pipeline.SYSTEM_FAIL_MSG)
                    yield _sse("notice", {"html": _render_response(res, None)})
                    return

                # The tools path carries the full reply (incl. embedded figure fences) in
                # meta; the plain path has no meta reply, so fall back to the streamed text.
                reply = (meta.get("reply") if meta.get("reply") is not None else "".join(chunks)).strip()
                if not reply:
                    await run_in_threadpool(pipeline.record_tutor_error, prep.conv_id, sid, text, prep.gate_tokens)
                    res = pipeline.PipelineResult(status="error", message=pipeline.SYSTEM_FAIL_MSG)
                    yield _sse("notice", {"html": _render_response(res, None)})
                    return

                if tokens <= 0:  # provider didn't report usage — estimate so caps stay honest
                    tokens = pipeline.estimate_tokens(reply)
                tutor_done = True  # from here, the turn counts as persisted (don't re-log on error)
                await run_in_threadpool(pipeline.finalize_on_subject, prep.conv_id, sid, text,
                                        prep.gate_reason, prep.gate_tokens, reply, tokens)
                # Swap the streamed plain text for the sanitized markdown+KaTeX rendering.
                yield _sse("done", {"html": str(render_tutor_markdown(reply))})
            except Exception:  # noqa: BLE001 — last-resort: never leave the student with a stuck bubble
                # Log the failed attempt ONLY if we hadn't started persisting the turn,
                # so a mid-finalize failure can't double-write/double-count it.
                if conv_id is not None and not tutor_done:
                    try:
                        await run_in_threadpool(pipeline.record_tutor_error, conv_id, sid, text, gate_tokens)
                    except Exception:
                        pass
                res = pipeline.PipelineResult(status="error", message=pipeline.SYSTEM_FAIL_MSG)
                yield _sse("notice", {"html": _render_response(res, None)})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
