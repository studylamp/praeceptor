"""The request pipeline: caps -> gate -> tutor -> log.

`process_message` is the single entry point the student router calls. It returns a
dict the router renders; every branch persists what happened (including blocked
attempts) before returning.
"""

from dataclasses import dataclass
from typing import Optional

from app import clock, models, model_client
from app.config import settings
from app.prompts import build_gate_system, build_tutor_system

# Friendly, kid-facing copy. Routers may override, but these are sensible defaults.
OFF_TOPIC_MSG = (
    "That doesn't look like part of this subject, so I can't help with it here. "
    "If it's for a different subject, pick that subject — otherwise check with your parent."
)
ERROR_MSG = ("I had trouble understanding that. Could you try saying it a different way? "
             "If it keeps happening, ask your parent to check on the tutor.")
CAPPED_MSG = "You've reached today's limit for this. Come back tomorrow, or ask your parent."
# Shown when something on OUR side breaks (model/tool/system error), as opposed to the
# student's input. Points the student to a parent so a stuck turn is never a dead end.
SYSTEM_FAIL_MSG = (
    "Sorry — something went wrong on my end, so I couldn't finish that. Please wait a "
    "moment and try again. If it keeps happening, ask your parent to check on the tutor."
)
# Shown when a message targets a chat that no longer exists (e.g. a parent deleted it
# while the page was open). Retrying can't fix it — point at reloading instead.
CHAT_GONE_MSG = ("This chat isn't available anymore — reload the page, then pick a "
                 "chat or start a new one.")

# Cap how much prior dialogue the tutor sees — for token cost and multi-turn drift —
# but trim in STEPS, never a per-turn sliding window. Prompt caching is a byte-prefix
# match (see model_client._cached_messages): a window that slides each exchange changes
# the OLDEST retained turn on every request, which invalidates the cached history and
# re-writes all of it at the cache-WRITE premium every turn — worse than no caching.
# Instead the window grows to MAX + STEP - 1 turns, then chops back to MAX in one
# deterministic cut, so between chops the prefix is byte-stable and served as cache
# reads (~0.1x input price). A second, token-estimated ceiling bounds paste-heavy
# sessions (a student pasting novella chapters) that a turn count alone wouldn't.
# Older turns drop from the model's context only — they're still logged. The admin
# chat test shares this exact path (its history is rebuilt server-side from the
# persisted is_test thread — see routers/admin.py), so there is no client-side bound
# to keep in sync.
MAX_HISTORY_TURNS = 40      # turns kept after a chop (~20 exchanges of continuity)
HISTORY_TRIM_STEP = 20      # chop granularity (turns); also the token-ceiling block size
MAX_HISTORY_TOKENS = 50_000  # estimated (~chars/4) ceiling on retained history
MIN_HISTORY_TURNS = 8       # keep at least this much continuity, even over the ceiling


def _est_history_tokens(turns: list[dict]) -> int:
    """~4 chars/token estimate over turn contents (same heuristic as elsewhere)."""
    return sum(len(t["content"]) for t in turns) // 4


