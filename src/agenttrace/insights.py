"""Rule-based failure insights for one trace.

Deliberately simple and explainable (no ML): every insight names the rule that fired
and the spans involved, so a developer can verify it in seconds.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Tuple

from .models import Span
from .serialize import canonical


@dataclass
class Insight:
    kind: str  # error | loop | retry | slow | token_heavy
    severity: str  # error | warn | info
    message: str
    span_ids: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_WORD = re.compile(r"\w+")


def _tokens(obj: Any) -> set:
    """Word tokens in the canonical form of an object (deterministic)."""
    return set(_WORD.findall(canonical(obj)))


def _jaccard(a: Any, b: Any) -> float:
    """Token-overlap similarity between two snapshots; 1.0 = identical."""
    ta, tb = _tokens(a), _tokens(b)
    union = len(ta | tb)
    return (len(ta & tb) / union) if union else 1.0


def analyze(spans: List[Span], *, loop_threshold: int = 3, slow_ms: float = 2000.0,
            heavy_share: float = 0.5, min_llm_calls: int = 3,
            near_threshold: float = 0.9, near_min: int = 3) -> List[Insight]:
    out: List[Insight] = []
    ordered = sorted(spans, key=lambda s: s.seq)

    # 1. failures
    for s in ordered:
        if s.error:
            out.append(Insight("error", "error", f"{s.kind} '{s.name}' failed: {s.error}", [s.span_id]))

    # 2. loops: the same call with byte-identical input, again and again
    groups: Dict[Tuple[str, str, str], List[Span]] = defaultdict(list)
    for s in ordered:
        if s.kind in ("llm", "tool"):
            groups[(s.kind, s.name, canonical(s.input))].append(s)
    for (kind, name, _), items in groups.items():
        if len(items) >= loop_threshold:
            out.append(Insight(
                "loop", "warn",
                f"{kind} '{name}' was called {len(items)} times with identical input - possible loop",
                [s.span_id for s in items]))

    # 3. retries: a call that failed and later succeeded
    by_call: Dict[Tuple[str, str], List[Span]] = defaultdict(list)
    for s in ordered:
        if s.kind in ("llm", "tool"):
            by_call[(s.kind, s.name)].append(s)
    for (kind, name), items in by_call.items():
        failed_before_success = 0
        failed_ids: List[str] = []
        for s in items:
            if s.error:
                failed_before_success += 1
                failed_ids.append(s.span_id)
            elif failed_before_success:
                out.append(Insight(
                    "retry", "info",
                    f"{kind} '{name}' failed {failed_before_success} time(s) before succeeding",
                    failed_ids + [s.span_id]))
                break

    # 4. slow steps (the root agent span is expected to be the slowest, so skip it)
    for s in ordered:
        if s.kind != "agent" and s.duration_ms >= slow_ms:
            out.append(Insight("slow", "warn", f"{s.kind} '{s.name}' took {s.duration_ms:.0f} ms", [s.span_id]))

    # 5. one LLM call that dominates token usage
    llm = [s for s in ordered if s.kind == "llm" and s.tokens > 0]
    total = sum(s.tokens for s in llm)
    if len(llm) >= min_llm_calls and total > 0:
        top = max(llm, key=lambda s: s.tokens)
        if top.tokens / total >= heavy_share:
            out.append(Insight(
                "token_heavy", "info",
                f"llm '{top.name}' used {top.tokens} of {total} tokens ({top.tokens / total:.0%}) in this run",
                [top.span_id]))

    # 6. near-identical LLM prompts: the same prompt sent again and again
    #    (token overlap >= near_threshold) - a sign the agent is re-asking the
    #    same question instead of progressing. Distinct from the "loop" rule,
    #    which only fires on byte-identical input.
    llm_spans = [s for s in ordered if s.kind == "llm" and s.input is not None]
    clusters: List[List[Span]] = []
    for s in llm_spans:
        for c in clusters:
            if _jaccard(c[0].input, s.input) >= near_threshold:
                c.append(s)
                break
        else:
            clusters.append([s])
    for c in clusters:
        if len(c) >= near_min:
            out.append(Insight(
                "near_duplicate", "warn",
                f"llm '{c[0].name}' was called {len(c)} times with near-identical prompts "
                f"(>= {near_threshold:.0%} overlap) - possible re-asking loop",
                [s.span_id for s in c]))
    return out
