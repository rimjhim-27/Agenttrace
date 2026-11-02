"""Plain-text rendering helpers used by the CLI and the demo (ASCII only: safe on any terminal)."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .models import Span


def fmt_ms(ms: Optional[float]) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.2f}s" if ms >= 1000 else f"{ms:.1f}ms"


def fmt_ts(ts: Optional[float]) -> str:
    return "-" if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _short(value: Any, width: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=repr)
    return text if len(text) <= width else text[: width - 3] + "..."


def render_tree(spans: List[Span], show_io: bool = False, io_width: int = 70) -> str:
    ids = {s.span_id for s in spans}
    children: Dict[str, List[Span]] = defaultdict(list)
    roots: List[Span] = []
    for s in sorted(spans, key=lambda x: x.seq):
        if s.parent_id and s.parent_id in ids:
            children[s.parent_id].append(s)
        else:
            roots.append(s)

    lines: List[str] = []

    def label(s: Span) -> str:
        parts = [f"[{s.kind}] {s.name}  {fmt_ms(s.duration_ms)}"]
        if s.tokens:
            parts.append(f"tokens={s.attrs.get('tokens_in', 0)}/{s.attrs.get('tokens_out', 0)}")
        if s.attrs.get("cost_usd") is not None:
            parts.append(f"cost=${s.attrs['cost_usd']:.5f}")
        if s.attrs.get("replayed"):
            parts.append("(replayed)")
        if s.error:
            parts.append(f"ERROR: {s.error}")
        return "  ".join(parts)

    def walk(s: Span, prefix: str, is_last: bool, is_root: bool) -> None:
        branch = "" if is_root else ("`- " if is_last else "|- ")
        lines.append(f"{prefix}{branch}{label(s)}")
        child_prefix = prefix + ("" if is_root else ("   " if is_last else "|  "))
        if show_io:
            lines.append(f"{child_prefix}   in : {_short(s.input, io_width)}")
            lines.append(f"{child_prefix}   out: {_short(s.output, io_width)}")
        kids = children.get(s.span_id, [])
        for i, k in enumerate(kids):
            walk(k, child_prefix, i == len(kids) - 1, False)

    for r in roots:
        walk(r, "", True, True)
    return "\n".join(lines)
