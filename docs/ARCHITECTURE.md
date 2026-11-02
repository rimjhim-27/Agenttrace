# Architecture and design decisions

## Goals

1. Add almost nothing to the latency of the agent being traced.
2. Never crash or block the agent because tracing failed.
3. Make a recorded run reproducible without any network or API cost.
4. Keep the SDK dependency-free so it can be dropped into any project.

## Data flow

1. A decorated function is called. `_SpanCtx.__enter__` creates a `Span` (new id, parent from a `ContextVar`, `seq` from a per-trace counter).
2. Input is snapshotted to a JSON-able value (`serialize.to_jsonable`): deterministic, bounded, never raises.
3. The function runs. Output is snapshotted; for `llm` spans token usage and cost are extracted.
4. `__exit__` computes duration with `perf_counter`, records any exception string, and calls `queue.put_nowait`.
5. One daemon thread drains the queue in batches (up to `batch_size`) and writes each batch in **one transaction**.
6. Readers (CLI, API, replay) query the store. `Tracer.flush()` waits for the queue to drain so "read your own writes" works.

## Key decisions and trade-offs

| Decision | Why | Trade-off |
|---|---|---|
| `contextvars` for current trace/span | Correct across `await` and `asyncio.gather` (tested) | Plain threads do not inherit context |
| Bounded queue + **drop on full** | An observability tool must never stall the app; drops are counted and visible | Under a stalled DB, spans are lost (not the agent) |
| Single writer thread, batched transactions | One commit per batch instead of per span: ~20k spans/s end-to-end on SQLite | Spans appear in storage slightly after they finish |
| `INSERT OR REPLACE` keyed by `span_id` | Idempotent ingestion: client retries cannot create duplicates | A replaced row loses older field values (same id = same span) |
| SQLite (WAL) first | Zero setup, ideal for local debugging and CI | One writer process per file; Postgres planned |
| Snapshots instead of live objects | Replay and diff need stable, comparable values; addresses (`0x7f..`) are stripped | Non-JSON-able objects are stored as `repr`, so they cannot be replayed faithfully |
| Replay matches the n-th call per `(kind, name)` | Robust to reordering of unrelated calls; divergences are explicit | Renaming a traced function breaks matching (reported, not silent) |
| Redaction runs *before* queueing and **fails closed** | Secrets never reach storage; a broken redactor stores a placeholder, not the secret | A bad redactor loses payload detail |
| Viewer renders with `textContent` only | Traces hold arbitrary LLM/user strings; `innerHTML` would be an XSS hole (test asserts it) | Less rich rendering |
| Prices are user-registered | Prices change and differ per contract; a built-in table goes stale silently | One extra setup line |

## Replay semantics

`replay(trace_id)` loads the recorded spans, groups replayable ones (`llm`, `tool`) by `(kind, name)` in `seq` order, and sets a
`ContextVar` for the duration of the block. Every decorated `llm`/`tool` call consults it: a hit returns a deep copy of the
recorded output (so mutations cannot leak between replays) or re-raises the recorded error; a miss raises `ReplayMismatch` in
strict mode, or runs the real function in non-strict mode. After the block, recordings that were never consumed are reported as
`unused_recording`. The replay is itself recorded as a new trace with `replay_of` pointing at the original.

## Failure modes considered

| Failure | Behaviour |
|---|---|
| Store raises while writing a batch | Logged, `write_errors` incremented, writer keeps running (test) |
| Queue full | Span dropped, `dropped` incremented, agent unaffected (test) |
| Exception in traced code | Recorded on the span, re-raised unchanged, run marked `error` (test) |
| Unserialisable / cyclic / huge payload | Snapshot falls back to bounded `repr`; depth and length capped (test) |
| Redactor raises | Payload replaced by `<redaction failed>` (test) |
| Process exit with spans queued | `atexit` handler flushes the default tracer |
| Tracing disabled (`enabled=False` or `AGENTTRACE_DISABLED=1`) | Decorators become pass-through (test) |

## Extending

- **New storage backend:** implement `apply`, `ensure_trace`, `get_trace`, `list_traces`, `count_traces`, `get_spans`, `resolve_trace_id`, `close`. See `docs/schema_postgres.sql`.
- **New insight rule:** add a block to `insights.analyze` and a test in `tests/test_diff_and_insights.py`.
- **Framework integrations:** wrap the framework's LLM/tool call sites with `at.trace(kind=...)`.
