"""Prepare a new database alongside a read-only source; never replace live data.

The source SQLite backup is retained. Reparse only the byte prefixes already
ingested in that snapshot; later log appends are picked up by normal ingestion.
Activation is a separate, explicitly approved offline operation.
"""
from contextlib import closing
import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from .db import init, connect
from .parse_codex import parse_file, CODEX_ROOT
from .ingest import _upsert_session, _insert_messages, _insert_mcp, _recompute_session


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def summary(conn):
    result = rows(conn, """SELECT COUNT(*) steps, COUNT(DISTINCT session_id) sessions,
        SUM(input_tokens) fresh, SUM(cache_read) cached, SUM(output_tokens) output,
        SUM(reasoning_tokens) reasoning, SUM(est_cost_usd) priced_subtotal_usd,
        SUM(est_cost_usd IS NULL) unpriced_steps FROM messages WHERE tool='codex'""")[0]
    result['mcp'] = rows(conn, "SELECT server,COUNT(*) calls,SUM(is_error) errors FROM mcp_calls WHERE tool='codex' GROUP BY server")
    columns = {r[1] for r in conn.execute('PRAGMA table_info(sessions)')}
    result['classifications'] = rows(conn, "SELECT session_kind,COUNT(*) sessions FROM sessions WHERE tool='codex' GROUP BY session_kind") if 'session_kind' in columns else 'not recorded'
    result['models'] = rows(conn, "SELECT model,COUNT(*) steps,SUM(est_cost_usd IS NULL) unpriced_steps,SUM(est_cost_usd) priced_subtotal_usd FROM messages WHERE tool='codex' GROUP BY model")
    return result


def copy_rows(src, dst, table, where='', args=()):
    allowed = {r[1] for r in dst.execute('PRAGMA table_info('+table+')')}
    fields = [r[1] for r in src.execute('PRAGMA table_info('+table+')') if r[1] in allowed]
    columns = ','.join(fields)
    dst.executemany('INSERT INTO '+table+'('+columns+') VALUES('+','.join('?' for _ in fields)+')',
                    src.execute('SELECT '+columns+' FROM '+table+' '+where,args))


def prepare(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or output.exists():
        raise ValueError('Output must be a new, separate database')
    if not source.is_file():
        raise FileNotFoundError(source)
    backup = output.with_suffix(output.suffix+'.source-backup')
    if backup.exists():
        raise FileExistsError(backup)
    output.parent.mkdir(parents=True,exist_ok=True)
    # SQLite backup captures committed WAL data consistently; never copy only a
    # WAL-backed main database or change source journal mode.
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as live, live:
        with closing(sqlite3.connect(backup)) as saved, saved:
            live.backup(saved)
    src = sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)
    src.row_factory=sqlite3.Row
    conn = None
    try:
        if src.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Source integrity check failed')
        report={'before':summary(src), 'source':str(source), 'backup':str(backup), 'manifest':[]}
        states=rows(src,'SELECT * FROM ingest_state')
        codex_paths={r[0] for r in src.execute("SELECT DISTINCT source_file FROM messages WHERE tool='codex'")}
        codex_paths.update(r['source_file'] for r in states if Path(r['source_file']).name.startswith('rollout-'))
        state_by_path={r['source_file']:r for r in states}
        missing=codex_paths-set(state_by_path)
        if missing:
            raise ValueError('Missing ingestion offsets; cannot safely reconstruct '+str(len(missing))+' files')
        init(output)
        conn=connect(output)
        with conn:
            for table in ('sessions','messages','mcp_calls'):
                copy_rows(src,conn,table,"WHERE tool!='codex'")
            conn.execute("UPDATE messages SET cost_status=CASE WHEN est_cost_usd IS NULL THEN 'unpriced' ELSE 'legacy_api_estimate' END WHERE tool!='codex'")
            copy_rows(src,conn,'ingest_runs')
            copy_rows(src,conn,'ingest_state')
            diagnostics=Counter()
            seen_sessions=set()
            for name in sorted(codex_paths):
                path=Path(name);offset=state_by_path[name]['last_offset']
                with path.open('rb') as f:
                    prefix=f.read(offset)
                if len(prefix)!=offset:
                    raise ValueError('Source log shrank: '+str(path))
                digest=hashlib.sha256(prefix).hexdigest()
                parsed,new_offset=parse_file(path,end_offset=offset)
                with path.open('rb') as f:
                    if hashlib.sha256(f.read(offset)).hexdigest()!=digest:
                        raise ValueError('Source log changed while parsing')
                if new_offset!=offset:
                    raise ValueError('Stored offset is not a complete JSONL boundary')
                seen_sessions.add(parsed.session.session_id)
                _upsert_session(conn,parsed.session)
                _insert_messages(conn,parsed.messages)
                _insert_mcp(conn,parsed.mcp_calls)
                _recompute_session(conn,parsed.session.session_id)
                diagnostics.update(parsed.diagnostics)
                report['manifest'].append({'source_file':name,'offset':offset,'sha256':digest})
            expected={r[0] for r in src.execute("SELECT id FROM sessions WHERE tool='codex'")}
            if expected-seen_sessions:
                raise ValueError('Some sessions have no reconstructable source log')
            report['diagnostics']=dict(diagnostics)
        report['after']=summary(conn)
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('Rebuilt database integrity check failed')
        # Readiness report is written only after successful validation/checkpoint.
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        output.with_suffix(output.suffix+'.report.json').write_text(json.dumps(report,indent=2)+'\n')
        return report
    except BaseException:
        if conn:
            conn.close()
            conn=None
        for p in (output,Path(str(output)+'-wal'),Path(str(output)+'-shm')):
            if p.exists():
                p.unlink()
        raise
    finally:
        if conn:
            conn.close()
        src.close()


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source',required=True)
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    report=prepare(args.source,args.output)
    print(json.dumps({k:v for k,v in report.items() if k!='manifest'},indent=2))


if __name__=='__main__':
    main()
