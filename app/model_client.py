"""All inference goes through here, via LiteLLM, so the provider is configuration.
Model strings are LiteLLM format, e.g. `anthropic/claude-haiku-4-5`.

Two entry points: `run_gate` (strict-JSON classifier) and `run_tutor` (free text).
Both return (result, tokens) so the pipeline can enforce per-student token caps.
"""

import json
import sys
from typing import Awaitable, Callable, Optional

import litellm

from app.config import settings

# LAN appliance: no phone-home, and drop any param a given provider/model rejects
# (e.g. some Claude models reject `temperature`) instead of erroring.
litellm.telemetry = False
litellm.drop_params = True

# Completion ceiling for tutor replies. Generous so a thorough answer — a full worked
# example with every step, or an explanation PLUS a detailed inline SVG/plot (gridlines,
# labels, many sampled points) — isn't truncated mid-thought (a cut-off SVG renders
# broken). 4000 was too low: a genuinely complete answer can run past it. The tutor path
# streams (no SDK HTTP-timeout concern) and the non-streaming fallback stays within the
# safe ~16K window; Sonnet 5 allows up to 64K output. Daily token caps still bound overall
# usage — this is only a per-reply ceiling, and typical replies are far shorter.
TUTOR_MAX_TOKENS = 16000

# --- Prompt caching (multi-turn tutor sessions) ---------------------------------
# The tutor prefix (tool defs → system → prior turns) is stable across a session, so
# we mark it with Anthropic ephemeral cache breakpoints: after turn 1 that prefix is
# served from cache at ~0.1x input price instead of full price. Block-level
# cache_control (not LiteLLM's top-level auto-cache) keeps this provider-flexible —
# it also works on Bedrock/Vertex.
# TTL: 1h by default — it keeps the prefix warm across the read-think-write pauses of
# subjects like creative writing (a 5m entry routinely expires between turns, and every
# expiry re-writes the whole prefix at the write premium; the 2x-vs-1.25x write premium
# pays for itself after the first pause it survives). PROMPT_CACHE_TTL=5m opts into
# the cheaper writes for consistently rapid-fire usage (quick math Q&A).
# Anything but an explicit "5m" gets the 1h default; validate_runtime warns at boot
# on an unrecognized value (soft misconfig — same pattern as the sandbox notice).
# Note: on Bedrock, LiteLLM maps cache_control to cachePoint blocks that carry no TTL,
# so "1h" silently degrades to that provider's default there (caching still works).
# Shared by reference into every request's content blocks — treat as immutable.
_CACHE_CONTROL: dict = {"type": "ephemeral", "ttl": "1h"}
if settings.prompt_cache_ttl == "5m":
    _CACHE_CONTROL = {"type": "ephemeral"}
# Daily-cap accounting weights cache traffic at its real price so the token cap tracks
# cost, not raw re-sent volume: cached READS bill at ~0.1x base input, and cache WRITES
# bill at a premium (1.25x for the 5m TTL, 2x for 1h) — both are folded into _charge.
# The message cap still bounds turn volume.
CACHE_READ_WEIGHT = 0.1
CACHE_WRITE_WEIGHT = 1.25 if settings.prompt_cache_ttl == "5m" else 2.0

VERDICTS = ("on_subject", "other_subject", "off_topic")


class ModelError(Exception):
    """Raised when a model call fails or returns unusable output (e.g. empty reply).
    The pipeline catches this and degrades gracefully instead of crashing."""

_GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "subject": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "subject", "reason"],
    "additionalProperties": False,
}


def _content(resp) -> str:
    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def _tokens(resp) -> int:
    usage = getattr(resp, "usage", None)
    if not usage:
        return 0
    total = getattr(usage, "total_tokens", 0) or 0
    if not total:  # some providers omit the aggregate — sum the parts
        total = (getattr(usage, "prompt_tokens", 0) or 0) + (getattr(usage, "completion_tokens", 0) or 0)
    return int(total)


