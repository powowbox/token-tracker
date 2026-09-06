"""Read-only, bounded period aggregates; never reads conversation text."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Use timezone-aware start/end timestamps")
    return parsed.astimezone(timezone.utc)


def summarize(db, project, start, end, limit=10):
    start, end = instant(start), instant(end)
    if start >= end or not 1 <= limit <= 50:
        raise ValueError("Require start < end and limit between 1 and 50")
    with closing(sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        required = {
            "sessions": {"id", "cwd", "session_kind"},
            "messages": {"session_id", "ts", "model", "input_tokens", "cache_write_5m", "cache_write_1h", "cache_read", "output_tokens", "reasoning_tokens", "est_cost_usd", "usage_status", "cost_status"},
            "mcp_calls": {"session_id", "ts", "server", "tool_name", "is_error", "result_chars", "media_count", "completed"},
            "ingest_runs": {"finished_at", "error"},
        }
        for table, columns in required.items():
            present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not columns <= present:
                raise ValueError(f"Unsupported schema: {table} missing {sorted(columns - present)}")
        def period(lo, hi):
            params = (project, lo.isoformat(), hi.isoformat())
            where = "s.cwd=? AND julianday(m.ts)>=julianday(?) AND julianday(m.ts)<julianday(?)"
            fresh = "m.input_tokens+m.cache_write_5m+m.cache_write_1h"
            total = f"({fresh}+m.cache_read+m.output_tokens)"
            aggregates = f"""COUNT(*) steps, SUM({fresh}) fresh_input,
                SUM(m.cache_read) cached_input, SUM(m.output_tokens) output,
                SUM(m.reasoning_tokens) reasoning_in_output, SUM({total}) total_tokens,
                SUM(m.est_cost_usd) known_api_estimate_usd,
                SUM(m.est_cost_usd IS NULL) unpriced_steps,
                SUM(CASE WHEN m.est_cost_usd IS NULL THEN {total} ELSE 0 END) unpriced_tokens"""
            base = f"FROM messages m JOIN sessions s ON s.id=m.session_id WHERE {where}"
            def rows(sql, args=params):
                return [dict(r) for r in conn.execute(sql, args)]
            totals = rows(f"SELECT {aggregates}, COUNT(DISTINCT s.id) sessions, COUNT(DISTINCT date(m.ts)) active_days_utc {base}")[0]
            for key in totals:
                if key != "known_api_estimate_usd" and totals[key] is None:
                    totals[key] = 0
            totals["pricing_coverage"] = ("no_usage" if not totals["steps"] else "unpriced" if totals["unpriced_steps"] == totals["steps"] else "partial" if totals["unpriced_steps"] else "priced")
            sessions = rows(f"SELECT s.id session_id, s.session_kind, {aggregates} {base} GROUP BY s.id ORDER BY total_tokens DESC, s.id LIMIT ?", params + (limit,))
            models = rows(f"SELECT m.model, {aggregates} {base} GROUP BY m.model ORDER BY total_tokens DESC LIMIT ?", params + (limit,))
            kinds = rows(f"SELECT s.session_kind, {aggregates} {base} GROUP BY s.session_kind ORDER BY total_tokens DESC")
            statuses = rows(f"SELECT m.usage_status, COUNT(*) steps {base} GROUP BY m.usage_status")
            mcp_where = "s.cwd=? AND julianday(c.ts)>=julianday(?) AND julianday(c.ts)<julianday(?)"
            calls = rows(f"""SELECT c.server, c.tool_name, COUNT(*) calls,
                SUM(c.is_error) errors, SUM(c.completed=0) incomplete,
                SUM(c.result_chars) text_characters, SUM(c.media_count) media_count
                FROM mcp_calls c JOIN sessions s ON s.id=c.session_id WHERE {mcp_where}
                GROUP BY c.server,c.tool_name ORDER BY calls DESC,c.server,c.tool_name LIMIT ?""", params + (limit,))
            mcp_totals = rows(f"""SELECT COUNT(*) calls, COALESCE(SUM(c.is_error),0) errors
                FROM mcp_calls c JOIN sessions s ON s.id=c.session_id WHERE {mcp_where}""")[0]
            warnings = []
            if totals["sessions"] < 5:
                warnings.append("Fewer than five sessions: project-wide conclusions are exploratory")
            if totals["active_days_utc"] < 3:
                warnings.append("Fewer than three active UTC days")
            if sessions and sessions[0]["total_tokens"] > totals["total_tokens"] / 2:
                warnings.append("One session exceeds half of period tokens; inspect task complexity")
            invalid = conn.execute("""SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id
                WHERE s.cwd=? AND julianday(m.ts) IS NULL""", (project,)).fetchone()[0]
            if invalid:
                warnings.append(f"{invalid} project usage rows have invalid timestamps and cannot be assigned to a period")
            return dict(totals=totals, top_sessions=sessions, top_models=models, session_kinds=kinds,
                        usage_statuses=statuses, mcp_totals=mcp_totals, top_mcp_tools=calls,
                        selected_token_share=(sum(r["total_tokens"] for r in sessions)/totals["total_tokens"] if totals["total_tokens"] else None),
                        caution_indicators=warnings)
        latest = conn.execute("SELECT MAX(finished_at) FROM ingest_runs WHERE error IS NULL AND finished_at IS NOT NULL").fetchone()[0]
        return dict(scope="exact project cwd; period message usage; sessions not grouped discussions",
                    start=start.isoformat(), end_exclusive=end.isoformat(), last_successful_ingestion=latest,
                    current=period(start,end), previous=period(start-(end-start),start),
                    limits=["Archives and exact prompt attribution require targeted follow-up", "Session kind does not separate Claude child messages already grouped into a session", "Top model/tool lists are bounded; totals include all matching rows", "Snapshot intervals are not added", "Project aliases and child sessions with different cwd require confirmed scope expansion"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--project", required=True, help="Exact sessions.cwd; aliases must be confirmed separately")
    parser.add_argument("--start", required=True, help="ISO timestamp including timezone")
    parser.add_argument("--end", required=True, help="Exclusive ISO timestamp including timezone")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    try:
        print(json.dumps(summarize(args.db,args.project,args.start,args.end,args.limit), ensure_ascii=False, indent=2))
    except (ValueError, sqlite3.Error, OSError) as exc:
        parser.exit(2, f"Audit unavailable: {exc}\n")

if __name__ == "__main__":
    main()
