# AgentTrace

**A debugger for AI agents: record every LLM and tool call, replay a failed run offline, and diff two runs to see exactly where they diverged.**

[![CI](https://github.com/rimjhim-27/agenttrace/actions/workflows/ci.yml/badge.svg)](https://github.com/rimjhim-27/agenttrace/actions)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

Agents are non-deterministic and expensive to re-run. When one fails in production you usually have a log line and a guess.
AgentTrace records the full call tree of a run, then lets you **re-run your agent code against the recording**: control flow,
prompt building, parsing and retries execute for real, while every LLM and tool call is answered from the recording.
The bug reproduces, with no API calls, no side effects and no cost.

```text
$ python examples/demo_agent.py

=== 1. healthy run ===
[agent] support-agent  71.3ms
|- [llm] llm.complete  20.2ms  tokens=11/6  cost=$0.01450
|- [tool] tool.search  10.2ms
|- [llm] llm.complete  20.2ms  tokens=20/4  cost=$0.01600
|- [tool] tool.calc  0.1ms
`- [llm] llm.complete  20.2ms  tokens=27/3  cost=$0.01800

=== 2. insights for the buggy run ===
[WARN ] loop: tool 'tool.search' was called 4 times with identical input - possible loop

=== 4. replay the healthy run (no real LLM/tool calls) ===
same answer       : True
real calls made   : llm=0 tool=0
```

## Features

| Feature | What it does |
|---|---|
| **Tracing SDK** | `@trace` decorator (sync + async) and `with span(...)` build a tree of spans: agent, LLM, tool |
| **Deterministic replay** | Re-run your code with `llm`/`tool` calls served from a recording; reports any divergence |
| **Run diff** | Aligns two runs by `(kind, name, n-th occurrence)` and names the first step that differs |
| **Failure insights** | Rule-based: errors, loops, retries, slow steps, one call dominating token use |
| **Cost + tokens** | Reads token usage from responses; cost from prices *you* register (no stale built-in price list) |
| **Safe by design** | Bounded queue (drops, never blocks your agent), `redact` hook (fails closed), XSS-safe viewer, API-key auth |
| **CLI + HTTP API + viewer** | `agenttrace runs / show / insights / diff / serve` and a REST API |
| **Zero SDK dependencies** | The SDK uses only the standard library; FastAPI is needed only for the server |

## Quick start

```bash
git clone https://github.com/rimjhim-27/agenttrace && cd agenttrace
pip install -e ".[dev]"

pytest                          # test suite
python examples/demo_agent.py   # record -> insights -> diff -> replay, no API key needed
agenttrace --db examples/demo.db serve   # http://127.0.0.1:8000
```

## Use it in your agent

```python
import agenttrace as at

at.init("traces.db")
at.set_price("my-model", input_per_1k=0.003, output_per_1k=0.015)   # your real prices, USD

@at.trace(kind="llm")
def call_llm(prompt: str) -> dict:
    return client.messages.create(...)          # any dict/object with a `usage` field is understood

@at.trace(kind="tool")
def search(query: str) -> str: ...

@at.trace(kind="agent")
def my_agent(question: str) -> str:
    ...                                          # your loop: call_llm / search / ...

with at.run("support-bot") as r:
    my_agent("Why is my order late?")
print(r.trace_id)
```

Later, reproduce that exact run offline:

```python
with at.replay(r.trace_id) as rs:
    my_agent(*rs.root_input["args"], **rs.root_input["kwargs"])   # llm/tool calls come from the recording

print(rs.ok, rs.divergences)    # did the code under test still make the same calls with the same input?
```

Compare two runs, or inspect one:

```bash
agenttrace runs
agenttrace show a1b2c3d4 --io
agenttrace insights a1b2c3d4
agenttrace diff a1b2c3d4 e5f6a7b8
```

## How replay works (and where it stops)

The n-th call of `(kind, name)` during replay gets the n-th recorded call of the same `(kind, name)`.

- `llm` and `tool` spans are served from the recording. `agent` and `custom` spans are *your code under test* and always run for real.
- Recorded exceptions are re-raised with their original builtin type (`TimeoutError` stays `TimeoutError`), so your `except` branches behave as in production. Non-builtin types surface as `RecordedError`.
- Differences are never hidden: `input_changed`, `missing_recording`, `unused_recording` are collected in `session.divergences`. `strict=True` (default) raises on a missing recording.

**Replay does not control** randomness, wall-clock time, environment variables, or side effects inside `agent`/`custom` spans.
Recorded values must be JSON-able. Inputs and outputs are truncated at 20,000 characters per string (configurable), and a
truncated recording cannot reproduce the original value.

## Architecture

```text
 your agent code                          background                        consumers
+------------------+  put_nowait   +---------------------+   batch    +-----------------+   +----------------+
| @trace / span()  | ------------> | bounded queue       | ---------> | SQLiteStore     |<--| CLI            |
| (snapshot I/O,   |  (drop if     | + 1 writer thread   | 1 txn per  | traces, spans   |   | FastAPI + viewer|
|  redact, cost)   |   full)       +---------------------+  batch     +-----------------+   | replay / diff  |
+------------------+                                                                        +----------------+
```

Details and the design trade-offs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Data model: [docs/DATA_MODEL.md](docs/DATA_MODEL.md).

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/spans` | Idempotent batch ingest (re-sending the same `span_id` overwrites, never duplicates) |
| `GET` | `/api/v1/runs` | List runs (`limit`, `offset`), newest first |
| `GET` | `/api/v1/runs/{id}` | Run summary + all spans |
| `GET` | `/api/v1/runs/{id}/insights` | Rule-based findings |
| `GET` | `/api/v1/diff?a=..&b=..` | Diff of two runs |
| `GET` | `/api/v1/health` | Liveness (no auth) |

Set `AGENTTRACE_API_KEY` to require an `X-API-Key` header on everything except `/health`. Interactive docs at `/docs`.
The server has no TLS or rate limiting of its own: put it behind a reverse proxy before exposing it to a network.

## Performance

Measured with `python benchmarks/bench.py` on a small cloud sandbox (Python 3.13, Linux x86_64, no-op traced function, n = 20,000).
**Re-run it on your machine and replace this table with your numbers.**

| Metric | Result |
|---|---|
| Added overhead per traced call, p50 / p95 | ~35 us / ~52 us |
| Ingest, caller side | ~28,000 spans/s |
| Ingest, end-to-end (committed to SQLite file) | ~20,000 spans/s |
| Spans dropped under normal load | 0 |

Context: an LLM call takes 100s of ms, so ~35 us is well under 0.1% of a real step. These numbers are for a no-op function, i.e. the
*worst-case relative* cost. Under a stalled database the queue fills and spans are dropped and counted (`tracer.stats()["dropped"]`) instead of blocking the agent.

## Project layout

```text
src/agenttrace/
  tracer.py     @trace, span(), run(), bounded queue + writer thread
  replayer.py   deterministic replay, divergence reporting
  differ.py     run-vs-run diff
  insights.py   loop / retry / slow / token-heavy / error rules
  storage.py    SQLite backend (WAL, idempotent upserts, indexes)
  api.py ui.py  FastAPI app and the dependency-free viewer
  cli.py        agenttrace command
  costs.py serialize.py render.py models.py context.py
tests/          pytest suite (tracing, async, replay, diff, insights, writer, API, CLI)
examples/       demo_agent.py (no API key needed)
benchmarks/     bench.py
docs/           architecture, data model, roadmap, PostgreSQL schema draft
```

## Limitations (honest list)

- SQLite only today. The storage surface is small on purpose; a PostgreSQL backend is on the [roadmap](docs/ROADMAP.md) (schema draft included).
- Local SQLite writes assume one writer process per database file. A multi-process setup should send spans through the HTTP API.
- No streaming-response capture yet: record the final, assembled response.
- Spans of a *running* trace appear once their step finishes; a crash mid-step loses that step.
- Threads started with plain `threading.Thread` do not inherit trace context; use `contextvars.copy_context().run` or `asyncio.to_thread`.
- The viewer is intentionally minimal (list, tree, insights). A richer dashboard is planned.

## Development

```bash
pip install -e ".[dev]"
pytest
python benchmarks/bench.py --n 20000
```

## License

MIT. See [LICENSE](LICENSE).
