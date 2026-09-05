from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tracker.codex_usage import UsageCounter
from tracker.parse_codex import parse_file, result_stats
from tracker.pricing import cost_usd, _lookup
from tracker.db import init, connect
from tracker.ingest import _process_file
from tracker import parse_codex


def usage(i, c=0, o=10, r=2, w=0):
    return dict(input_tokens=i, cached_input_tokens=c, output_tokens=o,
                reasoning_output_tokens=r, cache_write_input_tokens=w)


def event(t, **p):
    return {'type': t, 'timestamp': '2026-09-04T12:00:00Z', 'payload': p}


def token(total=None, last=None):
    return event('event_msg', type='token_count', info={'total_token_usage': total, 'last_token_usage': last})


class CounterTests(unittest.TestCase):
    def test_duplicates_and_correction(self):
        c = UsageCounter()
        first = usage(100, 40)
        self.assertEqual(c.consume({'total_token_usage': first, 'last_token_usage': first})[0], first)
        self.assertIsNone(c.consume({'total_token_usage': first, 'last_token_usage': first})[0])
        delta, status = c.consume({'total_token_usage': usage(250, 80, 30, 5), 'last_token_usage': first})
        self.assertEqual(delta, usage(150, 40, 20, 3))
        self.assertEqual(status, 'cumulative_corrected')

    def test_reset_and_compaction(self):
        c = UsageCounter()
        c.consume({'total_token_usage': usage(1000, 900, 100, 20)})
        c.compacted = True
        delta, status = c.consume({'total_token_usage': usage(1100, 950, 110, 22)})
        self.assertEqual(delta, usage(100, 50))
        self.assertIn('after_compaction', status)
        delta, status = c.consume({'total_token_usage': usage(40, 10), 'last_token_usage': usage(40, 10)})
        self.assertEqual(delta, usage(40, 10))
        self.assertEqual(status, 'counter_reset')

    def test_missing_last_and_gap_bridge(self):
        c = UsageCounter()
        self.assertIsNone(c.consume({})[0])
        c.consume({'total_token_usage': usage(100, 40)})
        self.assertEqual(c.consume({'last_token_usage': usage(20, 10)})[0], usage(20, 10))
        delta, _ = c.consume({'total_token_usage': usage(150, 60, 30, 6)})
        self.assertEqual(delta, usage(30, 10))

    def test_inherited_baseline_and_subsets(self):
        c = UsageCounter()
        delta, _ = c.consume({'total_token_usage': usage(10000, 9000, 500, 100), 'last_token_usage': usage(100, 50)})
        self.assertEqual(delta, usage(100, 50))
        self.assertIsNone(UsageCounter().consume({'last_token_usage': usage(10, 20)})[0])

    def test_partial_fields(self):
        c = UsageCounter()
        v, status = c.consume({'last_token_usage': {'input_tokens': 15}})
        self.assertEqual(v['input_tokens'], 15)
        self.assertIn('partial_fields', status)

    def test_initial_zero_does_not_replay_last(self):
        c = UsageCounter()
        self.assertIsNone(c.consume({'total_token_usage':usage(0,0,0,0), 'last_token_usage':usage(100)})[0])

    def test_optional_missing_field_bridge(self):
        c = UsageCounter()
        c.consume({'total_token_usage':usage(100,50)})
        partial=usage(120,60,20,4);del partial['cached_input_tokens']
        v,_=c.consume({'total_token_usage':partial,'last_token_usage':usage(20,10)})
        self.assertEqual(v,usage(20,10))
        v,_=c.consume({'total_token_usage':usage(140,70,30,6),'last_token_usage':usage(20,10)})
        self.assertEqual(v,usage(20,10))


