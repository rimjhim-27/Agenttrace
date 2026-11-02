"""AgentTrace - record, replay and debug AI agents.

Typical use::

    import agenttrace as at

    at.init("traces.db")

    @at.trace(kind="llm")
    def call_llm(prompt): ...

    @at.trace(kind="agent")
    def my_agent(question): ...

    with at.run("support-bot") as r:
        my_agent("hello")

    with at.replay(r.trace_id) as rs:   # re-run the same code, no real LLM/tool calls
        my_agent(*rs.root_input["args"])
"""
__version__ = "0.1.0"

from .costs import clear_prices, set_price
from .differ import SpanDiff, TraceDiff, diff_traces
from .insights import Insight, analyze
from .models import Span
from .replayer import Divergence, RecordedError, ReplayMismatch, ReplaySession, replay
from .storage import SQLiteStore
from .tracer import (
    SpanHandle,
    Tracer,
    close,
    current_span,
    flush,
    get_spans,
    get_trace,
    get_tracer,
    init,
    run,
    span,
    trace,
)

__all__ = [
    "__version__",
    "init", "trace", "run", "span", "current_span", "flush", "close", "get_tracer", "get_spans", "get_trace",
    "replay", "diff_traces", "analyze", "set_price", "clear_prices",
    "Tracer", "SQLiteStore", "Span", "SpanHandle", "Insight", "SpanDiff", "TraceDiff",
    "ReplaySession", "Divergence", "ReplayMismatch", "RecordedError",
]