def _cache_read_tokens(usage) -> int:
    """Anthropic cache-read tokens, however this LiteLLM version surfaces them
    (newer: `prompt_tokens_details.cached_tokens`; also the raw Anthropic fields).
    Returns 0 when nothing was served from cache (turn 1, or prefix below the min)."""
    if not usage:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached:
            return int(cached)
    for attr in ("cache_read_input_tokens", "_cache_read_input_tokens"):
        v = getattr(usage, attr, None)
        if v:
            return int(v)
    return 0


def _cache_creation_tokens(usage) -> int:
    """Tokens written to cache this request (turn 1, or when the prefix grew)."""
    if not usage:
        return 0
    for attr in ("cache_creation_input_tokens", "_cache_creation_input_tokens"):
        v = getattr(usage, attr, None)
        if v:
            return int(v)
    return 0


def _charge(total: int, cache_read: int, cache_creation: int = 0) -> int:
    """Tokens to charge against the daily cap: cached reads discounted to
    CACHE_READ_WEIGHT and cache writes up-weighted to CACHE_WRITE_WEIGHT, so the cap
    tracks real cost. Both read and written tokens are already part of `total` at
    raw count (LiteLLM includes cache_creation_input_tokens in prompt_tokens); we
    adjust each portion to its billed weight."""
    charged = total
    if cache_read > 0:
        charged -= int(round(cache_read * (1 - CACHE_READ_WEIGHT)))
    if cache_creation > 0:
        charged += int(round(cache_creation * (CACHE_WRITE_WEIGHT - 1)))
    return max(0, charged)


def _with_breakpoint(turn: dict) -> dict:
    """A copy of `turn` with a cache breakpoint on its LAST content block. Never
    mutates the caller's dict (history rows are reused across the request path)."""
    content = turn.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content, "cache_control": _CACHE_CONTROL}]
    elif isinstance(content, list) and content:
        blocks = [dict(b) for b in content]
        blocks[-1] = {**blocks[-1], "cache_control": _CACHE_CONTROL}
    else:  # empty/None content — nothing to cache
        return dict(turn)
    return {**turn, "content": blocks}


def _cached_messages(system: str, history: list[dict]) -> list[dict]:
    """Assemble the tutor request messages with (up to) two cache breakpoints:

    1. the system block — on the tools path tool defs render BEFORE system, so this
       one breakpoint caches tools+system together;
    2. the last PRIOR turn — so request N reuses turns 1..N-1 from cache.

    The brand-new user message (`history[-1]`) is left AFTER the last breakpoint: it
    differs every turn, so caching it would only cost a write with no read. Turn 1
    (history == just the new message) gets only the system breakpoint."""
    msgs: list[dict] = [{
        "role": "system",
        "content": [{"type": "text", "text": system, "cache_control": _CACHE_CONTROL}],
    }]
    last_prior = len(history) - 2  # last turn before the new user msg (-1 if none yet)
    for i, turn in enumerate(history):
        msgs.append(_with_breakpoint(turn) if i == last_prior and last_prior >= 0 else turn)
    return msgs


