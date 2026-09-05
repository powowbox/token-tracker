from contextlib import closing
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracker.db import init, connect
from tracker.ingest import _process_file
from tracker import parse_codex
from tracker.rebuild_codex import prepare


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'source.db'
        init(self.source)
        self.log=self.root/'rollout-test.jsonl'
        events=[{'type':'session_meta','payload':{'id':'id','source':'vscode','originator':'JetBrains.PhpStorm'}},
                {'type':'turn_context','payload':{'model':'unpriced-model'}},
                {'type':'event_msg','timestamp':'2026-09-04T12:00:00Z','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':100,'cached_input_tokens':0,'output_tokens':10},'last_token_usage':{'input_tokens':100,'cached_input_tokens':0,'output_tokens':10}}}}]
        self.log.write_text(''.join(json.dumps(e)+'\n' for e in events))
        with closing(connect(self.source)) as c, c:
            _process_file(c,self.log,parse_codex)

    def test_prepare_backup_never_replaces_source(self):
        before=self.source.read_bytes()
        output=self.root/'new.db'
        report=prepare(self.source,output)
        self.assertEqual(self.source.read_bytes(),before)
        self.assertTrue(Path(report['backup']).exists())
        self.assertEqual(report['before']['fresh'],report['after']['fresh'])
        with self.assertRaises(ValueError):
            prepare(self.source,self.source)
        with self.assertRaises(ValueError):
            prepare(self.source,output)

    def test_missing_log_fails_closed(self):
        self.log.unlink()
        out=self.root/'fail.db'
        with self.assertRaises(FileNotFoundError):
            prepare(self.source,out)
        self.assertFalse(out.exists())
        self.assertFalse(Path(str(out)+'.report.json').exists())

    @unittest.skipUnless(importlib.util.find_spec('fastapi'), 'API dependencies not installed')
    def test_unknown_prices_stay_unknown_in_api(self):
        from tracker import api
        args=dict(tool='codex',model=None,project=None,start=None,end=None,agent=None,entrypoint=None)
        with patch.object(api,'DEFAULT_DB_PATH',self.source):
            stats=api.stats(**args,granularity='day')
            self.assertIsNone(stats['totals']['cost_usd'])
            self.assertEqual(stats['totals']['pricing_coverage']['unpriced_messages'],1)
            self.assertIsNone(stats['by_model'][0]['cost_usd'])
            series=api.breakdown_series(**args,granularity='day',group='model',limit=5)
            self.assertIsNone(series['series'][0]['total_cost_usd'])
            self.assertEqual(api.breakdown_series(**args,granularity='day',group='server',limit=5)['series'],[])


if __name__=='__main__':
    unittest.main()
