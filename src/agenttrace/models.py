"""Core data model.

A *trace* (a "run") is one execution of an agent. It contains *spans*: one
timed step each (the agent itself, an LLM call, a tool call, ...). Spans form
a tree through ``parent_id`` and are totally ordered inside a trace by ``seq``.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

SPAN_KINDS = ("agent", "llm", "tool", "custom")

# Only these kinds are served from a recording during replay. "agent" and
# "custom" spans are the code under test, so they always execute for real.
REPLAYABLE_KINDS = frozenset({"llm", "tool"})


def new_id() -> str:
    """64-bit random id as 16 hex chars (collision-safe enough for traces)."""
    return uuid.uuid4().hex[:16]


@dataclass(slots=True)
class Span:
    span_id: str
    trace_id: str
    name: str
    kind: str = "custom"
    parent_id: Optional[str] = None
    seq: int = 0  # order of span *start* inside its trace
    start_ts: float = 0.0  # epoch seconds
    end_ts: float = 0.0
    duration_ms: float = 0.0
    input: Any = None  # JSON-able snapshot taken when the span started
    output: Any = None  # JSON-able snapshot taken when the span ended
    error: Optional[str] = None  # "ExceptionType: message" if the span failed
    attrs: Dict[str, Any] = field(default_factory=dict)  # tokens_in, tokens_out, model, cost_usd, replayed, ...

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Span":
        return cls(
            span_id=d["span_id"],
            trace_id=d["trace_id"],
            name=d["name"],
            kind=d.get("kind", "custom"),
            parent_id=d.get("parent_id"),
            seq=int(d.get("seq", 0)),
            start_ts=float(d.get("start_ts", 0.0)),
            end_ts=float(d.get("end_ts", 0.0)),
            duration_ms=float(d.get("duration_ms", 0.0)),
            input=d.get("input"),
            output=d.get("output"),
            error=d.get("error"),
            attrs=dict(d.get("attrs") or {}),
        )

    @property
    def tokens(self) -> int:
        return int(self.attrs.get("tokens_in") or 0) + int(self.attrs.get("tokens_out") or 0)
