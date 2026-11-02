# Data model

## Trace (a "run")

One execution of an agent.

| Field | Type | Notes |
|---|---|---|
| `trace_id` | text, PK | 16 hex chars |
| `name` | text | what you passed to `run(...)`; replays are named `replay:<original>` |
| `start_ts`, `end_ts` | real | epoch seconds; `end_ts` is NULL while running |
| `status` | text | `running`, `ok`, `error` |
| `replay_of` | text, nullable | `trace_id` this run replayed |
| `attrs` | JSON | free-form keyword arguments passed to `run(...)` |

## Span

One timed step. Spans form a tree via `parent_id` and are ordered by `seq`.

| Field | Type | Notes |
|---|---|---|
| `span_id` | text, PK | idempotency key for ingestion |
| `trace_id` | text | indexed together with `seq` |
| `parent_id` | text, nullable | NULL for root spans |
| `name` | text | function qualname unless overridden |
| `kind` | text | `agent`, `llm`, `tool`, `custom` |
| `seq` | integer | order of span **start** within the trace |
| `start_ts`, `end_ts`, `duration_ms` | real | duration from `perf_counter` (monotonic) |
| `input`, `output` | JSON | snapshots; strings truncated at `max_payload_chars` |
| `error` | text, nullable | `"ExceptionType: message"` |
| `attrs` | JSON | `tokens_in`, `tokens_out`, `model`, `cost_usd`, `replayed`, `recorded_span_id`, custom keys |

## Example span

```json
{
  "span_id": "9f1c2a7b4d3e8a01", "trace_id": "7422500003d0416f", "parent_id": "c04d...",
  "name": "llm.complete", "kind": "llm", "seq": 1,
  "start_ts": 1792950597.49, "end_ts": 1792950597.51, "duration_ms": 20.2,
  "input":  {"args": [["QUESTION: What is the capital of France?"]], "kwargs": {"mode": "good"}},
  "output": {"model": "demo-model", "text": "ACTION search | capital of france", "usage": {"input_tokens": 11, "output_tokens": 6}},
  "error": null,
  "attrs": {"tokens_in": 11, "tokens_out": 6, "model": "demo-model", "cost_usd": 0.0145}
}
```

## Indexes

- `spans(trace_id, seq)`: loading a run's spans in order is a single index range scan.
- `traces(start_ts DESC)`: the run list is newest-first.

Run summaries (span count, error count, tokens, cost) are computed with one aggregate query over `spans`, using SQLite JSON1
`json_extract` on `attrs`. For very large deployments, store these as real columns instead (see roadmap).
