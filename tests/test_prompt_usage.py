import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import asyncio
from urllib.parse import urlencode
from tracker import api
from tracker.prompt_usage import PromptIndex, read_turns, warnings_for
from tests.test_codex_accounting import event, token, usage


def fixture():
    return [
        event('session_meta', id='session', cwd='/project', source='vscode', originator='JetBrains.PhpStorm'),
        event('event_msg', type='task_started', turn_id='one'),
        event('turn_context', turn_id='one', model='gpt-5.6-sol', effort='medium'),
        event('event_msg', type='user_message', message='PRIVATE PROMPT'),
        token(usage(100,40), usage(100,40)),
        token(usage(100,40), usage(100,40)),
        event('response_item', type='function_call', name='mcp__test__read', call_id='c'),
        event('event_msg', type='task_complete', turn_id='one'),
        event('event_msg', type='task_started', turn_id='two'),
        event('turn_context', turn_id='two', model='unknown', effort='high'),
        event('response_item', type='function_call_output', call_id='c', output={'isError':True,'content':[{'type':'text','text':'SECRET OUTPUT'}]}),
        token(usage(150,60,20,4),usage(50,20)),
        event('compacted'),
        token(usage(200,80,30,6),usage(50,20)),
    ]


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'rollout-test.jsonl'

    def write(self, rows):
        self.path.write_text(''.join(json.dumps(r)+'\n' for r in rows))

    def test_turn_totals_and_late_mcp_result(self):
        self.write(fixture())
        reports, unmatched = read_turns(self.path)
        self.assertEqual(unmatched, 0)
        one, two = reports
        self.assertEqual(one['tokens'], dict(fresh_input=60,cached_input=40,input=100,output=10,reasoning_in_output=2,total=110))
        self.assertEqual(one['mcp'], dict(calls=1, errors=1))
        self.assertEqual(one['status'], 'completed')
        self.assertEqual(two['tokens']['total'],120)
        self.assertEqual(two['status'],'running')
        self.assertEqual(two['pricing']['coverage'],'unpriced')
        self.assertIsNone(two['pricing']['known_api_equivalent_usd'])
        self.assertNotIn('PRIVATE', json.dumps(reports))
        self.assertNotIn('SECRET', json.dumps(reports))

    def test_incremental_cache_equals_full_and_unchanged(self):
        index = PromptIndex(lambda:[self.path])
        rows=fixture()
        for n in range(1,len(rows)+1):
            self.write(rows[:n])
            reports,_=index.snapshot()
            self.assertEqual(reports,read_turns(self.path)[0])
        with patch('tracker.prompt_usage.read_turns',side_effect=AssertionError('unchanged reparse')):
            self.assertEqual(index.snapshot()[0],reports)
        with self.path.open('a') as f:
            f.write('{"type":')
        self.assertEqual(index.snapshot()[0],reports)

    def test_fallback_boundaries_and_orphan_usage(self):
        self.write([token(usage(10),usage(10)),event('event_msg',type='user_message'),
                    event('turn_context',turn_id='old',model='gpt-5.6-sol'),
                    token(usage(20,0,20,4),usage(10)),event('event_msg',type='task_complete',turn_id='old')])
        reports,unmatched=read_turns(self.path)
        self.assertEqual(unmatched,1)
        self.assertEqual(reports[0]['turn_id'],'old')
        self.assertEqual(reports[0]['boundary'],'approximate')
        self.assertEqual(warnings_for(reports[0],[])['status'],'uncertain_usage')

    def test_steering_and_mixed_model_stay_in_turn(self):
        rows=fixture()[:5]+[event('event_msg',type='user_message'),event('turn_context',turn_id='one',model='unknown'),
                            token(usage(200,80,20,4),usage(100,40))]
        self.write(rows)
        reports,_=read_turns(self.path)
        self.assertEqual(len(reports),1)
        self.assertEqual(reports[0]['models'],['gpt-5.6-sol','unknown'])
        self.assertEqual(reports[0]['pricing']['coverage'],'partial')

    def test_warnings_match_cohort_and_exclude_future(self):
        self.write(fixture())
        target=read_turns(self.path)[0][0]
        target['started_at']='2026-09-05T12:00:00Z'
        peers=[]
        for i in range(20):
            r=copy.deepcopy(target)
            r.update(turn_id=str(i),started_at='2026-09-04T12:00:00Z',ended_at='2026-09-04T12:01:00Z')
            peers.append(r)
        target['tokens']['fresh_input']=1000
        warning=warnings_for(target,peers)
        self.assertEqual(warning['status'],'high')
        self.assertEqual(warning['warnings'][0]['metric'],'fresh_input')
        self.assertEqual(warning['comparisons']['fresh_input']['threshold'],120)
        peers[0]['project']='/different'
        peers[1]['ended_at']='2026-09-06T12:00:00Z'
        self.assertEqual(warnings_for(target,peers)['status'],'insufficient_history')
        self.assertEqual(warnings_for(target,peers,min_samples=18)['status'],'high')
        target['uncertain_usage_steps']=1
        self.assertEqual(warnings_for(target,peers,min_samples=18)['status'],'uncertain_usage')

    def test_endpoint_selection_validation_and_no_db_writes(self):
        self.write(fixture())
        index=PromptIndex(lambda:[self.path])
        # No lifespan: endpoint has no dependency on live DB initialization or ingestion.
        class Response:
            def __init__(self, messages):
                self.status_code=next(m['status'] for m in messages if m['type']=='http.response.start')
                self.body=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
            def json(self):
                return json.loads(self.body)
        class Client:
            def get(self,path,params):
                async def request():
                    messages=[]
                    async def send(m): messages.append(m)
                    async def receive(): return {'type':'http.request','body':b'','more_body':False}
                    await api.app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1',
                        'method':'GET','scheme':'http','path':path,'raw_path':path.encode(),
                        'query_string':urlencode(params).encode(),'headers':[],
                        'server':('localhost',8732),'client':('localhost',1234),'root_path':''},receive,send)
                    return Response(messages)
                return asyncio.run(request())
        client=Client()
        with patch.object(api,'prompt_index',index), patch.object(api,'run_ingest',side_effect=AssertionError('write')):
            response=client.get('/api/prompt-usage',params={'session_id':'session','turn_id':'one'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['tokens']['total'],110)
            self.assertEqual(client.get('/api/prompts',params={'project':'/project'}).json()['matching_prompts'],2)
            self.assertEqual(client.get('/api/prompt-usage',params={'session_id':'missing'}).status_code,404)
            self.assertEqual(client.get('/api/prompt-usage',params={'session_id':'session','min_samples':0}).status_code,422)

    def test_copied_files_deduplicate_and_conflicts_are_excluded(self):
        self.write(fixture())
        other=self.path.with_name('copy.jsonl')
        other.write_bytes(self.path.read_bytes())
        idx=PromptIndex(lambda:[self.path,other])
        reports,info=idx.snapshot()
        self.assertEqual(len(reports),2)
        self.assertEqual(info['conflicting_turns'],0)
        with other.open('a') as f:
            f.write(json.dumps(event('event_msg',type='turn_aborted',turn_id='two'))+'\n')
        reports,info=idx.snapshot()
        self.assertEqual(len(reports),1)
        self.assertEqual(info['conflicting_turns'],1)

    def test_changing_file_is_not_served_from_stale_cache(self):
        self.write(fixture())
        idx=PromptIndex(lambda:[self.path])
        def changing(path):
            result=read_turns(path)
            with path.open('a') as f:f.write('\n')
            return result
        with patch('tracker.prompt_usage.read_turns',side_effect=changing):
            reports,info=idx.snapshot()
        self.assertEqual(reports,[])
        self.assertEqual(info['unavailable_files'],1)

    def test_reset_and_aborted_turn(self):
        self.write(fixture()+[token(usage(10,5),usage(10,5)),event('event_msg',type='turn_aborted',turn_id='two')])
        reports,_=read_turns(self.path)
        self.assertEqual(reports[1]['tokens']['total'],140)
        self.assertEqual(reports[1]['status'],'aborted')


if __name__=='__main__':
    unittest.main()
