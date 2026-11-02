from agenttrace import Span, analyze, diff_traces


def sp(seq, name, kind="tool", inp=None, out=None, dur=10.0, error=None, tokens=None, parent=None):
    attrs = {}
    if tokens:
        attrs = {"tokens_in": tokens[0], "tokens_out": tokens[1]}
    return Span(span_id=f"s{seq}", trace_id="t", name=name, kind=kind, seq=seq, parent_id=parent,
                input=inp, output=out, duration_ms=dur, error=error, attrs=attrs)


# ------------------------------------------------------------------ diff
def test_identical_runs_have_no_divergence():
    run = [sp(0, "agent", "agent"), sp(1, "search", inp={"q": 1}, out="a")]
    d = diff_traces(run, run)
    assert d.summary()["identical"] and d.first_divergence is None


def test_first_divergence_points_at_the_first_changed_span():
    a = [sp(0, "agent", "agent"), sp(1, "search", out="paris"), sp(2, "calc", out=391)]
    b = [sp(0, "agent", "agent"), sp(1, "search", out="lyon"), sp(2, "calc", out=0)]
    d = diff_traces(a, b)
    fd = d.first_divergence
    assert (fd.name, fd.status, fd.changes) == ("search", "changed", ["output differs"])
    assert d.summary()["changed"] == 2


def test_alignment_is_by_occurrence_so_an_extra_retry_does_not_shift_everything():
    a = [sp(0, "agent", "agent"), sp(1, "search", out="x"), sp(2, "calc", out=1)]
    b = [sp(0, "agent", "agent"), sp(1, "search", out="x"), sp(2, "search", out="x"), sp(3, "calc", out=1)]
    d = diff_traces(a, b)
    added = [e for e in d.entries if e.status == "added"]
    assert [(e.name, e.nth) for e in added] == [("search", 2)]
    assert d.summary()["changed"] == 0  # calc is still matched with calc


def test_removed_span_and_token_and_duration_deltas():
    a = [sp(0, "agent", "agent", dur=100), sp(1, "llm", "llm", tokens=(10, 10), dur=50, parent="s0")]
    b = [sp(0, "agent", "agent", dur=400)]
    s = diff_traces(a, b).summary()
    assert s["removed"] == 1 and s["tokens_delta"] == -20 and s["duration_delta_ms"] == 300.0


def test_timing_is_a_note_not_a_semantic_change():
    a = [sp(0, "search", dur=100)]
    b = [sp(0, "search", dur=900)]
    (e,) = diff_traces(a, b).entries
    assert e.status == "same" and e.notes and "slower" in e.notes[0]


# -------------------------------------------------------------- insights
def kinds(spans, **kw):
    return [i.kind for i in analyze(spans, **kw)]


def test_loop_detection_needs_identical_input():
    looping = [sp(i, "search", inp={"q": "same"}) for i in range(4)]
    varied = [sp(i, "search", inp={"q": str(i)}) for i in range(4)]
    assert "loop" in kinds(looping)
    assert "loop" not in kinds(varied)


def test_error_and_retry_detection():
    spans = [sp(0, "api", error="TimeoutError: slow"), sp(1, "api", error="TimeoutError: slow"), sp(2, "api", out="ok")]
    found = analyze(spans)
    assert [i.kind for i in found].count("error") == 2
    retry = next(i for i in found if i.kind == "retry")
    assert "2 time(s) before succeeding" in retry.message and len(retry.span_ids) == 3


def test_slow_ignores_the_root_agent_span():
    spans = [sp(0, "agent", "agent", dur=9000), sp(1, "search", dur=3000)]
    slow = [i for i in analyze(spans, slow_ms=2000) if i.kind == "slow"]
    assert len(slow) == 1 and "search" in slow[0].message


def test_token_heavy_requires_dominance_and_enough_calls():
    llm = lambda i, n: sp(i, "llm", "llm", inp={"i": i}, tokens=(n, 0))
    assert "token_heavy" in kinds([llm(0, 900), llm(1, 50), llm(2, 50)])
    assert "token_heavy" not in kinds([llm(0, 100), llm(1, 100), llm(2, 100)])
    assert "token_heavy" not in kinds([llm(0, 900), llm(1, 50)])  # too few calls to call it a pattern


def test_clean_run_has_no_insights():
    assert analyze([sp(0, "agent", "agent"), sp(1, "search", inp={"q": 1})]) == []


def test_near_duplicate_prompt_detection():
    # Same question re-asked with a few extra words -> high token overlap
    base = "What is the capital of France and what is its population?"
    near = [sp(i, "llm", "llm", inp={"q": base}) for i in range(3)]
    varied = [sp(i, "llm", "llm", inp={"q": f"question number {i} about totally different things"}) for i in range(3)]
    assert "near_duplicate" in kinds(near)
    assert "near_duplicate" not in kinds(varied)


def test_near_duplicate_does_not_fire_on_distinct_prompts():
    spans = [sp(i, "llm", "llm", inp={"q": f"unique topic {i} alpha beta gamma"}) for i in range(3)]
    assert "near_duplicate" not in kinds(spans)


def test_near_duplicate_ignores_tool_spans():
    spans = [sp(i, "search", "tool", inp={"q": "same question repeated"}) for i in range(3)]
    assert "near_duplicate" not in kinds(spans)
