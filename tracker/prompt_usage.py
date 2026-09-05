"""Read-only, disposable turn index. Never stores prompt text or changes tokens.db."""
from __future__ import annotations

import bisect
import copy
import json
import math
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import parse_codex
from .pricing import cost_usd, PRICES_PATH, reload as reload_prices


def instant(value):
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError, TypeError):
        return None


def uncertain(status):
    return "unverified" in status or "partial_fields" in status or status.startswith("cumulative_corrected")


def read_turns(path):
    """Join reconciled usage and MCP invocation lines to explicit/inferred turns."""
    # Bound both passes to the same prefix; the index additionally checks stat changes.
    size = path.stat().st_size
    parsed, consumed = parse_codex.parse_file(path, end_offset=size)
    events = []
    with path.open("rb") as f:
        for n, raw in enumerate(f.read(consumed).splitlines(), 1):
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            p = d.get("payload") or {}
            # Retain structural fields only; no conversations/tool output in the index.
            events.append((n, d.get("timestamp"), d.get("type"), {
                k: p.get(k) for k in ("type", "turn_id", "role")
            }))
    has_user_events = any(t == "event_msg" and p["type"] == "user_message" for _, _, t, p in events)
    turns = []
    active = None
    by_id = {}

    def start(n, ts, tid=None, explicit=False):
        nonlocal active
        if active and active["status"] == "running":
            active["status"] = "unknown_end"
        active = dict(turn_id=tid or f"inferred:{n}", start_line=n, started_at=ts,
                      ended_at=None, status="running", boundary="explicit" if explicit else "approximate",
                      explicit_start=explicit, rows=[], calls=[])
        turns.append(active)
        if tid:
            by_id[tid] = active

    for n, ts, t, p in events:
        pt, tid = p["type"], p["turn_id"]
        if t == "event_msg" and pt in ("task_started", "turn_started"):
            if tid and tid in by_id:
                active = by_id[tid]
            else:
                start(n, ts, tid, bool(tid))
        elif t == "turn_context" and tid:
            if active and active["turn_id"].startswith("inferred:") and active["status"] == "running":
                active["turn_id"] = tid
                by_id[tid] = active
            elif not active or active["turn_id"] != tid:
                start(n, ts, tid)
        elif ((t == "event_msg" and pt == "user_message") or
              (not has_user_events and t == "response_item" and pt == "message" and p["role"] == "user")):
            # User steering inside an explicitly identified running turn is part of that turn.
            if not active or active["status"] != "running" or active["turn_id"].startswith("inferred:"):
                start(n, ts)
        elif t == "event_msg" and pt in ("task_complete", "turn_completed", "turn_aborted"):
            target = by_id.get(tid) if tid else active
            if target:
                target["status"] = "aborted" if pt == "turn_aborted" else "completed"
                target["ended_at"] = ts
                if not tid or not target["explicit_start"]:
                    target["boundary"] = "approximate"

    starts = [t["start_line"] for t in turns]
    unmatched = 0
    for rows, key in ((parsed.messages, "rows"), (parsed.mcp_calls, "calls")):
        for row in rows:
            i = bisect.bisect_right(starts, row.source_line) - 1
            if i >= 0:
                turns[i][key].append(row)
            elif key == "rows":
                unmatched += 1
    reports = []
    for turn in turns:
        rows, calls = turn.pop("rows"), turn.pop("calls")
        turn.pop("explicit_start")
        turn.pop("start_line")
        costs = [None if uncertain(r.usage_status) else cost_usd(
            "codex", r.model, input_tokens=r.input_tokens, cache_read=r.cache_read,
            output_tokens=r.output_tokens, cache_write_input_tokens=r.cache_write_input_tokens,
        ) for r in rows]
        fresh = sum(r.input_tokens for r in rows)
        cached = sum(r.cache_read for r in rows)
        output = sum(r.output_tokens for r in rows)
        unpriced = sum(c is None for c in costs)
        turn.update(
            session_id=parsed.session.session_id, project=parsed.session.cwd,
            agent_type=parsed.session.session_kind, originator=parsed.session.originator,
            models=sorted({r.model or "unknown" for r in rows}),
            reasoning_efforts=sorted({r.reasoning_effort or "unknown" for r in rows}),
            usage_steps=len(rows), uncertain_usage_steps=sum(uncertain(r.usage_status) or r.usage_status.startswith("initial_total") for r in rows),
            tokens=dict(fresh_input=fresh, cached_input=cached, input=fresh + cached, output=output,
                        reasoning_in_output=sum(r.reasoning_tokens for r in rows), total=fresh + cached + output),
            pricing=dict(known_api_equivalent_usd=round(sum(c for c in costs if c is not None), 8) if len(costs) > unpriced else None,
                         priced_steps=len(costs) - unpriced, unpriced_steps=unpriced,
                         coverage="no_usage" if not rows else "unpriced" if unpriced == len(costs) else "partial" if unpriced else "priced"),
            mcp=dict(calls=len(calls), errors=sum(c.is_error for c in calls)),
        )
        reports.append(turn)
    return reports, unmatched


