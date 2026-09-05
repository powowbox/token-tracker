"""Per-model price lookup; computes USD cost for one message."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PRICES_PATH = Path(__file__).resolve().parent.parent / "prices.json"


@lru_cache(maxsize=1)
def _prices() -> dict:
    with open(PRICES_PATH) as f:
        return json.load(f)


def reload() -> None:
    _prices.cache_clear()


def _lookup(tool: str, model: str | None) -> dict:
    """Exact IDs and explicitly verified aliases only; unknown returns no rates."""
    if not model or model.startswith("_"):
        return {}
    data = _prices()
    canonical = data.get("aliases", {}).get(tool, {}).get(model, model)
    price = data.get(tool, {}).get(canonical, {})
    if tool == "codex" and not price.get("source"):
        return {}
    return price


def cost_breakdown(tool, model, *, input_tokens=0, output_tokens=0, cache_read=0,
                   cache_write_5m=0, cache_write_1h=0, cache_write_input_tokens=0):
    """Current standard API-equivalent estimate, never billing/subscription usage.

    Codex cache writes are a subset of fresh input, so apply their premium only.
    Other vendors' write buckets remain separate from input.
    """
    p = _lookup(tool, model)
    if not p:
        return None
    if any(v < 0 for v in (input_tokens, output_tokens, cache_read, cache_write_5m, cache_write_1h, cache_write_input_tokens)):
        return None
    if tool == "codex" and cache_write_input_tokens > input_tokens:
        return None
    long = input_tokens + cache_read > p.get("long_context_threshold", float("inf"))
    inf = p.get("long_input_multiplier", 1) if long else 1
    outf = p.get("long_output_multiplier", 1) if long else 1
    write = cache_write_input_tokens if tool == "codex" else 0
    if write and "cache_write" not in p:
        return None
    if cache_write_5m and "cache_write_5m" not in p or cache_write_1h and "cache_write_1h" not in p:
        return None
    return {
        "input": (input_tokens-write)*p["input"]*inf/1e6,
        "cache_read": cache_read*p["cache_read"]*inf/1e6,
        "output": output_tokens*p["output"]*outf/1e6,
        "cache_write_5m": (cache_write_5m*p.get("cache_write_5m",0)+write*p.get("cache_write",0)*inf)/1e6,
        "cache_write_1h": cache_write_1h*p.get("cache_write_1h",0)/1e6,
    }


def cost_usd(tool, model, **usage) -> float | None:
    parts = cost_breakdown(tool, model, **usage)
    return round(sum(parts.values()), 6) if parts is not None else None
