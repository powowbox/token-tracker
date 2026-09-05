"""Persistent session-scoped checkpoints; tokens.db and source logs are never written."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import parse_codex
from .pricing import cost_usd, reload as reload_prices
from .prompt_usage import uncertain, warnings_for


class SnapshotError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat()


class SnapshotStore:
    def __init__(self, path=None, discover=None):
        self.path = path or Path(__file__).resolve().parent.parent / 'usage-snapshots.sqlite3'
        self.discover = discover or parse_codex.discover_files

    def connection(self):
        c = sqlite3.connect(self.path)
        c.execute('CREATE TABLE IF NOT EXISTS snapshots (sid TEXT PRIMARY KEY, body TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS reports (sid TEXT PRIMARY KEY, body TEXT NOT NULL)')
        c.commit()
        return c

    def sources(self, session_id):
        paths = []
        for path in self.discover():
            try:
                with path.open('rb') as f:
                    for _, line in zip(range(100), f):
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get('type') == 'session_meta':
                            if 'codex:' + str(d.get('payload', {}).get('id')) == session_id:
                                paths.append(path)
                            break
            except OSError:
                # Do not silently omit a potentially relevant file.
                raise SnapshotError(503, 'A source log is unavailable; retry when logs are accessible')
        return paths

    @staticmethod
    def read(path, baseline=None):
        st = path.stat()
        with path.open('rb') as f:
            data = f.read(st.st_size)
        if baseline:
            offset = baseline['offset']
            if len(data) < offset or hashlib.sha256(data[:offset]).hexdigest() != baseline['sha256']:
                raise SnapshotError(409, 'Source history changed since snapshot; create a new snapshot')
        else:
            offset = 0
        parsed, consumed = parse_codex.parse_file(path, start_offset=offset, end_offset=st.st_size)
        after = path.stat()
        if (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise SnapshotError(503, 'Source log changed during refresh; retry with the same SID')
        checkpoint = dict(offset=consumed, sha256=hashlib.sha256(data[:consumed]).hexdigest(),
                          lines=data[:consumed].count(b'\n'))
        return parsed, checkpoint

    def create(self, session_id):
        session_id = session_id if session_id.startswith('codex:') else 'codex:' + session_id
        paths = self.sources(session_id)
        if not paths:
            raise SnapshotError(404, 'Session log not found yet; retry after the session log is created')
        # Multiple files with one session ID may contain inherited or copied counters.
        if len(paths) != 1:
            raise SnapshotError(409, 'Session has multiple source logs; cannot safely isolate its usage')
        baseline = {}
        for path in paths:
            parsed, baseline[str(path)] = self.read(path)
            if parsed.session.session_id != session_id:
                raise SnapshotError(503, 'Session identity changed during refresh; retry')
        body = dict(session_id=session_id, created_at=now(), sources=baseline)
        sid = str(uuid.uuid4())
        with closing(self.connection()) as c, c:
            c.execute('INSERT INTO snapshots VALUES (?,?)', (sid, json.dumps(body)))
        return dict(sid=sid, session_id=session_id, created_at=body['created_at'],
                    scope='session_only', measurement='newly_logged_usage_after_snapshot')

    def consumption(self, sid, *, days=30, min_samples=20, percentile=95, multiplier=2.0):
        with closing(self.connection()) as c:
            row = c.execute('SELECT body FROM snapshots WHERE sid=?', (sid,)).fetchone()
        if not row:
            raise SnapshotError(404, 'Unknown SID')
        body = json.loads(row[0])
        paths = self.sources(body['session_id'])
        if {str(p) for p in paths} != set(body['sources']):
            raise SnapshotError(409, 'Session source files changed since snapshot; create a new snapshot')
        rows, calls = [], []
        for path in paths:
            baseline = body['sources'][str(path)]
            parsed, _ = self.read(path, baseline)
            rows.extend(parsed.messages)
            # A result arriving late for a pre-snapshot invocation is not new work.
            calls.extend(c for c in parsed.mcp_calls if c.source_line > baseline['lines'])
        reload_prices()
        costs = [None if uncertain(r.usage_status) else cost_usd('codex', r.model,
                 input_tokens=r.input_tokens, cache_read=r.cache_read, output_tokens=r.output_tokens,
                 cache_write_input_tokens=r.cache_write_input_tokens) for r in rows]
        priced = sum(c is not None for c in costs)
        fresh = sum(r.input_tokens for r in rows)
        cached = sum(r.cache_read for r in rows)
        output = sum(r.output_tokens for r in rows)
        result = dict(sid=sid, session_id=body['session_id'], created_at=body['created_at'], refreshed_at=now(),
            scope='session_only; other sessions including subagents and guardians excluded',
            measurement='newly_logged_usage_after_snapshot; log flush delays may cross checkpoint boundaries',
            usage_steps=len(rows), uncertain_usage_steps=sum(uncertain(r.usage_status) for r in rows),
            tokens=dict(fresh_input=fresh,cached_input=cached,input=fresh+cached,output=output,
                        reasoning_in_output=sum(r.reasoning_tokens for r in rows),total=fresh+cached+output),
            pricing=dict(known_api_equivalent_usd=round(sum(c for c in costs if c is not None),8) if priced else (0 if not rows else None),
                         priced_steps=priced,unpriced_steps=len(rows)-priced,
                         coverage='no_usage' if not rows else 'priced' if priced==len(rows) else 'partial' if priced else 'unpriced'),
            mcp=dict(calls=len(calls),errors=sum(c.is_error for c in calls)),
            warning=None)
        comparison = dict(result, turn_id=sid, started_at=body['created_at'], ended_at=result['refreshed_at'],
            status='completed', boundary='explicit', project=parsed.session.cwd,
            agent_type=parsed.session.session_kind, models=sorted({r.model or 'unknown' for r in rows}),
            reasoning_efforts=sorted({r.reasoning_effort or 'unknown' for r in rows}))
        with closing(self.connection()) as c, c:
            peers = [json.loads(r[0]) for r in c.execute('SELECT body FROM reports WHERE sid != ?', (sid,))]
            result['warning'] = warnings_for(comparison, peers, days=days, min_samples=min_samples,
                                              percentile=percentile, multiplier=multiplier)
            result['warning']['baseline'] = 'previously_reported_snapshot_intervals'
            c.execute('INSERT INTO reports VALUES (?,?) ON CONFLICT(sid) DO UPDATE SET body=excluded.body',
                      (sid, json.dumps(comparison)))
        return result


store = SnapshotStore()