class PromptIndex:
    def __init__(self, discover=None):
        self.discover = discover or parse_codex.discover_files
        self.cache = {}
        self.lock = threading.Lock()
        self.price_signature = None

    def snapshot(self):
        with self.lock:
            price_stat = PRICES_PATH.stat()
            price_signature = (price_stat.st_mtime_ns, price_stat.st_size)
            if price_signature != self.price_signature:
                reload_prices()
                self.cache.clear()
                self.price_signature = price_signature
            paths = self.discover()
            errors = 0
            reports = []
            unmatched = 0
            for path in paths:
                try:
                    st = path.stat()
                    signature = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                    saved = self.cache.get(path)
                    if not saved or saved[0] != signature:
                        data, missing = read_turns(path)
                        after = path.stat()
                        if signature != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                            # A writer raced the two passes. Don't return mismatched boundaries/usage.
                            errors += 1
                            continue
                        self.cache[path] = signature, data, missing
                    _, data, missing = self.cache[path]
                    reports.extend(data)
                    unmatched += missing
                except (OSError, ValueError, TypeError, AttributeError):
                    errors += 1
            path_set = set(paths)
            self.cache = {p: v for p, v in self.cache.items() if p in path_set}
            # Copied/resumed files may contain identical turns. Never silently sum them.
            grouped = {}
            for r in reports:
                grouped.setdefault((r["session_id"], r["turn_id"]), []).append(r)
            unique = []
            ambiguous = 0
            for items in grouped.values():
                if all(r == items[0] for r in items):
                    unique.append(items[0])
                else:
                    ambiguous += 1
            return copy.deepcopy(unique), dict(files=len(paths), unavailable_files=errors,
                conflicting_turns=ambiguous, unattributed_usage_steps=unmatched,
                refreshed_at=datetime.now(timezone.utc).isoformat(), storage="derived_from_local_logs")


def warnings_for(target, reports, *, days=30, min_samples=20, percentile=95, multiplier=2.0):
    start = instant(target["started_at"])
    signature = ("project", "agent_type", "models", "reasoning_efforts")
    peers = [r for r in reports if start and r is not target
             and (r["session_id"], r["turn_id"]) != (target["session_id"], target["turn_id"])
             and all(r[k] == target[k] for k in signature)
             and r["status"] == "completed" and r["boundary"] == "explicit"
             and r["usage_steps"] and not r["uncertain_usage_steps"]
             and instant(r["started_at"]) and instant(r["ended_at"])
             and start - timedelta(days=days) <= instant(r["started_at"])
             and instant(r["ended_at"]) <= start]
    result = dict(status="insufficient_history" if len(peers) < min_samples else "normal",
                  sample_count=len(peers), required_samples=min_samples, lookback_days=days,
                  percentile=percentile, median_multiplier=multiplier, comparisons={}, warnings=[])
    if target["boundary"] != "explicit" or target["uncertain_usage_steps"] or not target["usage_steps"]:
        result["status"] = "uncertain_usage"
        return result
    for metric in ("fresh_input", "output", "total", "known_api_equivalent_usd"):
        if metric == "known_api_equivalent_usd":
            if target["pricing"]["coverage"] != "priced":
                continue
            values = [r["pricing"][metric] for r in peers if r["pricing"]["coverage"] == "priced"]
            value = target["pricing"][metric]
        else:
            values = [r["tokens"][metric] for r in peers]
            value = target["tokens"][metric]
        if len(values) < min_samples:
            continue
        median = statistics.median(values)
        p = sorted(values)[max(0, math.ceil(len(values) * percentile / 100) - 1)]
        threshold = max(p, median * multiplier)
        result["comparisons"][metric] = dict(sample_count=len(values), median=median,
            percentile_value=p, threshold=threshold, current=value)
        if value > threshold:
            result["warnings"].append(dict(metric=metric, current=value, threshold=threshold,
                ratio_to_median=value / median if median else None))
    if result["warnings"]:
        result["status"] = "high"
    return result


index = PromptIndex()
