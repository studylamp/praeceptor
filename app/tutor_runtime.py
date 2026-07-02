"""Glue between the routers and the inference layer for the tutor turn: pick the
plain streaming path or the agentic tools path, and provide the tool executor.

Kept out of model_client so that module stays provider-agnostic and unaware of the
sandbox/tools; kept out of the routers so the student and admin-test paths share one
copy of the decision and the executor wiring.
"""

from starlette.concurrency import run_in_threadpool

from app import model_client, tools
from app.config import settings


def tools_active(subject) -> bool:
    """True when tools are on globally AND enabled for this subject."""
    if not settings.tools_enabled:
        return False
    try:
        return bool(subject["tools_enabled"])
    except (KeyError, IndexError):  # pre-migration row missing the column
        return False


async def _tool_executor(name: str, args: dict) -> dict:
    # The sandbox spawns a subprocess (blocking) — keep it off the event loop.
    return await run_in_threadpool(tools.run_tool, name, args)


def tutor_stream(model: str, system: str, history: list[dict], *, use_tools: bool):
    """Return the appropriate async generator. Both yield the same shapes: str text
    deltas, then a final meta dict; the tools path may also yield `{"status":...}`."""
    if use_tools:
        return model_client.run_tutor_tools_stream(
            model, system, history, tools.tool_specs(), _tool_executor)
    return model_client.run_tutor_stream(model, system, history)
