"""Command line interface: ``agenttrace runs | show | export | insights | diff | serve``."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .differ import diff_traces
from .insights import analyze
from .render import fmt_ms, fmt_ts, render_tree
from .serialize import dumps
from .storage import SQLiteStore


def _resolve(store: SQLiteStore, ident: str) -> str:
    tid = store.resolve_trace_id(ident)
    if tid is None:
        raise SystemExit(f"error: no unique run matches {ident!r} (try `agenttrace runs`)")
    return tid


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="agenttrace", description="Record, replay and debug AI agents.")
    p.add_argument("--db", default=os.environ.get("AGENTTRACE_DB", "agenttrace.db"),
                   help="SQLite file (default: $AGENTTRACE_DB or ./agenttrace.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("runs", help="list recent runs")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--since", type=float, default=None,
                   help="only runs started at or after this epoch second")
    s.add_argument("--status", default=None, choices=("ok", "error", "running"),
                   help="only runs with this status")
    s = sub.add_parser("show", help="print the span tree of a run")
    s.add_argument("run")
    s.add_argument("--io", action="store_true", help="also print (truncated) inputs and outputs")
    s = sub.add_parser("export", help="export a run as JSON")
    s.add_argument("run")
    s.add_argument("--pretty", action="store_true", help="pretty-print the JSON instead of compact")
    s = sub.add_parser("insights", help="loops, errors, slow and token-heavy steps")
    s.add_argument("run")
    s = sub.add_parser("diff", help="where do two runs start to differ?")
    s.add_argument("a")
    s.add_argument("b")
    s = sub.add_parser("serve", help="start the HTTP API + web viewer")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    args = p.parse_args(argv)
    if args.db != ":memory:" and not os.path.exists(args.db):
        print(f"error: database {args.db!r} not found (use --db or run an instrumented agent first)", file=sys.stderr)
        return 2
    store = SQLiteStore(args.db)

    if args.cmd == "runs":
        rows = store.list_traces(limit=args.limit, since=args.since, status=args.status)
        print(f"{'ID':<10} {'NAME':<24} {'STATUS':<8} {'SPANS':>5} {'ERR':>3} {'TOKENS':>7} {'DURATION':>9}  STARTED")
        for r in rows:
            name = ("replay:" if r["replay_of"] else "") + r["name"].replace("replay:", "")
            print(f"{r['trace_id'][:8]:<10} {name[:24]:<24} {r['status']:<8} {r['span_count']:>5} {r['error_count']:>3} "
                  f"{r['tokens_in'] + r['tokens_out']:>7} {fmt_ms(r['duration_ms']):>9}  {fmt_ts(r['start_ts'])}")
        if not rows:
            print("(no runs yet)")
    elif args.cmd == "show":
        tid = _resolve(store, args.run)
        print(render_tree(store.get_spans(tid), show_io=args.io))
    elif args.cmd == "export":
        tid = _resolve(store, args.run)
        payload = {
            "run": store.get_trace(tid),
            "spans": [s.to_dict() for s in store.get_spans(tid)],
        }
        print(dumps(payload) if not args.pretty else json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.cmd == "insights":
        tid = _resolve(store, args.run)
        found = analyze(store.get_spans(tid))
        if not found:
            print("no issues found")
        for i in found:
            print(f"[{i.severity.upper():5}] {i.kind}: {i.message}")
    elif args.cmd == "diff":
        a, b = _resolve(store, args.a), _resolve(store, args.b)
        d = diff_traces(store.get_spans(a), store.get_spans(b))
        sm = d.summary()
        print(f"same={sm['same']} changed={sm['changed']} added={sm['added']} removed={sm['removed']} "
              f"| duration {sm['duration_delta_ms']:+.1f} ms | tokens {sm['tokens_delta']:+d}")
        fd = d.first_divergence
        print("runs are identical" if fd is None else f"first divergence: {fd.kind} '{fd.name}' call #{fd.nth} ({fd.status})")
        for e in d.entries:
            if e.status != "same" or e.notes:
                print(f"  {e.status:<8} {e.kind} '{e.name}' #{e.nth}: {'; '.join(e.changes + e.notes)}")
    elif args.cmd == "serve":
        try:
            import uvicorn

            from .api import create_app
        except ImportError:
            print('error: server extras missing. Run: pip install "agenttrace[server]"', file=sys.stderr)
            return 2
        print(f"AgentTrace viewer on http://{args.host}:{args.port}  (db: {args.db})")
        uvicorn.run(create_app(store), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
