"""Measure what AgentTrace costs you. Run it on YOUR machine and paste the table into the README.

    python benchmarks/bench.py                 # defaults
    python benchmarks/bench.py --n 50000 --json

1. per-call overhead : extra time a traced call takes vs. the bare function (caller-side latency)
2. ingest throughput : spans/second, caller-side (enqueue) and end-to-end (until committed to SQLite)

These are micro-benchmarks of a no-op function: real agents spend 100s of ms in LLM calls, so the
overhead is the *worst case* relative cost. Always report the machine, Python version and n.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agenttrace import SQLiteStore, Tracer  # noqa: E402


def pct(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p / 100))]


def bench_overhead(n: int) -> dict:
    tracer = Tracer(SQLiteStore(":memory:"), max_queue=n * 4 + 100)

    def bare(x):
        return x + 1

    traced = tracer.trace(bare, name="bare", kind="tool")
    for i in range(1000):  # warm-up
        bare(i)
    t0 = time.perf_counter_ns()
    for i in range(n):
        bare(i)
    baseline_ns = (time.perf_counter_ns() - t0) / n

    samples = []
    with tracer.run("bench"):
        for i in range(n):
            t = time.perf_counter_ns()
            traced(i)
            samples.append(time.perf_counter_ns() - t)
    tracer.flush(120)
    stats = tracer.stats()
    tracer.close()
    samples.sort()
    us = lambda ns: round(ns / 1000.0, 2)  # noqa: E731
    return {
        "n": n,
        "bare_call_us": us(baseline_ns),
        "traced_mean_us": us(statistics.fmean(samples)),
        "traced_p50_us": us(pct(samples, 50)),
        "traced_p95_us": us(pct(samples, 95)),
        "traced_p99_us": us(pct(samples, 99)),
        "overhead_p50_us": us(pct(samples, 50) - baseline_ns),
        "overhead_p95_us": us(pct(samples, 95) - baseline_ns),
        "dropped": stats["dropped"],
    }


def bench_ingest(n: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tracer = Tracer(SQLiteStore(str(Path(tmp) / "bench.db")), max_queue=n + 100)

        def work(x):
            return x

        traced = tracer.trace(work, name="work", kind="tool")
        t0 = time.perf_counter()
        with tracer.run("ingest"):
            for i in range(n):
                traced(i)
        enqueue_s = time.perf_counter() - t0
        tracer.flush(300)
        total_s = time.perf_counter() - t0
        stats = tracer.stats()
        committed = len(tracer.store.get_spans(tracer.store.list_traces()[0]["trace_id"]))
        tracer.close()
        return {
            "n": n,
            "caller_side_spans_per_s": int(n / enqueue_s),
            "end_to_end_spans_per_s": int(n / total_s),
            "committed_spans": committed,
            "dropped": stats["dropped"],
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    env = {"python": platform.python_version(), "platform": platform.platform(), "cpu": platform.processor() or platform.machine()}
    over, ingest = bench_overhead(args.n), bench_ingest(args.n)
    if args.json:
        print(json.dumps({"env": env, "overhead": over, "ingest": ingest}, indent=2))
        return
    print(f"AgentTrace benchmark | Python {env['python']} | {env['platform']}\n")
    print("| Metric | Result |\n|---|---|")
    print(f"| Bare function call | {over['bare_call_us']} us |")
    print(f"| Traced call p50 / p95 / p99 | {over['traced_p50_us']} / {over['traced_p95_us']} / {over['traced_p99_us']} us |")
    print(f"| Added overhead p50 / p95 | {over['overhead_p50_us']} / {over['overhead_p95_us']} us per call |")
    print(f"| Ingest, caller side | {ingest['caller_side_spans_per_s']:,} spans/s |")
    print(f"| Ingest, end-to-end (committed to SQLite) | {ingest['end_to_end_spans_per_s']:,} spans/s |")
    print(f"| Spans committed / dropped | {ingest['committed_spans']:,} / {ingest['dropped']} |")
    print(f"\n(n = {args.n:,} calls per benchmark)")


if __name__ == "__main__":
    main()
