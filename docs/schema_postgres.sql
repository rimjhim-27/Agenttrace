-- DRAFT: PostgreSQL schema for a future backend. Not exercised by the test suite yet.
-- Differences from the SQLite schema: native JSONB, real columns for hot aggregates,
-- time partitioning for the span table.

CREATE TABLE traces (
    trace_id   TEXT PRIMARY KEY,
    name       TEXT        NOT NULL,
    start_ts   TIMESTAMPTZ NOT NULL,
    end_ts     TIMESTAMPTZ,
    status     TEXT        NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'error')),
    replay_of  TEXT REFERENCES traces(trace_id),
    attrs      JSONB       NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_traces_start ON traces (start_ts DESC);
CREATE INDEX idx_traces_replay_of ON traces (replay_of) WHERE replay_of IS NOT NULL;

CREATE TABLE spans (
    span_id     TEXT        NOT NULL,
    trace_id    TEXT        NOT NULL,
    parent_id   TEXT,
    name        TEXT        NOT NULL,
    kind        TEXT        NOT NULL CHECK (kind IN ('agent', 'llm', 'tool', 'custom')),
    seq         INTEGER     NOT NULL,
    start_ts    TIMESTAMPTZ NOT NULL,
    end_ts      TIMESTAMPTZ NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL,
    input       JSONB,
    output      JSONB,
    error       TEXT,
    attrs       JSONB       NOT NULL DEFAULT '{}',
    -- hot aggregates as real columns so run summaries do not parse JSON
    tokens_in   INTEGER GENERATED ALWAYS AS ((attrs ->> 'tokens_in')::int) STORED,
    tokens_out  INTEGER GENERATED ALWAYS AS ((attrs ->> 'tokens_out')::int) STORED,
    cost_usd    NUMERIC(12, 6) GENERATED ALWAYS AS ((attrs ->> 'cost_usd')::numeric) STORED,
    PRIMARY KEY (span_id, start_ts)          -- partition key must be part of the PK
) PARTITION BY RANGE (start_ts);

CREATE INDEX idx_spans_trace_seq ON spans (trace_id, seq);
CREATE INDEX idx_spans_errors ON spans (trace_id) WHERE error IS NOT NULL;

-- one partition per month, created by a scheduled job
CREATE TABLE spans_2026_10 PARTITION OF spans
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

-- idempotent ingestion (replaces SQLite's INSERT OR REPLACE)
-- INSERT INTO spans (...) VALUES (...)
-- ON CONFLICT (span_id, start_ts) DO UPDATE SET output = EXCLUDED.output, error = EXCLUDED.error, ...;