def run_gate(model: str, system: str, user_message: str,
             history: list[dict] | None = None) -> tuple[dict, int]:
    """Classify a student message. Returns ({verdict, subject, reason}, tokens).

    `history` is the recent on-subject dialogue (chat turns; user=student,
    assistant=tutor). Including it lets a short reply to the tutor's question
    ("equal?") be judged as a continuation rather than blocked in isolation.

    Fails CLOSED: if the model can't produce a valid verdict after a retry, returns
    verdict='error' so the pipeline withholds the tutor rather than guessing.
    """
    context = ""
    if history:
        lines = []
        for h in history[-6:]:
            who = "Student" if h["role"] == "user" else "Tutor"
            snippet = h["content"].strip()
            lines.append(f"{who}: {snippet[:600] + '…' if len(snippet) > 600 else snippet}")
        context = "Recent conversation in this subject (context only):\n" + "\n".join(lines) + "\n\n"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{context}New message from the student to classify:\n{user_message}"},
    ]
    tokens = 0
    last_error: Optional[Exception] = None
    for _ in range(2):
        try:
            resp = litellm.completion(
                model=model,
                messages=messages,
                max_tokens=300,
                timeout=30,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "gate_verdict", "schema": _GATE_SCHEMA, "strict": True},
                },
            )
        except Exception as e:  # noqa: BLE001 — provider/transport failure; retry, then fail closed
            last_error = e
            continue
        tokens += _tokens(resp)
        try:
            data = json.loads(_content(resp))
        except (json.JSONDecodeError, TypeError):
            last_error = None  # the model WAS reachable; its output was just unusable
            continue
        if isinstance(data, dict) and data.get("verdict") in VERDICTS:
            subject = data.get("subject")
            return {
                "verdict": data["verdict"],
                "subject": subject if isinstance(subject, str) else None,
                "reason": str(data.get("reason") or ""),
                "error_kind": None,
            }, tokens
    # Fail closed — but distinguish a genuine can't-classify from a model-call failure
    # (bad key, provider down, model rejected), so the pipeline can tell the student the
    # right thing and the admin/logs get the actual cause instead of a vague notice.
    if last_error is not None:
        detail = f"{type(last_error).__name__}: {last_error}"
        print(f"ERROR [praeceptor]: gate model call failed — {detail}", file=sys.stderr, flush=True)
        return {"verdict": "error", "subject": None, "error_kind": "transport",
                "reason": f"gate model call failed ({detail[:300]})"}, tokens
    return {"verdict": "error", "subject": None, "error_kind": "unclassified",
            "reason": "gate could not classify the message"}, tokens


def run_tutor(model: str, system: str, history: list[dict]) -> tuple[str, int]:
    """Generate a tutor reply. `history` is chat-format turns (user/assistant), the
    last of which is the student's current message. Returns (reply_text, tokens).

    No `temperature` is sent: some Claude models reject it (and `drop_params` guards
    the rest). Raises ModelError on a provider/transport failure or an empty reply, so the
    pipeline never persists a phantom tutor turn.
    """
    try:
        resp = litellm.completion(
            model=model,
            messages=_cached_messages(system, history),
            max_tokens=TUTOR_MAX_TOKENS,
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001 — normalize any provider error
        raise ModelError(f"tutor request failed: {e}") from e
    reply = _content(resp).strip()
    if not reply:
        raise ModelError("tutor returned an empty reply")
    usage = getattr(resp, "usage", None)
    return reply, _charge(_usage_tokens(usage), _cache_read_tokens(usage),
                          _cache_creation_tokens(usage))


def _usage_tokens(usage) -> int:
    if not usage:
        return 0
    total = getattr(usage, "total_tokens", 0) or 0
    if not total:
        total = (getattr(usage, "prompt_tokens", 0) or 0) + (getattr(usage, "completion_tokens", 0) or 0)
    return int(total or 0)


async def run_tutor_stream(model: str, system: str, history: list[dict]):
    """Stream a tutor reply token-by-token. Async generator that yields each text
    delta as a `str`, then finally yields a single meta `dict` so the caller can
    charge usage and diagnose the turn:

        {"tokens": int,            # total (0 if the provider didn't report it)
         "prompt_tokens": int|None, "completion_tokens": int|None,
         "finish_reason": str|None, # "stop" | "length" | … from the last chunk
         "partial": bool}          # True if the stream broke mid-reply

    Backward-compatible: existing callers only read `.get("tokens")`; the extra keys
    are additive. Same contract as `run_tutor`: no `temperature` (some models reject
    it). Raises ModelError if the call fails before producing ANY content, so the
    caller can fall back to the friendly error notice. If the stream breaks AFTER
    some content has been delivered, it stops gracefully with what was received
    (the partial is real text the student already saw) and reports `partial: True`.
    """
    try:
        stream = await litellm.acompletion(
            model=model,
            messages=_cached_messages(system, history),
            max_tokens=TUTOR_MAX_TOKENS,
            timeout=60,
            stream=True,
            # Ask the provider to include token usage in the final stream chunk.
            stream_options={"include_usage": True},
        )
    except Exception as e:  # noqa: BLE001 — normalize any provider error
        raise ModelError(f"tutor request failed: {e}") from e

    tokens = 0
    prompt_tokens = None
    completion_tokens = None
    cache_read = 0
    cache_creation = 0
    finish_reason = None
    got_content = False
    partial = False
    try:
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                t = _usage_tokens(usage)
                if t:
                    tokens = t
                p = getattr(usage, "prompt_tokens", None)
                c = getattr(usage, "completion_tokens", None)
                if p:
                    prompt_tokens = int(p)
                if c:
                    completion_tokens = int(c)
                cr = _cache_read_tokens(usage)
                if cr:
                    cache_read = cr
                cc = _cache_creation_tokens(usage)
                if cc:
                    cache_creation = cc
            try:
                choice = chunk.choices[0]
            except (AttributeError, IndexError):
                choice = None
            if choice is not None:
                fr = getattr(choice, "finish_reason", None)
                if fr:
                    finish_reason = fr
            try:
                delta = choice.delta.content if choice is not None else None
            except AttributeError:
                delta = None
            if delta:
                got_content = True
                yield delta
    except Exception as e:  # noqa: BLE001
        # Mid-stream failure: surface it only if nothing was delivered; otherwise
        # keep the partial reply the student already saw and flag it as partial.
        if not got_content:
            raise ModelError(f"tutor stream failed: {e}") from e
        partial = True

    if not got_content:
        raise ModelError("tutor returned an empty reply")
    yield {
        # `tokens` is what the daily cap is charged: total with cached reads discounted
        # and cache writes up-weighted to their billed premium.
        "tokens": _charge(tokens, cache_read, cache_creation),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "finish_reason": finish_reason,
        "partial": partial,
    }


# A small chunk so the client still animates text that arrives a whole round at a time
# (the agentic path resolves tool rounds non-streaming, then emits each text block).
_TEXT_CHUNK = 120

# Cap figures embedded per turn (matches the sandbox cap; bounds reply size).
_MAX_FIGURES = 6


def _chunk_text(text: str):
    for i in range(0, len(text), _TEXT_CHUNK):
        yield text[i:i + _TEXT_CHUNK]


def _emit_pieces(prior: list[str], content: str) -> list[str]:
    """Chunks for one round's text, with a single separating space if the previous
    round's text ran right up against this one (a tool call interrupted mid-thought,
    so the blocks would otherwise fuse like 'for us:So')."""
    pieces: list[str] = []
    if prior and content and not prior[-1][-1:].isspace() and not content[:1].isspace():
        pieces.append(" ")
    pieces.extend(_chunk_text(content))
    return pieces


def _assistant_msg(content: str, tool_calls) -> dict:
    """Rebuild the assistant turn (text + tool calls) to append to the running messages
    list, in the OpenAI tool-call shape LiteLLM translates per provider."""
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in tool_calls
        ],
    }


