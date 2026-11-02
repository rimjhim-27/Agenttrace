"""The tracing SDK: ``@trace`` decorator, ``span`` / ``run`` context managers and
the background writer that moves spans from the application into storage.

Design in one paragraph: the hot path (inside the user's code) only builds a
small ``Span`` object, snapshots input/output to JSON-able values, and does a
non-blocking ``queue.put_nowait``. A single daemon thread drains the queue in
batches and writes each batch in one transaction. If the queue is full the span
is *dropped and counted* instead of blocking the agent (see ``stats()``).
"""
from __future__ import annotations

import atexit
import functools
import inspect
import itertools
import logging
import os
import queue
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional

from . import context as ctx
from .costs import estimate_cost, extract_usage
from .models import REPLAYABLE_KINDS, SPAN_KINDS, Span, new_id
from .serialize import DEFAULT_MAX_CHARS, to_jsonable
from .storage import SQLiteStore

log = logging.getLogger("agenttrace")
_STOP = object()


# --------------------------------------------------------------------------- handles
class SpanHandle:
    """What ``with span(...) as sp`` gives you: annotate the live span."""

    __slots__ = ("span", "_tracer")

    def __init__(self, span: Span, tracer: "Tracer") -> None:
        self.span = span
        self._tracer = tracer

    @property
    def span_id(self) -> str:
        return self.span.span_id

    @property
    def trace_id(self) -> str:
        return self.span.trace_id

    def set_attr(self, key: str, value: Any) -> None:
        self.span.attrs[key] = value

    def set_output(self, value: Any) -> None:
        self.span.output = self._tracer._prep(value)

    def set_tokens(self, tokens_in: int, tokens_out: int, model: Optional[str] = None) -> None:
        a = self.span.attrs
        a["tokens_in"], a["tokens_out"] = int(tokens_in), int(tokens_out)
        if model:
            a["model"] = model
            cost = estimate_cost(model, int(tokens_in), int(tokens_out))
            if cost is not None:
                a["cost_usd"] = cost


class _NullHandle:
    """Returned when tracing is disabled or no span is active: every call is a no-op."""

    span = None
    span_id = None
    trace_id = None

    def set_attr(self, key: str, value: Any) -> None: ...
    def set_output(self, value: Any) -> None: ...
    def set_tokens(self, tokens_in: int, tokens_out: int, model: Optional[str] = None) -> None: ...


_NULL = _NullHandle()


class TraceState:
    """One live run. ``trace_id`` is the handle you use to query / replay it later."""

    __slots__ = ("tracer", "trace_id", "name", "start_ts", "end_ts", "attrs", "replay_of", "_seq")

    def __init__(self, tracer: "Tracer", name: str, replay_of: Optional[str], attrs: Dict[str, Any]) -> None:
        self.tracer = tracer
        self.trace_id = new_id()
        self.name = name
        self.start_ts = time.time()
        self.end_ts: Optional[float] = None
        self.attrs = attrs
        self.replay_of = replay_of
        self._seq = itertools.count()

    def next_seq(self) -> int:
        return next(self._seq)

    def as_row(self, status: str) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "status": status,
            "replay_of": self.replay_of,
            "attrs": to_jsonable(self.attrs),
        }


class _SpanCtx:
    """Class-based context manager (cheaper than a generator-based one on the hot path)."""

    __slots__ = ("tracer", "name", "kind", "input", "handle", "_token", "_t0", "_implicit")

    def __init__(self, tracer: "Tracer", name: str, kind: str, inp: Any) -> None:
        self.tracer, self.name, self.kind, self.input = tracer, name, kind, inp
        self.handle: Any = _NULL
        self._implicit = None

    def __enter__(self) -> Any:
        tr = self.tracer
        if not tr.enabled:
            return _NULL
        state = ctx.current_trace.get()
        if state is None:  # no run() active -> start an implicit one named after this span
            self._implicit = tr.run(self.name)
            state = self._implicit.__enter__()
        parent = ctx.current_span.get()
        sp = Span(
            span_id=new_id(),
            trace_id=state.trace_id,
            name=self.name,
            kind=self.kind,
            parent_id=parent.span.span_id if parent is not None else None,
            seq=state.next_seq(),
            start_ts=time.time(),
            input=self.input,
        )
        self.handle = SpanHandle(sp, tr)
        self._token = ctx.current_span.set(self.handle)
        self._t0 = time.perf_counter()
        return self.handle

    def __exit__(self, et, ev, tb) -> bool:
        h = self.handle
        if h is _NULL:
            return False
        sp = h.span
        dur_ms = (time.perf_counter() - self._t0) * 1000.0
        sp.duration_ms = dur_ms
        sp.end_ts = sp.start_ts + dur_ms / 1000.0
        if et is not None:
            sp.error = f"{et.__name__}: {ev}"
        ctx.current_span.reset(self._token)
        self.tracer._emit("span", sp)
        if self._implicit is not None:
            self._implicit.__exit__(et, ev, tb)
        return False


