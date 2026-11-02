"""Token usage extraction and cost estimation.

AgentTrace ships with *no* built-in price list: prices change and differ per
contract. Register the prices you actually pay with :func:`set_price`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

_PRICES: Dict[str, Tuple[float, float]] = {}


def set_price(model: str, input_per_1k: float, output_per_1k: float) -> None:
    """Register USD price per 1,000 input / output tokens for ``model``."""
    _PRICES[model] = (float(input_per_1k), float(output_per_1k))


def clear_prices() -> None:
    _PRICES.clear()


def estimate_cost(model: Optional[str], tokens_in: int, tokens_out: int) -> Optional[float]:
    price = _PRICES.get(model or "")
    if price is None:
        return None
    return (tokens_in / 1000.0) * price[0] + (tokens_out / 1000.0) * price[1]


def extract_usage(result: Any) -> Optional[Tuple[int, int, Optional[str]]]:
    """Find ``(tokens_in, tokens_out, model)`` in an LLM response.

    Understands dicts and objects exposing ``usage`` with either
    ``input_tokens/output_tokens`` or ``prompt_tokens/completion_tokens``.
    Returns ``None`` when no usage information is present.
    """
    if isinstance(result, dict):
        usage, model = result.get("usage"), result.get("model")
    else:
        usage, model = getattr(result, "usage", None), getattr(result, "model", None)
    if usage is None:
        return None

    def pick(*names: str) -> Optional[int]:
        for n in names:
            v = usage.get(n) if isinstance(usage, dict) else getattr(usage, n, None)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
        return None

    tin = pick("input_tokens", "prompt_tokens")
    tout = pick("output_tokens", "completion_tokens")
    if tin is None and tout is None:
        return None
    return tin or 0, tout or 0, model if isinstance(model, str) else None
