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


def _notice(request: Request, student_text: str, notice: str) -> HTMLResponse:
    """A 200 chat partial (student bubble + notice) so htmx always has something to
    swap in — raw 4xx responses are silently dropped and leave the kid with nothing."""
    result = pipeline.PipelineResult(status="error", message=notice)
    return templates.TemplateResponse(
        request, "student/_exchange.html",
        {"message": student_text, "result": result, "switch_id": None},
    )


@router.get("/chat/{subject_id}", response_class=HTMLResponse)
def chat_page(request: Request, subject_id: int, student=Depends(auth.current_student)):
    sub = _owned_subject(student, subject_id)
    if sub is None:
        return RedirectResponse("/subjects", status_code=303)
    conv_id = models.get_or_create_conversation(student["id"], subject_id)
    # Show the real dialogue: tutor replies + on-subject student turns (skip blocked
    # off-topic / other-subject / error turns, which were never answered).
    rows = models.get_messages(conv_id, include_blocked=False)
    history = [m for m in rows if m["role"] == "tutor" or m["gate_verdict"] in (None, "on_subject")]
    return templates.TemplateResponse(
        request, "student/chat.html",
        {"student": student, "subject": sub, "history": history},
    )


@router.post("/chat/{subject_id}/send", response_class=HTMLResponse)
async def chat_send(request: Request, subject_id: int, message: str = Form(...),
                    student=Depends(auth.current_student)):
    text = message.strip()
    sub = _owned_subject(student, subject_id)
    if sub is None:
        return _notice(request, text, "This subject isn't available anymore — go back to Subjects.")
    if not text:
        return _notice(request, "", "Type a message first.")

    lock = _locks.get(student["id"]) or _locks.setdefault(student["id"], asyncio.Lock())
    async with lock:
        result = await run_in_threadpool(pipeline.process_message, student["id"], subject_id, text)

    return templates.TemplateResponse(
        request, "student/_exchange.html",
        {"message": text, "result": result, "switch_id": _switch_id_for(student["id"], result)},
    )


@router.post("/chat/{subject_id}/stream")
async def chat_send_stream(request: Request, subject_id: int, message: str = Form(...),
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
                prep = await run_in_threadpool(pipeline.prepare, sid, subject_id, text)
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
                        prep.subject["tutor_model"], system, prep.history, use_tools=use_tools
                    ):
                        if isinstance(ev, str):
                            chunks.append(ev)
                            yield _sse("delta", {"text": ev})
                        elif isinstance(ev, dict):
                            if "status" in ev:
                                yield _sse("status", {"tool": ev.get("tool")})
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
