"""Optional local validation: reads source log prefixes, executes no log content."""
from contextlib import closing
import json
import sqlite3
import tempfile
from pathlib import Path

from tracker.db import init, connect
from tracker.ingest import _process_file
from tracker import parse_codex


def validate(rebuild_report):
    report=json.loads(Path(rebuild_report).read_text())
    manifest={r['source_file']:r for r in report['manifest']}
    selected=[]
    with closing(sqlite3.connect(report['backup'])) as src, src:
        # High-volume session, reset/resume session, and desktop/browser session.
        for prefix in ('01a05229','01a05eb7','01a06d2b'):
            row=src.execute('SELECT source_file FROM messages WHERE session_id LIKE ? LIMIT 1',('codex:'+prefix+'%',)).fetchone()
            if row:
                selected.append(row[0])
    results=[]
    for name in selected:
        with Path(name).open('rb') as f:
            lines=f.read(manifest[name]['offset']).splitlines(keepends=True)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);log=root/Path(name).name
            full=root/'full.db';inc=root/'incremental.db'
            init(full);init(inc)
            def fingerprint(c):
                return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY 1')]
                        for t in ('messages','sessions','mcp_calls')}
            log.write_bytes(b''.join(lines))
            with closing(connect(full)) as c, c:
                _process_file(c,log,parse_codex)
                expected=fingerprint(c)
            with closing(connect(inc)) as c, c:
                boundaries=sorted(set(list(range(1,len(lines),max(1,len(lines)//12)))+[len(lines)]))
                for n in boundaries:
                    log.write_bytes(b''.join(lines[:n]))
                    _process_file(c,log,parse_codex)
                assert fingerprint(c)==expected, 'Full/incremental mismatch'
                assert _process_file(c,log,parse_codex)==(False,0,0)
                results.append({'file':Path(name).name,'chunks':len(boundaries),
                                'steps':len(expected['messages']),'mcp':len(expected['mcp_calls']),
                                'full_equals_incremental':True,'unchanged_adds_zero':True})
    return results


if __name__=='__main__':
    import sys
    print(json.dumps(validate(sys.argv[1]),indent=2))
