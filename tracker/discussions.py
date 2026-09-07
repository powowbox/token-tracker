"""Lifetime discussion totals from imported usage; read-only title/parent metadata.

No snapshot summation, repricing, ingestion, or persistent schema changes here.
Metadata caches contain titles and identifiers only, never conversation content.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from .first_prompts import previews as first_prompt_previews


class DiscussionMetadata:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.lock = threading.RLock()
        self.files = {}
        self.refreshed = -float("inf")
        self.titles = {}
        self.edges = {}
        self.available = False

    @staticmethod
    def parent_ids(payload):
        """Only explicit relationship fields, never names/paths/text heuristics."""
        values = [payload.get("parent_thread_id")]
        source = payload.get("source")
        if isinstance(source, dict):
            subagent = source.get("subagent")
            if isinstance(subagent, dict):
                spawn = subagent.get("thread_spawn")
                if isinstance(spawn, dict):
                    values.append(spawn.get("parent_thread_id"))
        return {"codex:" + v.removeprefix("codex:") for v in values if isinstance(v, str) and v.strip()}

    def _refresh(self):
        if time.monotonic() - self.refreshed < 5:
            return
        titles, edges, available = {}, defaultdict(set), False
        # Newest supported state schema wins. Do not read title/first_user_message.
        for path in sorted(self.home.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1, reverse=True):
            try:
                conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
                    if not {"id", "name"} <= cols:
                        continue
                    for sid, name in conn.execute("SELECT id, name FROM threads WHERE name IS NOT NULL AND name != ''"):
                        titles["codex:" + sid] = (name, "threads.name")
                    available = True
                    edge_cols = {r[1] for r in conn.execute("PRAGMA table_info(thread_spawn_edges)")}
                    if {"parent_thread_id", "child_thread_id"} <= edge_cols:
                        for parent, child in conn.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
                            if parent and child:
                                edges["codex:" + child].add("codex:" + parent)
                    break
                finally:
                    conn.close()
            except (OSError, sqlite3.Error):
                continue
        # JSONL index takes priority over the state database, including renames.
        latest = {}
        try:
            with (self.home / "session_index.jsonl").open(encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                        sid, name = item.get("id"), item.get("thread_name")
                        stamp = str(item.get("updated_at", ""))
                        if isinstance(sid, str) and isinstance(name, str) and name.strip():
                            if sid not in latest or stamp >= latest[sid][0]:
                                latest[sid] = (stamp, name)
                    except (ValueError, AttributeError):
                        continue
            available = True
        except OSError:
            pass
        for sid, (_, name) in latest.items():
            titles["codex:" + sid] = (name, "thread_name")
        self.titles, self.edges, self.available = titles, dict(edges), available
        self.refreshed = time.monotonic()

    def _file(self, path):
        try:
            stat = path.stat()
            sig = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            cached = self.files.get(str(path))
            if cached and cached[0] == sig:
                return cached[1]
            value = None
            with path.open("rb") as f:
                # Session metadata normally is the first line. Bounded header read.
                for _ in range(32):
                    line = f.readline(2_000_001)
                    if not line or len(line) > 2_000_000:
                        break
                    try:
                        event = json.loads(line)
                        if event.get("type") == "session_meta":
                            payload = event.get("payload", {})
                            value = ("codex:" + payload["id"], self.parent_ids(payload))
                            break
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
            self.files[str(path)] = (sig, value)
            return value
        except OSError:
            return None

    def snapshot(self, sources):
        with self.lock:
            self._refresh()
            edges = {k: set(v) for k, v in self.edges.items()}
            missing = set()
            active = set()
            for sid, path in sources:
                active.add(path)
                found = self._file(Path(path))
                if found is None or found[0] != sid:
                    missing.add(sid)
                else:
                    edges.setdefault(sid, set()).update(found[1])
            self.files = {p: v for p, v in self.files.items() if p in active}
            return dict(self.titles), edges, missing, self.available


metadata = DiscussionMetadata()


def _roots(sessions, edges):
    parents, issues = {}, {}
    for sid, s in sessions.items():
        candidates = edges.get(sid, set())
        if len(candidates) > 1:
            issues[sid] = "conflicting_parents"
        elif candidates:
            parent = next(iter(candidates))
            if parent not in sessions:
                issues[sid] = "parent_not_imported"
            elif sessions[parent]["tool"] != s["tool"]:
                issues[sid] = "incompatible_parent"
            else:
                parents[sid] = parent
        elif s["session_kind"] in ("subagent", "guardian"):
            issues[sid] = "parent_unknown"
    roots = {}
    for sid in sessions:
        chain, node = [], sid
        while node in parents and node not in chain:
            chain.append(node)
            node = parents[node]
        if node in chain:
            # Quarantine all nodes leading into a cycle; none is counted twice.
            for member in chain:
                issues[member] = "parent_cycle"
                roots[member] = member
        else:
            roots[sid] = node
    return roots, issues


SUM_FIELDS = ("fresh_input", "cached_input", "output", "reasoning_in_output", "steps", "priced_steps", "unpriced_steps", "unpriced_tokens", "known_cost")


def _total(rows):
    result = {key: sum(row[key] or 0 for row in rows) for key in SUM_FIELDS}
    result["total_tokens"] = result["fresh_input"] + result["cached_input"] + result["output"]
    result["pricing_status"] = ("unavailable" if not result["priced_steps"] else "partial" if result["unpriced_steps"] else "complete")
    result["known_cost"] = result["known_cost"] if result["priced_steps"] else None
    result["priced_token_percent"] = (100 * (result["total_tokens"] - result["unpriced_tokens"]) / result["total_tokens"] if result["total_tokens"] else None)
    return result


def leaderboard(conn, *, meta=None, tool=None, model=None, project=None, start=None, end=None,
                agent=None, entrypoint=None, include_children=True, view="discussions", pricing=None,
                sort="tokens", direction="desc", search=None, limit=20, offset=0, discussion_id=None):
    meta = meta if meta is not None else metadata
    sessions = {r["id"]: dict(r) for r in conn.execute("SELECT id, tool, session_uuid, cwd, originator, source, session_kind, entrypoint, started_at, ended_at FROM sessions")}
    sources = [(r[0], r[1]) for r in conn.execute("SELECT DISTINCT session_id, source_file FROM messages WHERE tool='codex'")]
    titles, edges, missing, available = meta.snapshot(sources)
    for sid, s in sessions.items():
        try:
            edges.setdefault(sid, set()).update(meta.parent_ids({"source": json.loads(s["source"] or "null")}))
        except ValueError:
            pass
    roots, issues = _roots(sessions, edges)
    # Aggregate in SQL; never load messages or conversation text into the API.
    period_clauses, period_params = [], []
    for value, op in ((start, ">="), (end, "<=")):
        if value:
            period_clauses.append(f"julianday(ts) {op} julianday(?)")
            period_params.append(value)
    period_where = " WHERE " + " AND ".join(period_clauses) if period_clauses else ""
    rows = [dict(r) for r in conn.execute("""
        SELECT session_id, model, reasoning_effort, agent_id, agent_type,
               MIN(ts) AS started_at, MAX(ts) AS ended_at, COUNT(*) AS steps,
               SUM(input_tokens + cache_write_5m + cache_write_1h) AS fresh_input,
               SUM(cache_read) AS cached_input, SUM(output_tokens) AS output,
               SUM(reasoning_tokens) AS reasoning_in_output,
               SUM(est_cost_usd IS NOT NULL) AS priced_steps,
               SUM(est_cost_usd IS NULL) AS unpriced_steps,
               SUM(CASE WHEN est_cost_usd IS NULL THEN input_tokens + cache_write_5m + cache_write_1h + cache_read + output_tokens ELSE 0 END) AS unpriced_tokens,
               SUM(est_cost_usd) AS known_cost
        FROM messages """ + period_where + " GROUP BY session_id, model, reasoning_effort, agent_id, agent_type", period_params)]
    clauses, params = [], []
    for col, value, op in (("m.tool", tool, "="), ("m.model", model, "="), ("s.cwd", project, "="),
                           ("m.ts", start, ">="), ("m.ts", end, "<="), ("s.entrypoint", entrypoint, "=")):
        if value:
            if col == "m.ts":
                clauses.append(f"julianday({col}) {op} julianday(?)")
            else:
                clauses.append(f"{col} {op} ?")
            params.append(value)
    if agent == "main":
        clauses.append("m.agent_type IS NULL AND m.agent_id IS NULL")
    elif agent:
        clauses.append("m.agent_type = ?")
        params.append(agent)
    if not include_children:
        clauses.append("(m.tool != 'claude' OR (m.agent_id IS NULL AND m.agent_type IS NULL))")
    matching = {r[0] for r in conn.execute("SELECT DISTINCT m.session_id FROM messages m JOIN sessions s ON s.id=m.session_id" + (" WHERE " + " AND ".join(clauses) if clauses else ""), params)}
    by_root = defaultdict(list)
    embedded_children = defaultdict(set)
    members = defaultdict(set)
    for sid in sessions:
        members[roots[sid]].add(sid)
    for r in rows:
        sid = r["session_id"]
        if sid not in roots:
            continue
        if sessions[sid]["tool"] == "claude" and r["agent_id"]:
            embedded_children[roots[sid]].add((sid, r["agent_id"]))
        if not include_children and (sid != roots[sid] or (sessions[sid]["tool"] == "claude" and (r["agent_id"] or r["agent_type"]))):
            continue
        by_root[roots[sid]].append(r)
    result = []
    for root, group in members.items():
        s = sessions[root]
        orphan = root in issues or s["session_kind"] in ("subagent", "guardian")
        if discussion_id is None and ((view == "unattached") != orphan):
            continue
        if discussion_id is not None and root != discussion_id:
            continue
        selected_members = group if include_children else {root}
        if not selected_members & matching:
            continue
        usage = by_root[root]
        totals = _total(usage)
        if pricing and totals["pricing_status"] != pricing:
            continue
        title, title_source = titles.get(root, (f"{Path(s['cwd']).name if s['cwd'] else s['tool']} · {(s['started_at'] or 'undated')[:10]}", "fallback"))
        embedded = embedded_children[root]
        item = dict(id=root, title=title, title_source=title_source, project=s["cwd"], tool=s["tool"],
                    originator=s["originator"], session_kind=s["session_kind"],
                    models=sorted({r["model"] or "unknown" for r in usage}),
                    started_at=min([r["started_at"] for r in usage] + ([sessions[sid]["started_at"] for sid in selected_members if sessions[sid]["started_at"]] if not (start or end) else []), default=None),
                    ended_at=max([r["ended_at"] for r in usage] + ([sessions[sid]["ended_at"] for sid in selected_members if sessions[sid]["ended_at"]] if not (start or end) else []), default=None),
                    child_count=len(group)-1+len(embedded), include_children=include_children,
                    relationship_issue=issues.get(root), metadata_incomplete=bool(group & missing), **totals)
        if discussion_id:
            item["main"] = _total([r for r in usage if r["session_id"] == root and not (s["tool"] == "claude" and (r["agent_id"] or r["agent_type"]))])
            item["children"] = _total([r for r in usage if r["session_id"] != root or (s["tool"] == "claude" and (r["agent_id"] or r["agent_type"]))])
            item["by_model"] = [{"model": m, **_total([r for r in usage if (r["model"] or "unknown") == m])} for m in item["models"]]
            item["sessions"] = [{"id": sid, "title": titles.get(sid, (None,))[0], "kind": sessions[sid]["session_kind"],
                                  "originator": sessions[sid]["originator"], "project": sessions[sid]["cwd"],
                                  "reasoning_efforts": sorted({r["reasoning_effort"] for r in usage if r["session_id"] == sid and r["reasoning_effort"]}),
                                  **_total([r for r in usage if r["session_id"] == sid])} for sid in sorted(selected_members)]
        result.append(item)
    query = (search or "").strip()
    if len(query) >= 3:
        needle = query.casefold()
        candidates = [item["id"] for item in result if needle not in item["title"].casefold()]
        prompt_results = first_prompt_previews(conn, candidates)["previews"]
        result = [item for item in result if needle in item["title"].casefold()
                  or needle in (prompt_results.get(item["id"], {}).get("text") or "").casefold()]
    if discussion_id is None:
        for rank, item in enumerate(sorted(result, key=lambda item: (-item["total_tokens"], item["id"])), 1):
            item["rank"] = rank
    key = {"rank": "rank", "tokens": "total_tokens", "cost": "known_cost", "fresh_input": "fresh_input",
           "output": "output", "title": "title", "activity": "ended_at", "subagents": "child_count"}[sort]
    def sort_value(item):
        value = item[key]
        if sort == "title":
            return value.casefold() if value else None
        if sort == "activity" and value:
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
            except ValueError:
                return None
        return value
    # Stable ID tie-breaks; missing values remain last in either direction.
    result.sort(key=lambda item: item["id"])
    known = [item for item in result if sort_value(item) is not None]
    missing_values = [item for item in result if sort_value(item) is None]
    known.sort(key=sort_value, reverse=direction == "desc")
    result = known + missing_values
    last_ingest = conn.execute("SELECT MAX(finished_at) FROM ingest_runs WHERE error IS NULL AND finished_at IS NOT NULL").fetchone()[0]
    return {"discussions": result[offset:offset+limit], "total": len(result), "limit": limit, "offset": offset,
            "include_children": include_children, "totals_scope": "selected_period_imported_usage" if start or end else "lifetime_imported_usage",
            "last_successful_ingestion": last_ingest, "title_metadata_available": available,
            "unattached_sessions": sum(1 for sid in sessions if roots[sid] == sid and (sid in issues or sessions[sid]["session_kind"] in ("guardian", "subagent")))}
