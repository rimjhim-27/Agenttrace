import pytest

from agenttrace import RecordedError, ReplayMismatch, replay


@pytest.fixture
def app(tracer):
    calls = {"llm": 0, "tool": 0}

    @tracer.trace(kind="llm", name="llm")
    def llm(prompt):
        calls["llm"] += 1
        return {"text": prompt.upper()}

    @tracer.trace(kind="tool", name="tool")
    def tool(x):
        calls["tool"] += 1
        return x * 2

    @tracer.trace(kind="agent", name="agent")
    def agent(q):
        text = llm(q)["text"]
        return tool(len(text))

    @tracer.trace(kind="agent", name="agent2")
    def agent2(q):  # makes TWO llm calls
        llm(q)
        return llm(q)["text"]

    return tracer, calls, llm, tool, agent, agent2


def record(tracer, fn, *args):
    with tracer.run("rec") as r:
        out = fn(*args)
    return r.trace_id, out


def test_replay_returns_same_result_without_executing_llm_or_tools(app):
    tracer, calls, _, _, agent, _ = app
    tid, original = record(tracer, agent, "abc")
    assert calls == {"llm": 1, "tool": 1}

    with replay(tid, tracer=tracer) as rs:
        replayed = agent(*rs.root_input["args"], **rs.root_input["kwargs"])

    assert replayed == original == 6
    assert calls == {"llm": 1, "tool": 1}  # nothing real ran during replay
    assert rs.ok and rs.served == 2


def test_replay_writes_a_new_linked_trace_marked_as_replayed(app):
    tracer, _, _, _, agent, _ = app
    tid, _ = record(tracer, agent, "abc")
    with replay(tid, tracer=tracer) as rs:
        agent("abc")
    tracer.flush()
    summary = tracer.get_trace(rs.trace_id)
    assert summary["replay_of"] == tid and summary["name"] == "replay:rec"
    spans = tracer.get_spans(rs.trace_id)
    flags = {s.name: s.attrs.get("replayed") for s in spans}
    assert flags == {"agent": None, "llm": True, "tool": True}  # the agent itself ran for real


def test_changed_input_is_reported_as_divergence(app):
    tracer, _, _, _, agent, _ = app
    tid, _ = record(tracer, agent, "abc")
    with replay(tid, tracer=tracer) as rs:
        out = agent("abcd")  # code under test now feeds a different prompt
    assert out == 6  # still served from the recording
    kinds = [(d.kind, d.name) for d in rs.divergences]
    assert kinds == [("input_changed", "llm")]
    assert not rs.ok


def test_fail_on_input_change_raises(app):
    tracer, _, _, _, agent, _ = app
    tid, _ = record(tracer, agent, "abc")
    with pytest.raises(ReplayMismatch):
        with replay(tid, tracer=tracer, fail_on_input_change=True):
            agent("zzz")


def test_extra_call_in_strict_mode_raises(app):
    tracer, _, _, _, agent, agent2 = app
    tid, _ = record(tracer, agent, "abc")  # recording has ONE llm call
    with pytest.raises(ReplayMismatch):
        with replay(tid, tracer=tracer):
            agent2("abc")  # code now makes two


def test_extra_call_in_non_strict_mode_runs_live_and_is_reported(app):
    tracer, calls, _, _, agent, agent2 = app
    tid, _ = record(tracer, agent, "abc")
    before = calls["llm"]
    with replay(tid, tracer=tracer, strict=False) as rs:
        agent2("abc")
    assert calls["llm"] == before + 1  # only the unrecorded call executed for real
    assert [d.kind for d in rs.divergences] == ["missing_recording", "unused_recording"]


def test_unused_recording_is_reported(app):
    tracer, _, _, _, agent, agent2 = app
    tid, _ = record(tracer, agent2, "abc")  # recording has TWO llm calls
    with replay(tid, tracer=tracer, strict=False) as rs:
        agent("abc")  # code now makes only one
    assert any(d.kind == "unused_recording" and d.name == "llm" and d.nth == 2 for d in rs.divergences)


def test_recorded_builtin_exception_is_reraised_with_its_original_type(tracer):
    @tracer.trace(kind="tool", name="flaky")
    def flaky():
        raise TimeoutError("upstream too slow")

    @tracer.trace(kind="agent", name="agent")
    def agent():
        try:
            flaky()
        except TimeoutError:  # the agent's own recovery path must still run on replay
            return "recovered"

    tid, out = record(tracer, agent)
    assert out == "recovered"
    with replay(tid, tracer=tracer) as rs:
        assert agent() == "recovered"
    assert rs.ok


def test_recorded_custom_exception_becomes_recorded_error(tracer):
    class MyApiError(Exception):
        pass

    @tracer.trace(kind="tool", name="api")
    def api():
        raise MyApiError("429")

    @tracer.trace(kind="agent", name="agent")
    def agent():
        try:
            api()
        except MyApiError:
            return "handled"

    tid, _ = record(tracer, agent)
    with pytest.raises(RecordedError, match="MyApiError: 429"):
        with replay(tid, tracer=tracer):
            agent()


def test_replay_of_unknown_trace_raises_keyerror(tracer):
    with pytest.raises(KeyError):
        with replay("does-not-exist", tracer=tracer):
            pass


def test_replayed_values_are_copies_not_shared_references(app):
    tracer, _, llm, _, agent, _ = app
    tid, _ = record(tracer, agent, "abc")
    with replay(tid, tracer=tracer):
        first = llm("abc")
        first["text"] = "MUTATED"
    with replay(tid, tracer=tracer):
        assert llm("abc")["text"] == "ABC"
