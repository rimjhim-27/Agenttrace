"""Run-vs-run diff: *where* did two executions of the same agent start to differ?

Spans are aligned by ``(kind, name, n-th occurrence)``, not by position, so an extra
retry in the middle does not make every later span look "changed".
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import Span
from .serialize import canonical


@dataclass
class SpanDiff:
    status: str  # same | changed | added | removed
    kind: str
    name: str
    nth: int  # 1-based occurrence of (kind, name)
    changes: List[str] = field(default_factory=list)  # semantic differences
    notes: List[str] = field(default_factory=list)  # timing observations
    a: Optional[Span] = None
    b: Optional[Span] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "kind": self.kind, "name": self.name, "nth": self.nth,
            "changes": self.changes, "notes": self.notes,
            "a_span_id": self.a.span_id if self.a else None,
            "b_span_id": self.b.span_id if self.b else None,
        }


@dataclass
class TraceDiff:
    entries: List[SpanDiff]
    a_total_ms: float
    b_total_ms: float
    a_tokens: int
    b_tokens: int

    @property
    def first_divergence(self) -> Optional[SpanDiff]:
        return next((e for e in self.entries if e.status != "same"), None)

    def summary(self) -> Dict[str, Any]:
        counts = {"same": 0, "changed": 0, "added": 0, "removed": 0}
        for e in self.entries:
            counts[e.status] += 1
        fd = self.first_divergence
        return {
            **counts,
            "identical": counts["changed"] + counts["added"] + counts["removed"] == 0,
            "duration_delta_ms": round(self.b_total_ms - self.a_total_ms, 3),
            "tokens_delta": self.b_tokens - self.a_tokens,
            "first_divergence": None if fd is None else {"kind": fd.kind, "name": fd.name, "nth": fd.nth, "status": fd.status},
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary(), "entries": [e.to_dict() for e in self.entries]}


def _index(spans: List[Span]) -> Dict[Tuple[str, str, int], Span]:
    seen: Dict[Tuple[str, str], int] = defaultdict(int)
    out: Dict[Tuple[str, str, int], Span] = {}
    for s in sorted(spans, key=lambda x: x.seq):
        k = (s.kind, s.name)
        out[(s.kind, s.name, seen[k])] = s
        seen[k] += 1
    return out


def _compare(a: Span, b: Span, slow_threshold_ms: float, slow_ratio: float) -> Tuple[List[str], List[str]]:
    changes: List[str] = []
    notes: List[str] = []
    if canonical(a.input) != canonical(b.input):
        changes.append("input differs")
    if canonical(a.output) != canonical(b.output):
        changes.append("output differs")
    if a.error != b.error:
        changes.append(f"error differs ({a.error or 'none'} -> {b.error or 'none'})")
    if a.tokens != b.tokens:
        changes.append(f"tokens {a.tokens} -> {b.tokens}")
    delta = b.duration_ms - a.duration_ms
    if abs(delta) >= slow_threshold_ms and abs(delta) >= slow_ratio * max(a.duration_ms, 1e-9):
        notes.append(f"{'slower' if delta > 0 else 'faster'} by {abs(delta):.0f} ms")
    return changes, notes


def diff_traces(a_spans: List[Span], b_spans: List[Span], *,
                slow_threshold_ms: float = 100.0, slow_ratio: float = 0.5) -> TraceDiff:
    ia, ib = _index(a_spans), _index(b_spans)
    entries: List[SpanDiff] = []
    for key in set(ia) | set(ib):
        a, b = ia.get(key), ib.get(key)
        kind, name, n = key
        if a is not None and b is None:
            entries.append(SpanDiff("removed", kind, name, n + 1, ["only in A"], [], a, None))
        elif b is not None and a is None:
            entries.append(SpanDiff("added", kind, name, n + 1, ["only in B"], [], None, b))
        else:
            changes, notes = _compare(a, b, slow_threshold_ms, slow_ratio)  # type: ignore[arg-type]
            entries.append(SpanDiff("changed" if changes else "same", kind, name, n + 1, changes, notes, a, b))
    entries.sort(key=lambda e: (e.a.seq if e.a else e.b.seq, e.b.seq if e.b else -1))  # type: ignore[union-attr]

    def total(spans: List[Span]) -> float:
        return sum(s.duration_ms for s in spans if s.parent_id is None)

    return TraceDiff(
        entries=entries,
        a_total_ms=total(a_spans), b_total_ms=total(b_spans),
        a_tokens=sum(s.tokens for s in a_spans), b_tokens=sum(s.tokens for s in b_spans),
    )