def _trim_history(turns: list[dict]) -> list[dict]:
    """Drop the oldest turns in whole HISTORY_TRIM_STEP blocks (never one-by-one), so
    consecutive requests keep a byte-identical history prefix between chops. Pure and
    deterministic in the input rows: the same conversation state always trims the same
    way. The token ceiling only ever drops a block when at least MIN_HISTORY_TURNS
    would remain — one giant pasted turn can still exceed the ceiling; the daily token
    cap remains the hard spend bound."""
    n = len(turns)
    drop = 0
    if n > MAX_HISTORY_TURNS:
        drop = ((n - MAX_HISTORY_TURNS) // HISTORY_TRIM_STEP) * HISTORY_TRIM_STEP
    while (n - drop - HISTORY_TRIM_STEP >= MIN_HISTORY_TURNS
           and _est_history_tokens(turns[drop:]) > MAX_HISTORY_TOKENS):
        drop += HISTORY_TRIM_STEP
    return turns[drop:]


def _today() -> str:
    # Calendar date in the configured display zone; "daily" caps reset at that zone's
    # midnight. The zone is parent-set in admin → Settings (unset = server-local). The
    # admin usage view uses the same basis (see admin._today). See app/clock.py.
    return clock.today_str()


@dataclass
class PipelineResult:
    status: str  # on_subject | other_subject | off_topic | error | capped
    reply: Optional[str] = None             # tutor text (on_subject)
    suggested_subject: Optional[str] = None  # named subject to switch to (other_subject)
    message: Optional[str] = None            # friendly notice (off_topic | error | capped)
    gate_reason: Optional[str] = None


def _cap_exceeded(student) -> bool:
    msg_cap = student["daily_message_cap"]
    tok_cap = student["daily_token_cap"]
    if msg_cap is None and tok_cap is None:
        return False
    usage = models.get_usage(student["id"], _today())
    if usage is None:
        return False
    if msg_cap is not None and usage["message_count"] >= msg_cap:
        return True
    if tok_cap is not None and usage["token_count"] >= tok_cap:
        return True
    return False


def _history_for_tutor(conversation_id: int) -> list[dict]:
    """The actual tutor dialogue as chat turns: tutor replies, plus student turns
    that were on-subject. Off-topic (blocked), other-subject, and gate-error student
    turns are excluded so the tutor's context isn't polluted by messages it never
    answered. Bounded by the stepped trim above (cache-friendly turn + token ceilings;
    and trimmed so it never starts on an assistant turn, which the chat API rejects)."""
    turns: list[dict] = []
    for r in models.get_messages(conversation_id, include_blocked=False):
        if r["role"] == "tutor":
            turns.append({"role": "assistant", "content": r["content"]})
        elif r["role"] == "student" and r["gate_verdict"] in (None, "on_subject"):
            turns.append({"role": "user", "content": r["content"]})
    turns = _trim_history(turns)
    while turns and turns[0]["role"] == "assistant":
        turns.pop(0)
    return turns


def _match_enrolled(name: Optional[str], enrolled) -> Optional[str]:
    """Return the canonical enrolled subject name matching `name` (case-insensitive),
    or None. Guards against the gate naming a subject the student isn't enrolled in."""
    if not name:
        return None
    for s in enrolled:
        if s["name"].lower() == name.strip().lower():
            return s["name"]
    return None


def _record(conv_id: int, student_id: int, text: str, verdict: str, reason: Optional[str],
            tokens: int, blocked: bool = False) -> None:
    """Log a student turn (with its gate verdict) and charge it against usage.
    Used by the non-on_subject branches, which all share this shape."""
    models.add_message(conv_id, "student", text, blocked=blocked,
                       gate_verdict=verdict, gate_reason=reason, token_count=tokens)
    models.increment_usage(student_id, _today(), messages=1, tokens=tokens)


@dataclass
class GateDecision:
    """Pure gate+branch outcome — no DB reads or writes. `verdict` is the final branch
    (on_subject | other_subject | off_topic | error); `suggested_subject` is set only
    for a confirmed other_subject (a valid enrolled name)."""
    verdict: str
    suggested_subject: Optional[str]
    reason: Optional[str]
    gate_tokens: int
    # For verdict=='error' only: 'transport' = the gate's model call failed (bad key,
    # provider down) → a system problem to point at the parent; 'unclassified' = the model
    # was reachable but gave no usable verdict → the "try rephrasing" case. None otherwise.
    error_kind: Optional[str] = None


def error_message(error_kind: Optional[str]) -> str:
    """Student-facing notice for a gate error, keyed on WHY it failed: a model-call failure
    is a technical problem (point at the parent), not something rephrasing can fix."""
    return SYSTEM_FAIL_MSG if error_kind == "transport" else ERROR_MSG


def classify_turn(subject, enrolled, text: str, history: list[dict] | None) -> GateDecision:
    """Run the gate and resolve the branch, given an explicit history (no persistence).
    Shared by the real pipeline (`prepare`) and the admin chat-test path, so both apply
    identical gate/branch rules. `history` is recent chat turns (user/assistant)."""
    gate, gate_tokens = model_client.run_gate(
        settings.gate_model, build_gate_system(subject, enrolled), text, history=history,
    )
    verdict = gate["verdict"]
    suggested = None
    if verdict == "other_subject":
        # Only honor it if the named subject is actually enrolled; otherwise the gate
        # hallucinated a subject the student can't switch to → treat as off-topic.
        suggested = _match_enrolled(gate["subject"], enrolled)
        if suggested is None:
            verdict = "off_topic"
    return GateDecision(verdict=verdict, suggested_subject=suggested,
                        reason=gate["reason"], gate_tokens=gate_tokens,
                        error_kind=gate.get("error_kind"))


@dataclass
class Prepared:
    """Outcome of the front half of the pipeline (caps + gate + branch).

    Either `result` is set — a terminal outcome (capped / off_topic / other_subject /
    gate-error) that has ALREADY been persisted where the old code persisted it — and
    the caller just renders it; or `result` is None, meaning the message is on-subject
    and the caller should call the tutor with the carried-over context, then
    `finalize_on_subject`. Splitting here lets the sync (`process_message`) and the
    streaming router share one copy of the gate/branch logic.
    """
    result: Optional[PipelineResult] = None
    student: Optional[object] = None
    subject: Optional[object] = None
    conv_id: Optional[int] = None
    history: Optional[list[dict]] = None  # tutor history + the current msg as last user turn
    gate_tokens: int = 0
    gate_reason: Optional[str] = None


def prepare(student_id: int, subject_id: int, text: str,
            conversation_id: Optional[int] = None) -> Prepared:
    """Caps -> gate -> branch. Persists every non-on_subject turn (as the old
    pipeline did); returns the carry-over context when the message is on-subject.
    `conversation_id` targets a specific chat (multi-chat subjects); None = the
    subject's current thread."""
    student = models.get_student(student_id)
    subject = models.get_subject(subject_id)
    # Subject must exist, belong to this student, and be active (a stale tab on a
    # deactivated subject must not drive a tutor turn).
    if student is None or subject is None or subject["student_id"] != student_id or not subject["active"]:
        return Prepared(result=PipelineResult(status="error", message=ERROR_MSG))

    # 1. Caps (before any model call).
    if _cap_exceeded(student):
        return Prepared(result=PipelineResult(status="capped", message=CAPPED_MSG))

    enrolled = models.list_subjects(student_id, active_only=True)
    # An explicit chat target is honored only while the subject allows multiple chats:
    # once the parent turns the flag off ("one continuous conversation"), a stale tab's
    # id must not keep side threads alive — the turn falls back to the current thread.
    if conversation_id is not None and not subject["multi_chat_enabled"]:
        conversation_id = None
    if conversation_id is None:
        conv_id = models.get_or_create_conversation(student_id, subject_id)
    else:
        # The targeted chat must be this student's own REAL thread under THIS subject —
        # a tampered or stale id must not read or write another thread's history.
        conv = models.get_conversation(conversation_id)
        if (conv is None or conv["student_id"] != student_id
                or conv["subject_id"] != subject_id or conv["is_test"]):
            return Prepared(result=PipelineResult(status="error", message=CHAT_GONE_MSG))
        conv_id = conversation_id
    history = _history_for_tutor(conv_id)

    # 2. Gate (Haiku) + branch. Fails closed: a transport/parse failure yields "error".
    decision = classify_turn(subject, enrolled, text, history)
    gate_tokens, reason = decision.gate_tokens, decision.reason

    # Other subject -> log it and let the UI offer to switch.
    if decision.verdict == "other_subject":
        _record(conv_id, student_id, text, "other_subject", reason, gate_tokens)
        return Prepared(result=PipelineResult(status="other_subject",
                                              suggested_subject=decision.suggested_subject,
                                              gate_reason=reason))

    # Off-topic -> log blocked attempt, friendly refusal, no tutor.
    if decision.verdict == "off_topic":
        _record(conv_id, student_id, text, "off_topic", reason, gate_tokens, blocked=True)
        return Prepared(result=PipelineResult(status="off_topic", message=OFF_TOPIC_MSG,
                                              gate_reason=reason))

    # Gate failure -> withhold tutor (fail closed), ask to retry. Not a block.
    if decision.verdict == "error":
        _record(conv_id, student_id, text, "error", reason, gate_tokens)
        return Prepared(result=PipelineResult(status="error",
                                              message=error_message(decision.error_kind),
                                              gate_reason=reason))

    # On subject -> defer all persistence until the tutor reply is in hand (so a
    # provider error can't leave an orphaned student turn or mis-count usage). The
    # current message isn't stored yet; append it as the final user turn.
    full_history = history + [{"role": "user", "content": text}]
    return Prepared(student=student, subject=subject, conv_id=conv_id, history=full_history,
                    gate_tokens=gate_tokens, gate_reason=reason)


def finalize_on_subject(conv_id: int, student_id: int, text: str, gate_reason: Optional[str],
                        gate_tokens: int, reply: str, tutor_tokens: int) -> None:
    """Persist a completed on-subject turn (student msg + tutor reply) and charge usage."""
    models.add_message(conv_id, "student", text, blocked=False,
                       gate_verdict="on_subject", gate_reason=gate_reason, token_count=gate_tokens)
    models.add_message(conv_id, "tutor", reply, token_count=tutor_tokens)
    models.increment_usage(student_id, _today(), messages=1, tokens=gate_tokens + tutor_tokens)


def record_tutor_error(conv_id: int, student_id: int, text: str, gate_tokens: int) -> None:
    """Tutor unavailable: log the attempt as an error turn (kept out of future tutor
    context) and charge only the gate tokens already spent."""
    _record(conv_id, student_id, text, "error", "tutor unavailable", gate_tokens)


def estimate_tokens(text: str) -> int:
    """Rough fallback when a streamed response didn't report usage (~4 chars/token).
    Keeps daily caps roughly honest rather than charging zero."""
    return max(1, len(text) // 4)


def process_message(student_id: int, subject_id: int, text: str,
                    conversation_id: Optional[int] = None) -> PipelineResult:
    """Non-streaming pipeline (kept for the no-JS fallback and offline tests)."""
    prep = prepare(student_id, subject_id, text, conversation_id)
    if prep.result is not None:
        return prep.result
    try:
        framing = models.get_setting(models.FRAMING_SETTING_KEY)
        reply, tutor_tokens = model_client.run_tutor(
            prep.subject["tutor_model"],
            build_tutor_system(prep.student, prep.subject, framing=framing),
            prep.history,
        )
    except model_client.ModelError:
        record_tutor_error(prep.conv_id, student_id, text, prep.gate_tokens)
        return PipelineResult(status="error", message=ERROR_MSG)

    finalize_on_subject(prep.conv_id, student_id, text, prep.gate_reason,
                        prep.gate_tokens, reply, tutor_tokens)
    return PipelineResult(status="on_subject", reply=reply, gate_reason=prep.gate_reason)
