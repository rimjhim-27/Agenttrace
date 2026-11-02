"""End-to-end demo. No API key, no network: the "LLM" is a tiny scripted function.

    python examples/demo_agent.py

What it shows
  1. record a healthy agent run and a buggy one that loops
  2. failure insights flag the loop
  3. diff tells you the exact step where the two runs diverge
  4. replay re-runs the healthy run's code with ZERO real LLM/tool calls
Then view everything in the browser:  agenttrace --db examples/demo.db serve
"""
from __future__ import annotations

import ast
import operator
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without installing

import agenttrace as at  # noqa: E402
from agenttrace.render import render_tree  # noqa: E402

DB = Path(__file__).resolve().parent / "demo.db"
CALLS = {"llm": 0, "tool": 0}  # counts REAL executions, to prove replay does not run them
KNOWLEDGE = {"capital of france": "Paris"}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def safe_eval(expr: str) -> float:
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in OPS:
            return OPS[type(n.op)](ev(n.left), ev(n.right))
        raise ValueError("unsupported expression")

    return ev(ast.parse(expr, mode="eval").body)


@at.trace(name="llm.complete", kind="llm")
def fake_llm(history: list, mode: str = "good") -> dict:
    """Scripted stand-in for a real model call."""
    CALLS["llm"] += 1
    time.sleep(0.02)
    obs = [h.split(": ", 1)[1] for h in history if h.startswith("OBSERVATION")]
    if mode == "good":
        script = ["ACTION search | capital of france", "ACTION calc | 17*23"]
    else:  # the bug: the agent never moves on after the first tool call
        script = ["ACTION search | capital of france"] * 4
    text = script[len(obs)] if len(obs) < len(script) else "FINAL: " + ", ".join(obs[-2:])
    return {"model": "demo-model", "text": text,
            "usage": {"input_tokens": sum(len(h.split()) for h in history), "output_tokens": len(text.split())}}


@at.trace(name="tool.search", kind="tool")
def search(query: str) -> str:
    CALLS["tool"] += 1
    time.sleep(0.01)
    return KNOWLEDGE.get(query.lower(), "no result")


@at.trace(name="tool.calc", kind="tool")
def calc(expr: str) -> float:
    CALLS["tool"] += 1
    return safe_eval(expr)


TOOLS = {"search": search, "calc": calc}


@at.trace(name="support-agent", kind="agent")
def run_agent(question: str, mode: str = "good", max_steps: int = 6) -> str:
    history = [f"QUESTION: {question}"]
    for _ in range(max_steps):
        text = fake_llm(history, mode=mode)["text"]
        history.append(f"ASSISTANT: {text}")
        if text.startswith("FINAL"):
            return text
        head, arg = text.split("|", 1)
        history.append(f"OBSERVATION: {TOOLS[head.split()[1]](arg.strip())}")
    return "gave up"


def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB) + suffix).unlink(missing_ok=True)
    at.init(str(DB))
    at.set_price("demo-model", 0.5, 1.5)  # USD per 1k tokens; made-up numbers for the demo
    question = "What is the capital of France, and what is 17*23?"

    with at.run("demo-good") as good:
        answer = run_agent(question, mode="good")
    with at.run("demo-loopy") as loopy:
        run_agent(question, mode="loopy")
    at.flush()

    print("=== 1. healthy run ===")
    print(render_tree(at.get_spans(good.trace_id)))
    print(f"answer: {answer}\n")

    print("=== 2. insights for the buggy run ===")
    for i in at.analyze(at.get_spans(loopy.trace_id)):
        print(f"[{i.severity.upper():5}] {i.kind}: {i.message}")

    print("\n=== 3. diff: healthy vs buggy ===")
    d = at.diff_traces(at.get_spans(good.trace_id), at.get_spans(loopy.trace_id))
    # the root agent span always differs when its input differs, so look at llm/tool steps
    fd = next(e for e in d.entries if e.status != "same" and e.kind in ("llm", "tool"))
    print(f"first divergence: {fd.kind} '{fd.name}' call #{fd.nth} ({fd.status}) -> {', '.join(fd.changes)}")
    print(f"summary: {d.summary()}")

    print("\n=== 4. replay the healthy run (no real LLM/tool calls) ===")
    before = dict(CALLS)
    with at.replay(good.trace_id) as rs:
        replayed = run_agent(*rs.root_input["args"], **rs.root_input["kwargs"])
    at.flush()
    print(f"same answer       : {replayed == answer}")
    print(f"real calls made   : llm={CALLS['llm'] - before['llm']} tool={CALLS['tool'] - before['tool']}")
    print(f"calls from replay : {rs.served}   divergences: {len(rs.divergences)}")
    print(render_tree(at.get_spans(rs.trace_id)))

    print(f"\nstats: {at.get_tracer().stats()}")
    print(f"\nOpen the viewer:  agenttrace --db {DB} serve   (then http://127.0.0.1:8000)")
    at.close()


if __name__ == "__main__":
    main()
