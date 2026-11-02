"""HTTP API (FastAPI). Install with ``pip install "agenttrace[server]"``.

Not using ``from __future__ import annotations`` on purpose: FastAPI/pydantic resolve
the annotations of module-level models and endpoints at import time.
"""
import hmac
import os
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import __version__
from .differ import diff_traces
from .insights import analyze
from .models import Span
from .ui import INDEX_HTML


class SpanIn(BaseModel):
    span_id: str
    trace_id: str
    name: str
    kind: Literal["agent", "llm", "tool", "custom"] = "custom"
    parent_id: Optional[str] = None
    seq: int = 0
    start_ts: float = 0.0
    end_ts: float = 0.0
    duration_ms: float = 0.0
    input: Any = None
    output: Any = None
    error: Optional[str] = None
    attrs: Dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    spans: List[SpanIn] = Field(min_length=1, max_length=5000)
    trace_name: Optional[str] = None  # name used if the trace is new
    finish: bool = False  # mark the trace(s) as finished (status "ok")


def create_app(store: Any, *, api_key: Optional[str] = None) -> FastAPI:
    """Build the app around a store. ``api_key`` (or ``$AGENTTRACE_API_KEY``) enables ``X-API-Key`` auth."""
    key = api_key if api_key is not None else os.environ.get("AGENTTRACE_API_KEY")
    app = FastAPI(title="AgentTrace", version=__version__,
                  description="Record, replay and debug AI agents.")

    def auth(x_api_key: Optional[str] = Header(default=None)) -> None:
        if key and not (x_api_key and hmac.compare_digest(x_api_key, key)):
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    def load(trace_id: str) -> List[Span]:
        spans = store.get_spans(trace_id)
        if not spans and store.get_trace(trace_id) is None:
            raise HTTPException(status_code=404, detail=f"run {trace_id!r} not found")
        return spans

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return INDEX_HTML

    @app.get("/api/v1/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", "version": __version__, "auth": bool(key)}

    @app.post("/api/v1/spans", status_code=202, dependencies=[Depends(auth)])
    def ingest(req: IngestRequest) -> Dict[str, Any]:
        """Idempotent batch ingestion: re-sending the same span_ids overwrites, never duplicates."""
        spans = [Span.from_dict(s.model_dump()) for s in req.spans]
        first: Dict[str, float] = {}
        last: Dict[str, float] = {}
        for s in spans:
            first[s.trace_id] = min(first.get(s.trace_id, s.start_ts), s.start_ts)
            last[s.trace_id] = max(last.get(s.trace_id, s.end_ts), s.end_ts)
        for tid, start in first.items():
            store.ensure_trace(tid, req.trace_name or "ingested", start)
        items: List[Any] = [("span", s) for s in spans]
        if req.finish:
            for tid, start in first.items():
                items.append(("trace", {"trace_id": tid, "name": req.trace_name or "ingested",
                                        "start_ts": start, "end_ts": last[tid], "status": "ok"}))
        store.apply(items)
        return {"accepted": len(spans), "traces": sorted(first)}

    @app.get("/api/v1/runs", dependencies=[Depends(auth)])
    def list_runs(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> Dict[str, Any]:
        return {"runs": store.list_traces(limit=limit, offset=offset), "total": store.count_traces(),
                "limit": limit, "offset": offset}

    @app.get("/api/v1/runs/{trace_id}", dependencies=[Depends(auth)])
    def get_run(trace_id: str) -> Dict[str, Any]:
        spans = load(trace_id)
        return {"run": store.get_trace(trace_id), "spans": [s.to_dict() for s in spans]}

    @app.get("/api/v1/runs/{trace_id}/insights", dependencies=[Depends(auth)])
    def get_insights(trace_id: str) -> Dict[str, Any]:
        return {"insights": [i.to_dict() for i in analyze(load(trace_id))]}

    @app.get("/api/v1/diff", dependencies=[Depends(auth)])
    def get_diff(a: str, b: str) -> Dict[str, Any]:
        return diff_traces(load(a), load(b)).to_dict()

    return app
