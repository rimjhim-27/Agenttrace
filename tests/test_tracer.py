import asyncio

import pytest

from agenttrace import SQLiteStore, Tracer, set_price
from agenttrace.serialize import canonical, to_jsonable


def test_sync_span_records_input_output_and_timing(tracer):
    @tracer.trace(kind="tool")
    def add(a, b):
        return a + b

    with tracer.run("r") as r:
        assert add(1, b=2) == 3

    (s,) = tracer.get_spans(r.trace_id)
    assert s.kind == "tool" and s.name.endswith("add")
    assert s.input == {"args": [1], "kwargs": {"b": 2}}
    assert s.output == 3
    assert s.error is None and s.duration_ms >= 0 and s.parent_id is None


def test_nested_spans_form_a_tree_with_ordered_seq(tracer):
    @tracer.trace(kind="tool", name="inner")
    def inner():
        return 1

    @tracer.trace(kind="agent", name="outer")
    def outer():
        inner()
        inner()

    with tracer.run("r") as r:
        outer()

    spans = tracer.get_spans(r.trace_id)
    assert [s.name for s in spans] == ["outer", "inner", "inner"]
    assert [s.seq for s in spans] == [0, 1, 2]
    assert spans[1].parent_id == spans[0].span_id == spans[2].parent_id


def test_exception_is_recorded_and_reraised_and_marks_run_error(tracer):
    @tracer.trace(kind="tool")
    def boom():
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        with tracer.run("r") as r:
            boom()

    (s,) = tracer.get_spans(r.trace_id)
    assert s.error == "ValueError: bad input"
    assert tracer.get_trace(r.trace_id)["status"] == "error"


def test_async_functions_are_traced_and_nest(tracer):
    @tracer.trace(kind="llm", name="llm")
    async def llm(x):
        await asyncio.sleep(0)
        return x * 2

    @tracer.trace(kind="agent", name="agent")
    async def agent(x):
        return await llm(x)

    async def main():
        with tracer.run("async-run") as r:
            assert await agent(4) == 8
        return r.trace_id

    tid = asyncio.run(main())
    spans = tracer.get_spans(tid)
    assert [s.name for s in spans] == ["agent", "llm"]
    assert spans[1].parent_id == spans[0].span_id


def test_concurrent_async_tasks_do_not_mix_up_parents(tracer):
    @tracer.trace(kind="tool", name="work")
    async def work(i):
        await asyncio.sleep(0.001 * (3 - i))
        return i

    @tracer.trace(kind="agent", name="agent")
    async def agent(i):
        return await work(i)

    async def main():
        with tracer.run("r") as r:
            await asyncio.gather(*(agent(i) for i in range(3)))
        return r.trace_id

    spans = tracer.get_spans(asyncio.run(main()))
    agents = {s.span_id: s for s in spans if s.name == "agent"}
    works = [s for s in spans if s.name == "work"]
    assert len(agents) == 3 and len(works) == 3
    for w in works:  # each tool call hangs under the agent that made it
        assert w.parent_id in agents
        assert agents[w.parent_id].input["args"] == w.input["args"]


def test_implicit_run_is_created_when_no_run_is_active(tracer):
    @tracer.trace(kind="agent")
    def solo():
        return "ok"

    solo()
    tracer.flush()
    (summary,) = tracer.store.list_traces()
    assert summary["span_count"] == 1 and summary["status"] == "ok"


def test_context_manager_span_and_manual_output(tracer):
    with tracer.run("r") as r:
        with tracer.span("step", kind="custom", input={"q": 1}) as sp:
            sp.set_output({"answer": 42})
            sp.set_attr("note", "manual")
    (s,) = tracer.get_spans(r.trace_id)
    assert s.input == {"q": 1} and s.output == {"answer": 42} and s.attrs["note"] == "manual"