# --------------------------------------------------------------------------- tracer
class Tracer:
    """Owns a store, a bounded queue and one writer thread.

    Parameters
    ----------
    store:            where spans end up (``SQLiteStore`` by default in ``init``).
    max_queue:        bound of the in-memory queue; when full, spans are dropped (never blocks the agent).
    batch_size:       max items the writer commits in a single transaction.
    max_payload_chars: strings in captured input/output are truncated to this length.
    redact:           optional ``fn(jsonable) -> jsonable`` applied to every captured payload
                      (use it to mask API keys / PII *before* anything is stored).
    enabled:          ``False`` turns every call into a cheap no-op.
    """

    def __init__(
        self,
        store: Any,
        *,
        max_queue: int = 100_000,
        batch_size: int = 512,
        max_payload_chars: int = DEFAULT_MAX_CHARS,
        redact: Optional[Callable[[Any], Any]] = None,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.batch_size = batch_size
        self.max_payload_chars = max_payload_chars
        self.redact = redact
        self._q: "queue.Queue[Any]" = queue.Queue(maxsize=max_queue)
        self._enqueued = 0
        self._written = 0
        self._dropped = 0
        self._write_errors = 0
        self._closed = False
        self._thread = threading.Thread(target=self._writer_loop, name="agenttrace-writer", daemon=True)
        self._thread.start()

    # ---- public API -------------------------------------------------------
    @contextmanager
    def run(self, name: str, *, replay_of: Optional[str] = None, **attrs: Any):
        """Start a trace. Everything traced inside belongs to it. Yields a ``TraceState``."""
        state = TraceState(self, name, replay_of, attrs)
        if not self.enabled:
            yield state
            return
        t_tok = ctx.current_trace.set(state)
        s_tok = ctx.current_span.set(None)
        self._emit("trace", state.as_row("running"))
        status = "ok"
        try:
            yield state
        except BaseException:
            status = "error"
            raise
        finally:
            ctx.current_span.reset(s_tok)
            ctx.current_trace.reset(t_tok)
            state.end_ts = time.time()
            self._emit("trace", state.as_row(status))

    def span(self, name: str, kind: str = "custom", input: Any = None) -> _SpanCtx:
        """``with tracer.span("step", kind="tool") as sp: ...`` for code you cannot decorate."""
        if kind not in SPAN_KINDS:
            raise ValueError(f"kind must be one of {SPAN_KINDS}, got {kind!r}")
        return _SpanCtx(self, name, kind, None if input is None else self._prep(input))

    def trace(self, fn: Optional[Callable] = None, *, name: Optional[str] = None, kind: str = "custom",
              capture_input: bool = True, capture_output: bool = True):
        """Decorator for sync and async functions (``@tracer.trace`` or ``@tracer.trace(kind="tool")``)."""
        return _make_decorator(lambda: self, fn, name, kind, capture_input, capture_output)

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        self.flush()
        return self.store.get_trace(trace_id)

    def get_spans(self, trace_id: str) -> List[Span]:
        self.flush()
        return self.store.get_spans(trace_id)

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until everything enqueued so far is written. Returns ``False`` on timeout."""
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.001)
        return True

    def stats(self) -> Dict[str, int]:
        return {
            "enqueued": self._enqueued,
            "written": self._written,
            "dropped": self._dropped,
            "write_errors": self._write_errors,
            "queue_size": self._q.qsize(),
        }

    def close(self, timeout: float = 10.0) -> None:
        """Flush and stop the writer thread (the store stays open)."""
        if self._closed:
            return
        self.flush(timeout)
        self._closed = True
        self._q.put(_STOP)
        self._thread.join(timeout)

    # ---- internals --------------------------------------------------------
    def _prep(self, value: Any) -> Any:
        snap = to_jsonable(value, self.max_payload_chars)
        if self.redact is not None:
            try:
                snap = self.redact(snap)
            except Exception:  # fail closed: never store what we could not redact
                snap = "<redaction failed>"
        return snap

    def _capture_input(self, args: tuple, kwargs: dict, skip_first: bool) -> Any:
        if skip_first and args:
            args = args[1:]
        return self._prep({"args": list(args), "kwargs": kwargs})

    def _record_output(self, handle: SpanHandle, out: Any, kind: str) -> None:
        handle.span.output = self._prep(out)
        if kind == "llm":
            usage = extract_usage(out)
            if usage is not None:
                handle.set_tokens(usage[0], usage[1], usage[2])

    def _emit(self, kind: str, payload: Any) -> None:
        if self._closed:
            return
        try:
            self._q.put_nowait((kind, payload))
            self._enqueued += 1
        except queue.Full:
            self._dropped += 1

    def _writer_loop(self) -> None:
        q = self._q
        while True:
            first = q.get()
            batch: List[Any] = []
            stop = first is _STOP
            if not stop:
                batch.append(first)
            while not stop and len(batch) < self.batch_size:
                try:
                    nxt = q.get_nowait()
                except queue.Empty:
                    break
                if nxt is _STOP:
                    stop = True
                    break
                batch.append(nxt)
            if batch:
                try:
                    self.store.apply(batch)
                    self._written += len(batch)
                except Exception:
                    self._write_errors += 1
                    log.exception("agenttrace: failed to write a batch of %d items", len(batch))
            for _ in range(len(batch) + (1 if stop else 0)):
                q.task_done()
            if stop:
                return


# --------------------------------------------------------------------------- decorator factory
def _make_decorator(getter: Callable[[], Tracer], fn, name, kind, capture_input, capture_output):
    if kind not in SPAN_KINDS:
        raise ValueError(f"kind must be one of {SPAN_KINDS}, got {kind!r}")

    def decorate(f: Callable) -> Callable:
        span_name = name or getattr(f, "__qualname__", getattr(f, "__name__", "fn")).replace("<locals>.", "")
        try:
            params = list(inspect.signature(f).parameters)
        except (TypeError, ValueError):
            params = []
        skip_first = bool(params) and params[0] in ("self", "cls")
        replayable = kind in REPLAYABLE_KINDS

        if inspect.iscoroutinefunction(f):
            @functools.wraps(f)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                tr = getter()
                if not tr.enabled:
                    return await f(*a, **kw)
                inp = tr._capture_input(a, kw, skip_first) if capture_input else None
                with _SpanCtx(tr, span_name, kind, inp) as h:
                    out: Any = ctx.MISS
                    if replayable:
                        rs = ctx.active_replay.get()
                        if rs is not None:
                            out = rs.serve(span_name, kind, inp, h)
                    if out is ctx.MISS:
                        out = await f(*a, **kw)
                    if capture_output:
                        tr._record_output(h, out, kind)
                    return out

            return awrapper

        @functools.wraps(f)
        def wrapper(*a: Any, **kw: Any) -> Any:
            tr = getter()
            if not tr.enabled:
                return f(*a, **kw)
            inp = tr._capture_input(a, kw, skip_first) if capture_input else None
            with _SpanCtx(tr, span_name, kind, inp) as h:
                out: Any = ctx.MISS
                if replayable:
                    rs = ctx.active_replay.get()
                    if rs is not None:
                        out = rs.serve(span_name, kind, inp, h)
                if out is ctx.MISS:
                    out = f(*a, **kw)
                if capture_output:
                    tr._record_output(h, out, kind)
                return out

        return wrapper

    return decorate(fn) if fn is not None else decorate


# --------------------------------------------------------------------------- module-level default tracer
_default: Optional[Tracer] = None
_default_lock = threading.Lock()


def init(path: Optional[str] = None, *, store: Any = None, **tracer_kwargs: Any) -> Tracer:
    """Create the process-wide default tracer (SQLite file ``path`` or ``$AGENTTRACE_DB``)."""
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()
        if store is None:
            store = SQLiteStore(path or os.environ.get("AGENTTRACE_DB", "agenttrace.db"))
        tracer_kwargs.setdefault("enabled", os.environ.get("AGENTTRACE_DISABLED", "").lower() not in ("1", "true", "yes"))
        _default = Tracer(store, **tracer_kwargs)
        return _default


def get_tracer() -> Tracer:
    global _default
    if _default is None:
        init()
    return _default  # type: ignore[return-value]


def trace(fn: Optional[Callable] = None, *, name: Optional[str] = None, kind: str = "custom",
          capture_input: bool = True, capture_output: bool = True):
    """Module-level decorator. Looks the tracer up at *call* time, so ``init()`` can come after decoration."""
    return _make_decorator(get_tracer, fn, name, kind, capture_input, capture_output)


def run(name: str, **attrs: Any):
    return get_tracer().run(name, **attrs)


def span(name: str, kind: str = "custom", input: Any = None):
    return get_tracer().span(name, kind, input)


def current_span() -> Any:
    """The innermost active span handle (a harmless no-op handle if there is none)."""
    return ctx.current_span.get() or _NULL


def flush(timeout: float = 5.0) -> bool:
    return get_tracer().flush(timeout)


def get_spans(trace_id: str) -> List[Span]:
    return get_tracer().get_spans(trace_id)


def get_trace(trace_id: str) -> Optional[Dict[str, Any]]:
    return get_tracer().get_trace(trace_id)


def close() -> None:
    """Flush, stop the writer and close the default store."""
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()
            try:
                _default.store.close()
            except Exception:
                pass
            _default = None


atexit.register(close)
