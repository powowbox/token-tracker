import json
import tempfile
import unittest
from pathlib import Path

from tracker.usage_snapshots import SnapshotStore, SnapshotError
from tests.test_codex_accounting import event, token, usage


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path=self.root/'a.jsonl'
        self.other=self.root/'b.jsonl'
        self.write(self.path,[event('session_meta',id='a'),event('turn_context',model='gpt-5.6-sol'),token(usage(100,40),usage(100,40))])
        self.write(self.other,[event('session_meta',id='b'),token(usage(500),usage(500))])
        self.store=SnapshotStore(self.root/'snapshots.db',lambda:[self.path,self.other])

    def write(self,path,rows,mode='w'):
        with path.open(mode) as f:
            f.write(''.join(json.dumps(r)+'\n' for r in rows))

    def test_delta_isolated_persistent_and_repeatable(self):
        sid=self.store.create('a')['sid']
        self.write(self.path,[token(usage(150,60,20,4),usage(50,20)),token(usage(150,60,20,4),usage(50,20))],'a')
        self.write(self.other,[token(usage(5000),usage(4500))],'a')
        restarted=SnapshotStore(self.root/'snapshots.db',lambda:[self.path,self.other])
        for _ in range(2):
            r=restarted.consumption(sid)
            self.assertEqual(r['tokens'],dict(fresh_input=30,cached_input=20,input=50,output=10,reasoning_in_output=2,total=60))
            self.assertEqual(r['usage_steps'],1)
        second=restarted.create('a')['sid']
        self.assertEqual(restarted.consumption(second)['tokens']['total'],0)
        self.write(self.path,[token(usage(10,5),usage(10,5))],'a')
        self.assertEqual(restarted.consumption(sid)['tokens']['total'],80)
        self.assertEqual(restarted.consumption(second)['tokens']['total'],20)

    def test_rewrite_missing_or_ambiguous_source_fails(self):
        sid=self.store.create('a')['sid']
        original=self.path.read_bytes()
        self.path.write_bytes(original.replace(b'100',b'101'))
        with self.assertRaises(SnapshotError) as e:self.store.consumption(sid)
        self.assertEqual(e.exception.status,409)
        self.path.write_bytes(original)
        self.other.write_bytes(original)
        with self.assertRaises(SnapshotError):self.store.consumption(sid)
        with self.assertRaises(SnapshotError):self.store.create('a')

    def test_mcp_invocation_scope_and_unknown_price(self):
        self.write(self.path,[event('response_item',type='function_call',name='mcp__s__t',call_id='old')],'a')
        sid=self.store.create('a')['sid']
        self.write(self.path,[event('response_item',type='function_call_output',call_id='old',output='old result'),
            event('response_item',type='function_call',name='mcp__s__t',call_id='new'),
            event('response_item',type='function_call_output',call_id='new',output={'isError':True}),
            event('turn_context',model='codex-auto-review'),token(usage(150,60,20,4),usage(50,20))],'a')
        r=self.store.consumption(sid)
        self.assertEqual(r['mcp'],dict(calls=1,errors=1))
        self.assertIsNone(r['pricing']['known_api_equivalent_usd'])

    def test_partial_line_and_unknown_sid(self):
        fragment=json.dumps(token(usage(150,60,20,4),usage(50,20)))
        with self.path.open('a') as f:f.write(fragment[:20])
        sid=self.store.create('a')['sid']
        self.assertEqual(self.store.consumption(sid)['tokens']['total'],0)
        with self.path.open('a') as f:f.write(fragment[20:]+'\n')
        self.assertEqual(self.store.consumption(sid)['tokens']['total'],60)
        with self.assertRaises(SnapshotError) as e:self.store.consumption('missing')
        self.assertEqual(e.exception.status,404)

    def test_warning_uses_reported_snapshot_intervals(self):
        for i in range(2):
            sid=self.store.create('a')['sid']
            self.write(self.path,[token(usage(150+50*i,60+20*i,20+10*i,4+2*i),usage(50,20))],'a')
            r=self.store.consumption(sid,min_samples=2)
            self.assertEqual(r['warning']['status'],'insufficient_history')
        sid=self.store.create('a')['sid']
        self.write(self.path,[token(usage(1200,80,40,8),usage(1000,0))],'a')
        r=self.store.consumption(sid,min_samples=2)
        self.assertEqual(r['warning']['status'],'high')
        self.assertEqual(r['warning']['sample_count'],2)
        self.assertEqual(self.store.consumption(sid,min_samples=2)['warning']['sample_count'],2)


if __name__=='__main__':unittest.main()
