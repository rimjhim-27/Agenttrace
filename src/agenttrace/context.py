"""Context variables that carry "where are we right now" through sync and async code.

``contextvars`` (not thread-locals) are used so that tracing keeps working across
``await`` points and ``asyncio`` tasks. Note: plain ``threading.Thread`` does *not*
inherit the context; use ``contextvars.copy_context().run`` or ``asyncio.to_thread``.
"""
from __future__ import annotations

from contextvars import ContextVar


class _Miss:
    """Sentinel: "the replay session has no recording for this call"."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<MISS>"


MISS = _Miss()

current_trace: ContextVar = ContextVar("agenttrace_current_trace", default=None)
current_span: ContextVar = ContextVar("agenttrace_current_span", default=None)
active_replay: ContextVar = ContextVar("agenttrace_active_replay", default=None)
