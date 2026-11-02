"""SQLite storage backend (standard library only).

The rest of the package only depends on the small surface below (``apply``,
``get_trace``, ``list_traces``, ``get_spans``, ``ensure_trace``,
``resolve_trace_id``, ``close``), so a PostgreSQL backend can be added later
without touching the SDK. See ``docs/schema_postgres.sql`` for a draft schema.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import Span
from .serialize import dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id  TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    start_ts  REAL NOT NULL,
    end_ts    REAL,
    status    TEXT NOT NULL DEFAULT 'running',
    replay_of TEXT,
    attrs     TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS spans (
    span_id     TEXT PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    parent_id   TEXT,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    start_ts    REAL NOT NULL,
    end_ts      REAL NOT NULL,
    duration_ms REAL NOT NULL,
    input       TEXT,
    output      TEXT,
    error       TEXT,
    attrs       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_spans_trace_seq ON spans(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_traces_start ON traces(start_ts DESC);
"""

_UPSERT_TRACE = """
INSERT INTO traces(trace_id, name, start_ts, end_ts, status, replay_of, attrs)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(trace_id) DO UPDATE SET
    name = excluded.name,
    end_ts = excluded.end_ts,
    status = excluded.status,
    replay_of = excluded.replay_of,
    attrs = excluded.attrs
"""

# INSERT OR REPLACE keyed by span_id makes ingestion idempotent: re-sending a
# batch (client retry) never creates duplicates.
_INSERT_SPAN = """
INSERT OR REPLACE INTO spans(span_id, trace_id, parent_id, name, kind, seq,
    start_ts, end_ts, duration_ms, input, output, error, attrs)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SUMMARY = """
SELECT t.trace_id, t.name, t.start_ts, t.end_ts, t.status, t.replay_of, t.attrs,
       COUNT(s.span_id) AS span_count,
       COALESCE(SUM(CASE WHEN s.error IS NOT NULL THEN 1 ELSE 0 END), 0) AS error_count,
       COALESCE(SUM(json_extract(s.attrs, '$.tokens_in')), 0) AS tokens_in,
       COALESCE(SUM(json_extract(s.attrs, '$.tokens_out')), 0) AS tokens_out,
       COALESCE(SUM(json_extract(s.attrs, '$.cost_usd')), 0) AS cost_usd
FROM traces t LEFT JOIN spans s ON s.trace_id = t.trace_id
{where}
GROUP BY t.trace_id
ORDER BY t.start_ts DESC
{limit}
"""


def _j(value: Any) -> Optional[str]:
    return None if value is None else dumps(value)


def _load(text: Optional[str]) -> Any:
    return None if text is None else json.loads(text)


def _span_params(s: Span) -> Tuple[Any, ...]:
    return (
        s.span_id, s.trace_id, s.parent_id, s.name, s.kind, s.seq,
        s.start_ts, s.end_ts, s.duration_ms, _j(s.input), _j(s.output), s.error, dumps(s.attrs),
    )


def _row_to_span(r: sqlite3.Row) -> Span:
    return Span(
        span_id=r["span_id"], trace_id=r["trace_id"], parent_id=r["parent_id"],
        name=r["name"], kind=r["kind"], seq=r["seq"], start_ts=r["start_ts"],
        end_ts=r["end_ts"], duration_ms=r["duration_ms"], input=_load(r["input"]),
        output=_load(r["output"]), error=r["error"], attrs=_load(r["attrs"]) or {},
    )


def _row_to_summary(r: sqlite3.Row) -> Dict[str, Any]:
    end = r["end_ts"]
    return {
        "trace_id": r["trace_id"],
        "name": r["name"],
        "status": r["status"],
        "start_ts": r["start_ts"],
        "end_ts": end,
        "duration_ms": None if end is None else (end - r["start_ts"]) * 1000.0,
        "replay_of": r["replay_of"],
        "span_count": r["span_count"],
        "error_count": r["error_count"],
        "tokens_in": int(r["tokens_in"]),
        "tokens_out": int(r["tokens_out"]),
        "cost_usd": round(float(r["cost_usd"]), 6),
        "attrs": _load(r["attrs"]) or {},
    }


class SQLiteStore:
    """Thread-safe SQLite store. Use ``":memory:"`` for tests and demos."""

    def __init__(self, path: str = "agenttrace.db") -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ writes
    def apply(self, items: Iterable[Tuple[str, Any]]) -> None:
        """Write a batch of ``("span", Span)`` / ``("trace", dict)`` items in ONE transaction."""
        with self._lock, self._conn:
            pending: List[Tuple[Any, ...]] = []
            for kind, payload in items:
                if kind == "span":
                    pending.append(_span_params(payload))
                    continue
                if pending:  # keep ordering: spans before a later trace update
                    self._conn.executemany(_INSERT_SPAN, pending)
                    pending = []
                if kind == "trace":
                    self._conn.execute(
                        _UPSERT_TRACE,
                        (
                            payload["trace_id"], payload["name"], payload["start_ts"],
                            payload.get("end_ts"), payload.get("status", "running"),
                            payload.get("replay_of"), dumps(payload.get("attrs") or {}),
                        ),
                    )
            if pending:
                self._conn.executemany(_INSERT_SPAN, pending)

    def ensure_trace(self, trace_id: str, name: str, start_ts: float) -> None:
        """Create the trace row if missing (used by remote ingestion)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO traces(trace_id, name, start_ts, status) VALUES (?, ?, ?, 'running')",
                (trace_id, name, start_ts),
            )

    # ------------------------------------------------------------------- reads
    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        sql = _SUMMARY.format(where="WHERE t.trace_id = ?", limit="")
        with self._lock:
            row = self._conn.execute(sql, (trace_id,)).fetchone()
        return None if row is None else _row_to_summary(row)

    def list_traces(self, limit: int = 50, offset: int = 0, *,
                    since: Optional[float] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List traces, newest first. ``since`` is an epoch-second lower bound on
        ``start_ts``; ``status`` matches the ``status`` column exactly (``ok``/``error``/``running``)."""
        clauses: List[str] = []
        params: List[Any] = []
        if since is not None:
            clauses.append("t.start_ts >= ?")
            params.append(float(since))
        if status is not None:
            clauses.append("t.status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = _SUMMARY.format(where=where, limit="LIMIT ? OFFSET ?")
        params.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_summary(r) for r in rows]

    def count_traces(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0])

    def get_spans(self, trace_id: str) -> List[Span]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY seq", (trace_id,)
            ).fetchall()
        return [_row_to_span(r) for r in rows]

    def resolve_trace_id(self, prefix: str) -> Optional[str]:
        """Exact id, or a unique prefix of one (handy for the CLI)."""
        with self._lock:
            exact = self._conn.execute("SELECT trace_id FROM traces WHERE trace_id = ?", (prefix,)).fetchone()
            if exact:
                return exact[0]
            esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = self._conn.execute(
                "SELECT trace_id FROM traces WHERE trace_id LIKE ? ESCAPE '\\' LIMIT 2", (esc + "%",)
            ).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
