"""Turn arbitrary Python objects into JSON-able snapshots (never raises).

Two things matter for replay and diffing:

* snapshots must be *deterministic* -> ``repr`` fallbacks have memory addresses
  (``<Foo object at 0x7f..>``) stripped, dict keys are stringified, sets sorted;
* snapshots must be *bounded* -> long strings are truncated.
"""
from __future__ import annotations

import dataclasses
import json
import math
import re
from typing import Any

_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")
MAX_DEPTH = 8
DEFAULT_MAX_CHARS = 20_000


def _truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"...[truncated {len(s) - max_chars} chars]"


def to_jsonable(obj: Any, max_chars: int = DEFAULT_MAX_CHARS, _depth: int = 0) -> Any:
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else repr(obj)
    if isinstance(obj, str):
        return _truncate(obj, max_chars)
    if _depth >= MAX_DEPTH:
        return "<max depth reached>"
    if isinstance(obj, (bytes, bytearray)):
        return f"<bytes len={len(obj)}>"
    nxt = _depth + 1
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, max_chars, nxt) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v, max_chars, nxt) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((to_jsonable(v, max_chars, nxt) for v in obj), key=repr)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        try:
            return to_jsonable(dataclasses.asdict(obj), max_chars, nxt)
        except Exception:
            pass
    for attr in ("model_dump", "dict"):  # pydantic v2 / v1
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return to_jsonable(method(), max_chars, nxt)
            except Exception:
                pass
    try:
        text = _ADDR.sub("", repr(obj))
    except Exception:
        text = f"<unrepresentable {type(obj).__name__}>"
    return _truncate(text, max_chars)


def dumps(obj: Any) -> str:
    """Canonical JSON text: sorted keys, compact separators, UTF-8 friendly."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=repr)


def canonical(obj: Any) -> str:
    """Canonical text of any object; equal snapshots always give equal text."""
    return dumps(to_jsonable(obj))
