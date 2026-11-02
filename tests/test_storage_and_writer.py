import threading

from agenttrace import SQLiteStore, Span, Tracer


def make_span(trace_id, seq, **kw):
    base = dict(span_id=f"{trace_id}-{seq}", trace_id=trace_id, name="n", kind="tool", seq=seq,
                start_ts=1.0, end_ts=1.1, duration_ms=100.0)
    base.update(kw)
    return Span(**base)


def test_apply_is_idempotent_for_retried_batches():
    store = SQLiteStore(":memory:")
    s = make_span("t1", 0, input={"a": 1})
    for _ in range(3):  # a client retrying the same batch must not create duplicates
        store.apply([("span", s)])
    assert len(store.get_spans("t1")) == 1


def test_summary_aggregates_errors_tokens_and_cost():
    store = SQLiteStore(":memory:")
    store.apply([
        ("trace", {"trace_id": "t1", "name": "run", "start_ts": 10.0, "end_ts": 12.0, "status": "ok"}),
        ("span", make_span("t1", 0, attrs={"tokens_in": 10, "tokens_out": 5, "cost_usd": 0.5})),
        ("span", make_span("t1", 1, attrs={"tokens_in": 1, "tokens_out": 1, "cost_usd": 0.25}, error="X: y")),
    ])
    s = store.get_trace("t1")
    assert (s["span_count"], s["error_count"], s["tokens_in"], s["tokens_out"]) == (2, 1, 11, 6)
    assert s["cost_usd"] == 0.75 and s["duration_ms"] == 2000.0


def test_list_traces_is_newest_first_and_paginated():
    store = SQLiteStore(":memory:")
    for i in range(5):
        store.apply([("trace", {"trace_id": f"t{i}", "name": "r", "start_ts": float(i), "status": "ok"})])
    assert [t["trace_id"] for t in store.list_traces(limit=2)] == ["t4", "t3"]
    assert [t["trace_id"] for t in store.list_traces(limit=2, offset=2)] == ["t2", "t1"]
    assert store.count_traces() == 5


def test_resolve_trace_id_accepts_unique_prefix_only():
    store = SQLiteStore(":memory:")
    for tid in ("abc123", "abd456"):
        store.ensure_trace(tid, "r", 1.0)
    assert store.resolve_trace_id("abc") == "abc123"
    assert store.resolve_trace_id("ab") is None  # ambiguous
    assert store.resolve_trace_id("zzz") is None
    assert store.resolve_trace_id("abd456") == "abd456"


def test_trace_row_is_updated_on_finish_not_duplicated(tracer):
    with tracer.run("r") as r:
        pass
    tracer.flush()
    (t,) = tracer.store.list_traces()
    assert t["trace_id"] == r.trace_id and t["status"] == "ok" and t["end_ts"] is not None


def test_backpressure_drops_instead_of_blocking_the_agent():
    gate = threading.Event()

    class SlowStore(SQLiteStore):
        def apply(self, items):
            gate.wait(5)  # simulate a stalled database
            super().apply(items)

    t = Tracer(SlowStore(":memory:"), max_queue=5)

    @t.trace(kind="tool", name="f")
    def f(i):
        return i

    with t.run("flood"):
        for i in range(60):
            f(i)  # must return immediately even though the writer is stuck
    stats = t.stats()
    assert stats["dropped"] > 0
    gate.set()
    assert t.flush(10)
    final = t.stats()
    assert final["written"] == final["enqueued"] and final["write_errors"] == 0
    t.close()


def test_writer_survives_a_failing_batch():
    calls = {"n": 0}

    class FlakyStore(SQLiteStore):
        def apply(self, items):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("disk full")
            super().apply(items)

    t = Tracer(FlakyStore(":memory:"), batch_size=1)
    with t.run("one"):
        pass
    t.flush()
    with t.run("two"):
        pass
    t.flush()
    assert t.stats()["write_errors"] >= 1
    assert any(x["name"] == "two" for x in t.store.list_traces())  # later runs still land
    t.close()
