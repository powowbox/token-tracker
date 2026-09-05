"""Backfill MCP statistics only; token usage and ingest offsets are untouched.

Run: python -m tracker.backfill_codex_mcp --dry-run
Then: python -m tracker.backfill_codex_mcp
A timestamped SQLite backup is created before applying changes.
"""
from contextlib import closing
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import DEFAULT_DB_PATH
from .parse_codex import discover_files, parse_file
from .ingest import _insert_mcp


def run(db_path=DEFAULT_DB_PATH, *, dry_run=False):
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    # Do not initialize/reprice/reingest messages as part of an MCP repair.
    conn = sqlite3.connect(path.as_uri() + "?mode=" + ("ro" if dry_run else "rw"), uri=True)
    conn.row_factory = sqlite3.Row
    summary = {"files": 0, "calls": 0, "errors": 0, "new_calls": 0}
    backup_path = None
    try:
        if not dry_run:
            backup_path = path.with_name(path.name + ".mcp-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
            with closing(sqlite3.connect(backup_path)) as backup, backup:
                conn.backup(backup)
            conn.execute("BEGIN IMMEDIATE")
        known_sessions = {r[0] for r in conn.execute("SELECT id FROM sessions WHERE tool='codex'")}
        for file in discover_files():
            parsed, _ = parse_file(file)
            # New sessions belong to normal ingestion, not this historical repair.
            if parsed.session.session_id not in known_sessions:
                continue
            summary["files"] += 1
            summary["calls"] += len(parsed.mcp_calls)
            summary["errors"] += sum(r.is_error for r in parsed.mcp_calls)
            if dry_run:
                summary["new_calls"] += sum(not conn.execute(
                    "SELECT 1 FROM mcp_calls WHERE source_file=? AND call_id=?",
                    (r.source_file, r.call_id)).fetchone() for r in parsed.mcp_calls)
            else:
                summary["new_calls"] += _insert_mcp(conn, parsed.mcp_calls)
        if not dry_run:
            conn.commit()
        summary["dry_run"] = dry_run
        summary["backup"] = str(backup_path) if backup_path else None
        return summary
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.db, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
