"""Deterministic replay.

``with replay(trace_id) as rs: my_agent(...)`` re-runs *your agent code* while every
``llm`` and ``tool`` call is answered from the recording instead of being executed.
Your control flow (prompts, parsing, branching, retries) runs for real, so a bug in
that code reproduces exactly and cheaply: no API calls, no side effects, no cost.

Matching rule: the n-th call of ``(kind, name)`` during replay gets the n-th recorded
call of ``(kind, name)``. Differences are never hidden; they are collected in
``session.divergences``:

* ``input_changed``     - the call received different input than when it was recorded
* ``missing_recording`` - the code made more calls than the recording contains
* ``unused_recording``  - the recording has calls the code did not make this time

What replay does NOT control: randomness, wall-clock time, environment, and any side
effect inside ``agent``/``custom`` spans. Recorded values must be JSON-able.
"""
from __future__ import annotations

import builtins
import copy
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import context as ctx
from .models import REPLAYABLE_KINDS, Span
from .serialize import canonical
from .tracer import Tracer, get_tracer


class ReplayMismatch(Exception):
    """The code under replay no longer matches the recording (strict mode)."""


class RecordedError(Exception):
    """Raised during replay when the original call failed with a non-builtin exception type."""


@dataclass
class Divergence:
    kind: str  # input_changed | missing_recording | unused_recording
    span_kind: str
    name: str
    nth: int  # 1-based occurrence of (span_kind, name)
    detail: str


def _raise_recorded(error: str) -> None:
    """Re-raise a recorded error, using the original builtin type when possible.

    Keeping the real type matters: ``except TimeoutError:`` in the agent must behave
    during replay exactly as it did in production.
    """
    type_name, _, message = error.partition(": ")
    cls = getattr(builtins, type_name, None)
    if isinstance(cls, type) and issubclass(cls, Exception):
        try:
            exc = cls(message)
        except Exception:
            exc = None
        if exc is not None:
            raise exc
    raise RecordedError(error)


class ReplaySession:
    def __init__(self, recorded: List[Span], *, strict: bool = True,
                 check_inputs: bool = True, fail_on_input_change: bool = False) -> None:
        self.strict = strict
        self.check_inputs = check_inputs
        self.fail_on_input_change = fail_on_input_change
        self.divergences: List[Divergence] = []
        self.served = 0
        self.trace_id: Optional[str] = None  # id of the *new* trace written by this replay
        self._recorded = recorded
        self._by_key: Dict[Tuple[str, str], List[Span]] = defaultdict(list)
        for s in sorted(recorded, key=lambda x: x.seq):
            if s.kind in REPLAYABLE_KINDS:  # agent/custom spans are the code under test: they run for real
                self._by_key[(s.kind, s.name)].append(s)
        self._cursor: Dict[Tuple[str, str], int] = defaultdict(int)

    @property
    def ok(self) -> bool:
        return not self.divergences

    @property
    def root_input(self) -> Optional[Dict[str, Any]]:
        """``{"args": [...], "kwargs": {...}}`` the recorded agent was started with."""
        roots = sorted((s for s in self._recorded if s.parent_id is None), key=lambda s: s.seq)
        return roots[0].input if roots else None

    def serve(self, name: str, kind: str, inp: Any, handle: Any) -> Any:
        key = (kind, name)
        i = self._cursor[key]
        self._cursor[key] = i + 1
        candidates = self._by_key.get(key, [])
        if i >= len(candidates):
            d = Divergence("missing_recording", kind, name, i + 1,
                           f"{kind} '{name}' call #{i + 1} has no recording")
            self.divergences.append(d)
            if self.strict:
                raise ReplayMismatch(d.detail)
            return ctx.MISS  # non-strict: let the real function run
        rec = candidates[i]
        if self.check_inputs and rec.input is not None and inp is not None \
                and canonical(rec.input) != canonical(inp):
            d = Divergence("input_changed", kind, name, i + 1,
                           f"{kind} '{name}' call #{i + 1} received different input than recorded")
            self.divergences.append(d)
            if self.fail_on_input_change:
                raise ReplayMismatch(d.detail)
        handle.set_attr("replayed", True)
        handle.set_attr("recorded_span_id", rec.span_id)
        if rec.error:
            _raise_recorded(rec.error)
        self.served += 1
        return copy.deepcopy(rec.output)

    def finalize(self) -> None:
        for key, spans in self._by_key.items():
            for j in range(self._cursor[key], len(spans)):
                self.divergences.append(Divergence(
                    "unused_recording", key[0], key[1], j + 1,
                    f"recorded {key[0]} '{key[1]}' call #{j + 1} was not made during replay"))


@contextmanager
def replay(trace_id: str, *, tracer: Optional[Tracer] = None, strict: bool = True,
           check_inputs: bool = True, fail_on_input_change: bool = False):
    """Replay a recorded trace. Yields a :class:`ReplaySession`; a new trace (``replay_of=trace_id``) is written."""
    tr = tracer or get_tracer()
    if not tr.enabled:
        raise RuntimeError("cannot replay while the tracer is disabled")
    recorded = tr.get_spans(trace_id)
    if not recorded:
        raise KeyError(f"no recorded spans for trace {trace_id!r}")
    original = tr.get_trace(trace_id) or {}
    session = ReplaySession(recorded, strict=strict, check_inputs=check_inputs,
                            fail_on_input_change=fail_on_input_change)
    token = ctx.active_replay.set(session)
    try:
        with tr.run(f"replay:{original.get('name', trace_id)}", replay_of=trace_id) as state:
            session.trace_id = state.trace_id
            yield session
        session.finalize()
    finally:
        ctx.active_replay.reset(token)