def test_disabled_tracer_is_a_transparent_noop():
    t = Tracer(SQLiteStore(":memory:"), enabled=False)

    @t.trace(kind="tool")
    def f(x):
        return x + 1

    with t.run("r"):
        assert f(1) == 2
    t.flush()
    assert t.store.list_traces() == []
    t.close()


def test_llm_usage_is_extracted_and_cost_estimated(tracer):
    set_price("m", 1.0, 2.0)  # USD per 1k tokens

    @tracer.trace(kind="llm")
    def call():
        return {"model": "m", "text": "hi", "usage": {"input_tokens": 1000, "output_tokens": 500}}

    with tracer.run("r") as r:
        call()
    (s,) = tracer.get_spans(r.trace_id)
    assert (s.attrs["tokens_in"], s.attrs["tokens_out"], s.attrs["model"]) == (1000, 500, "m")
    assert s.attrs["cost_usd"] == pytest.approx(2.0)
    summary = tracer.get_trace(r.trace_id)
    assert summary["tokens_in"] == 1000 and summary["cost_usd"] == pytest.approx(2.0)


def test_openai_style_usage_names_are_understood(tracer):
    @tracer.trace(kind="llm")
    def call():
        return {"usage": {"prompt_tokens": 7, "completion_tokens": 3}}

    with tracer.run("r") as r:
        call()
    (s,) = tracer.get_spans(r.trace_id)
    assert s.tokens == 10


def test_method_self_is_not_captured(tracer):
    class Bot:
        @tracer.trace(kind="tool", name="bot.say")
        def say(self, text):
            return text.upper()

    with tracer.run("r") as r:
        Bot().say("hi")
    (s,) = tracer.get_spans(r.trace_id)
    assert s.input == {"args": ["hi"], "kwargs": {}}


def test_redact_hook_masks_secrets_before_storage():
    def redact(obj):
        if isinstance(obj, dict):
            return {k: ("***" if k == "api_key" else redact(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [redact(v) for v in obj]
        return obj

    t = Tracer(SQLiteStore(":memory:"), redact=redact)

    @t.trace(kind="tool")
    def call(url, api_key=None):
        return {"api_key": api_key, "ok": True}

    with t.run("r") as r:
        call("http://x", api_key="sk-secret")
    (s,) = t.get_spans(r.trace_id)
    assert "sk-secret" not in str(s.to_dict())
    assert s.input["kwargs"]["api_key"] == "***" and s.output["api_key"] == "***"
    t.close()


def test_failing_redactor_fails_closed():
    t = Tracer(SQLiteStore(":memory:"), redact=lambda o: 1 / 0)

    @t.trace(kind="tool")
    def call(secret):
        return secret

    with t.run("r") as r:
        call("sk-secret")
    (s,) = t.get_spans(r.trace_id)
    assert "sk-secret" not in str(s.to_dict())
    t.close()


def test_invalid_kind_is_rejected(tracer):
    with pytest.raises(ValueError):
        tracer.trace(kind="nope")(lambda: None)


# ---- serialization ---------------------------------------------------------
def test_to_jsonable_handles_odd_objects_deterministically():
    class Thing:
        pass

    a, b = to_jsonable(Thing()), to_jsonable(Thing())
    assert a == b and "0x" not in a  # memory addresses never leak into snapshots
    assert to_jsonable({1: {2, 1}}) == {"1": [1, 2]}
    assert to_jsonable(float("nan")) == "nan"
    assert to_jsonable(b"abc") == "<bytes len=3>"


def test_to_jsonable_truncates_long_strings_and_handles_cycles():
    out = to_jsonable("x" * 50, max_chars=10)
    assert out.startswith("x" * 10) and "truncated 40" in out
    loop = []
    loop.append(loop)
    assert "max depth" in canonical(loop)  # no RecursionError


def test_dataclass_and_pydantic_like_objects():
    from dataclasses import dataclass

    @dataclass
    class P:
        x: int

    class M:
        def model_dump(self):
            return {"y": 2}

    assert to_jsonable(P(1)) == {"x": 1}
    assert to_jsonable(M()) == {"y": 2}