class PricingTests(unittest.TestCase):
    def test_unknown_no_family_fallback(self):
        for name in (None, 'codex-auto-review', 'gpt-5.6-sol-fake', 'gpt-5.4-mini-fake', 'gpt-5.6-sol-2099-01-01'):
            self.assertIsNone(cost_usd('codex', name, input_tokens=100))
        self.assertEqual(_lookup('codex', 'gpt-5.6'), _lookup('codex', 'gpt-5.6-sol'))

    def test_exact_rates_and_no_reasoning_double_charge(self):
        self.assertEqual(cost_usd('codex', 'gpt-5.6-sol', input_tokens=100, cache_read=100, output_tokens=10), .00064)

    def test_cache_write_premium_and_long_context(self):
        self.assertEqual(cost_usd('codex', 'gpt-5.6-sol', input_tokens=100, cache_write_input_tokens=100), .0005)
        self.assertEqual(cost_usd('codex', 'gpt-5.6-sol', input_tokens=300000, output_tokens=100), 2.403)
        self.assertIsNone(cost_usd('codex', 'gpt-5.6-sol', input_tokens=10, cache_write_input_tokens=20))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'rollout-2026-09-04T10-00-00-00000000-0000-0000-0000-000000000000.jsonl'

    def fixtures(self):
        return [
            event('session_meta', id='test', cwd='/project', originator='JetBrains.PhpStorm', source='vscode'),
            event('turn_context', model='gpt-5.6-sol', effort='medium'),
            token(usage(100, 40), usage(100, 40)),
            token(usage(100, 40), usage(100, 40)),
            event('response_item', type='function_call', name='js', namespace='mcp__cua_repl', call_id='c'),
            event('turn_context', model='unpriced-model', effort='low'),
            event('response_item', type='function_call_output', call_id='c', output=[{'type':'text','text':'hello'}, {'type':'image','data':'A'*1000}]),
            event('event_msg', type='item_completed', item={'type':'McpToolCall','id':'c','server':'cua_repl','tool':'js','status':'completed','result':{'content':[{'type':'text','text':'hello'}]}}),
            token(usage(200, 80, 20, 4), usage(100, 40)),
            event('compacted'),
            token(usage(250, 100, 30, 6), usage(50, 20)),
            token(usage(20, 5), usage(20, 5)),
            event('session_meta', id='test', originator='JetBrains.PhpStorm', source='vscode'),
            token(usage(20, 5), usage(20, 5)),
            event('response_item', type='message', content=[{'type':'text','text':'tools.mcp__fake__read()'}]),
            event('response_item', type='custom_tool_call', name='mcp__fake__not_executed', call_id='unrun'),
        ]

    def write(self, rows):
        self.path.write_text(''.join(json.dumps(r)+'\n' for r in rows))

    def fingerprint(self, conn):
        return {t: [tuple(r) for r in conn.execute('SELECT * FROM '+t+' ORDER BY 1')]
                for t in ('messages','sessions','mcp_calls')}

    def test_full_and_every_line_incremental_equivalent(self):
        rows = self.fixtures()
        self.write(rows)
        full = self.root/'full.db'; inc = self.root/'inc.db'
        init(full); init(inc)
        with closing(connect(full)) as c, c:
            _process_file(c, self.path, parse_codex)
            expected = self.fingerprint(c)
        with closing(connect(inc)) as c, c:
            for n in range(1, len(rows)+1):
                self.write(rows[:n])
                _process_file(c, self.path, parse_codex)
            self.assertEqual(self.fingerprint(c), expected)
            self.assertEqual(_process_file(c, self.path, parse_codex), (False,0,0))
            self.assertEqual(self.fingerprint(c), expected)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 4)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mcp_calls').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT model FROM mcp_calls').fetchone()[0], 'gpt-5.6-sol')
            self.assertIsNone(c.execute('SELECT est_cost_usd FROM sessions').fetchone()[0])
            self.assertEqual(c.execute('SELECT entrypoint,session_kind FROM sessions').fetchone()[:], ('JetBrains.PhpStorm','user'))

    def test_guardian_and_other_subagent(self):
        for source, kind in [({'subagent':{'other':'guardian'}}, 'guardian'), ({'subagent':{'spawn':{'parent_thread_id':'p'}}}, 'subagent')]:
            self.write([event('session_meta', id='test', source=source), token(usage(100),usage(100))])
            parsed,_=parse_file(self.path)
            self.assertEqual(parsed.session.session_kind,kind)
            self.assertEqual(parsed.messages[0].agent_type,kind)

    def test_new_error_and_media_formats(self):
        self.write([event('event_msg',type='item_completed',item={'type':'McpToolCall','id':'err','server':'phpstorm','tool':'read','status':'failed','error':{'message':'bad'}})])
        row=parse_file(self.path)[0].mcp_calls[0]
        self.assertEqual((row.is_error,row.result_chars),(1,3))
        self.assertEqual(result_stats({'content':[{'type':'audio','data':'x'*1000},{'type':'text','text':'ok'}]}),(2,0,1))
        self.assertEqual(result_stats({'base64':'x'*1000,'text':'ok'}),(2,0,0))


if __name__ == '__main__':
    unittest.main()