async def run_tutor_tools_stream(
    model: str, system: str, history: list[dict],
    tools: list[dict], tool_executor: Callable[[str, dict], Awaitable[dict]],
):
    """Agentic tutor turn WITH tools. Yields the same shapes as `run_tutor_stream`
    (str text deltas, then a final meta dict), plus `{"status": ..., "tool": ...}`
    dicts while a tool runs so the UI can show activity.

    Tool rounds are resolved with non-streaming calls (assembling streamed tool-call
    arguments across providers is fragile); each round's text is emitted in chunks so
    the bubble still fills progressively. After at most `settings.tool_max_rounds`, a
    final tools-disabled call guarantees a worded answer instead of an endless loop.

    Raises ModelError if the FIRST call fails before any content (so the caller shows
    the friendly error); a later failure stops gracefully with `partial: True`.
    `tool_executor(name, args)` runs the tool (off the event loop) and returns a
    JSON-serializable dict that becomes the tool result the model sees.
    """
    messages = _cached_messages(system, history)
    reply_parts: list[str] = []
    tool_log: list[dict] = []
    tokens = prompt_tokens = completion_tokens = 0
    cache_read = cache_creation = 0
    finish_reason = None
    partial = False
    max_rounds = max(1, settings.tool_max_rounds)

    async def _complete(use_tools: bool, first: bool):
        nonlocal tokens, prompt_tokens, completion_tokens, finish_reason
        nonlocal cache_read, cache_creation
        kwargs = dict(model=model, messages=messages, max_tokens=TUTOR_MAX_TOKENS, timeout=90)
        if use_tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            resp = await litellm.acompletion(**kwargs)
        except Exception as e:  # noqa: BLE001
            if first:
                raise ModelError(f"tutor request failed: {e}") from e
            return None
        try:
            choice = resp.choices[0]
            msg = choice.message
            finish_reason = getattr(choice, "finish_reason", finish_reason)
        except (AttributeError, IndexError):
            return None
        usage = getattr(resp, "usage", None)
        if usage:
            tokens += _usage_tokens(usage)
            prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            cache_read += _cache_read_tokens(usage)
            cache_creation += _cache_creation_tokens(usage)
        return msg

    for round_i in range(max_rounds):
        msg = await _complete(use_tools=True, first=(round_i == 0))
        if msg is None:
            partial = bool(reply_parts)
            break
        content = (getattr(msg, "content", None) or "")
        tool_calls = list(getattr(msg, "tool_calls", None) or [])
        if content:
            for piece in _emit_pieces(reply_parts, content):
                yield piece
            reply_parts.append(content)
        if not tool_calls:
            break

        # Execute the round's tool calls. Guard the whole block: a malformed provider
        # tool_call object (missing .id/.function) must degrade to a graceful partial
        # stop, not escape as an uncaught 500 mid-stream.
        try:
            messages.append(_assistant_msg(content, tool_calls))
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                yield {"status": "tool", "tool": name}
                result = await tool_executor(name, args)
                # Figures are shown to the student, not sent to the model: pull them out
                # and embed each as a ```svg fence in the reply (the render layer turns
                # that into a sanitized inline figure, and it persists for history).
                figures = result.pop("figures", None) if isinstance(result, dict) else None
                tool_log.append({"tool": name, "ok": bool(result.get("ok")),
                                 "timeout": bool(result.get("timeout"))})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result)})
                if figures:
                    # ```svgfig is the TRUSTED, server-generated figure channel (distinct
                    # from the model's strict ```svg) — see app/render.py.
                    for svg in figures[:_MAX_FIGURES]:
                        if isinstance(svg, str) and svg.strip():
                            reply_parts.append(f"\n\n```svgfig\n{svg.strip()}\n```\n\n")
        except Exception:  # noqa: BLE001 — never let a tool round crash the stream
            partial = True
            finish_reason = finish_reason or "tool_error"
            break
    else:
        # Rounds exhausted with tool calls still pending — force a worded wrap-up with
        # tools disabled so we never end on an unanswered tool round.
        finish_reason = "max_tool_rounds"
        msg = await _complete(use_tools=False, first=False)
        if msg is not None:
            content = (getattr(msg, "content", None) or "")
            if content:
                for piece in _emit_pieces(reply_parts, content):
                    yield piece
                reply_parts.append(content)
        partial = partial or not reply_parts

    # Charge the daily cap: discount cached reads and up-weight cache writes so it
    # tracks real cost (each round re-sends a growing context, but the stable prefix
    # is served from cache). Fall back to a size estimate ONLY when the provider
    # reported no usage at all — a full-weight estimate must not override the
    # intentional cache weighting.
    if tokens > 0:
        tokens = _charge(tokens, cache_read, cache_creation)
    else:
        tokens = _estimate_tokens(messages) + max(1, len("".join(reply_parts)) // 4)
    yield {
        "tokens": tokens,
        "prompt_tokens": prompt_tokens or None,
        "completion_tokens": completion_tokens or None,
        "cache_read": cache_read or None,
        "cache_creation": cache_creation or None,
        "finish_reason": finish_reason,
        "partial": partial,
        "tool_rounds": len(tool_log),
        "tool_log": tool_log,
        # Full reply INCLUDING embedded ```svg figure fences (the streamed text deltas
        # omit them). Consumers render/persist this so figures survive a history reload.
        "reply": "".join(reply_parts),
    }


def _estimate_tokens(messages: list[dict]) -> int:
    """~4 chars/token over the full message payload — a floor for usage when the
    provider didn't report it on the tools path."""
    try:
        return max(1, len(json.dumps(messages, default=str)) // 4)
    except Exception:
        return 1
