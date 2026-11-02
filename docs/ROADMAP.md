# Roadmap

Status: **v0.1 (alpha)**. Everything under "Done" is covered by tests or by the runnable demo.

## Done (v0.1)

- [x] Tracing SDK: sync + async decorators, context managers, nested spans, implicit runs
- [x] Bounded queue, batched writer, drop counters, `flush()`, `atexit` flush
- [x] SQLite storage (WAL, idempotent upserts, indexes)
- [x] Deterministic replay with divergence reporting, strict / non-strict modes
- [x] Run diff aligned by occurrence; rule-based insights
- [x] Token + cost tracking with user-registered prices
- [x] Redaction hook (fails closed), payload truncation
- [x] CLI, FastAPI server with API-key auth and idempotent ingest, minimal viewer
- [x] CI matrix (Python 3.10 to 3.13), benchmark script

## Next (v0.2)

- [ ] PostgreSQL backend (draft schema in `docs/schema_postgres.sql`) + a shared store test-suite run against both backends
- [ ] HTTP exporter in the SDK (`init(endpoint=..., api_key=...)`) so apps can ship spans to a remote server
- [ ] Streaming-response capture
- [ ] Publish to PyPI; GitHub release with changelog
- [ ] Real measured benchmark table from a documented machine in the README

## Later

- [ ] Next.js dashboard: timeline view, side-by-side diff, filters
- [ ] Framework adapters (OpenAI / Anthropic client wrappers, LangChain callbacks)
- [ ] Replay with input overrides ("what if the model had answered X here?")
- [ ] Regression mode: replay a folder of recorded runs in CI and fail on divergences
- [ ] Retention policy and sampling

## Good first issues

1. Add `agenttrace export <run> --json`.
2. Add an insight rule for repeated near-identical LLM prompts.
3. Add `--since` / `--status` filters to `agenttrace runs`.
