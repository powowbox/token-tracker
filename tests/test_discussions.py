import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tracker.db import init, connect
from tracker.discussions import DiscussionMetadata, leaderboard, _roots


class DiscussionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "tokens.db"
        init(self.path)
        self.c = connect(self.path)
        self.addCleanup(self.c.close)
        self.meta = DiscussionMetadata(self.root / "codex")
        self.meta.home.mkdir()

    def session(self, sid, kind="user", parent=None, tool="codex", project="/project"):
        sid = tool + ":" + sid
        self.c.execute("INSERT INTO sessions(id,tool,session_uuid,cwd,session_kind,started_at) VALUES(?,?,?,?,?,?)", (sid, tool, sid.split(":",1)[1], project, kind, "2026-01-01T00:00:00Z"))
        if tool == "codex":
            payload = {"id": sid.split(":",1)[1]}
            if parent: payload["parent_thread_id"] = parent
            (self.root / (sid.replace(":", "-") + ".jsonl")).write_text(json.dumps({"type":"session_meta","payload":payload})+"\n")
        return sid

    def usage(self, sid, model="model-a", cost=1.0, fresh=100, cached=20, output=5, agent=None, ts="2026-01-01T00:00:00Z", cache_write=0):
        line = self.c.execute("SELECT COUNT(*)+1 FROM messages").fetchone()[0]
        path = self.root / (sid.replace(":", "-") + ".jsonl")
        self.c.execute("""INSERT INTO messages(session_id,tool,ts,model,input_tokens,cache_read,output_tokens,reasoning_tokens,est_cost_usd,source_file,source_line,agent_id,agent_type,cache_write_5m,reasoning_effort)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (sid,sid.split(":")[0],ts,model,fresh,cached,output,3,cost,str(path),line,agent,"Explore" if agent else None,cache_write,"high"))

    def report(self, **kwargs):
        return leaderboard(self.c, meta=self.meta, **kwargs)

    def test_full_tree_mixed_model_cost_and_reasoning(self):
        main=self.session("main"); child=self.session("review", "guardian", "main"); grand=self.session("grand", "subagent", "review")
        self.usage(main); self.usage(main, model="model-b", cost=2); self.usage(child,cost=None); self.usage(grand,cost=4)
        d=self.report(discussion_id=main)["discussions"][0]
        self.assertEqual(d["total_tokens"],500)
        self.assertEqual(d["reasoning_in_output"],12)
        self.assertEqual(d["known_cost"],7)
        self.assertEqual(d["pricing_status"],"partial")
        self.assertEqual(d["child_count"],2)
        self.assertEqual(d["main"]["total_tokens"],250)
        self.assertEqual(d["children"]["total_tokens"],250)
        self.assertEqual(d["models"],["model-a","model-b"])
        self.assertEqual(sum(r["total_tokens"] for r in d["by_model"]),500)
        self.assertEqual(self.report(include_children=False)["discussions"][0]["total_tokens"],250)

    def test_filters_select_without_truncating_lifetime(self):
        main=self.session("main"); self.usage(main); self.usage(main, model="model-b",ts="2026-09-01T12:00:00Z")
        self.assertEqual(self.report(start="2026-09-01",model="model-b")["discussions"][0]["total_tokens"],250)
        self.assertEqual(self.report(start="2026-09-02")["total"],0)
        self.assertEqual(self.report(start="2026-09-01",model="model-a")["total"],0)
        self.assertEqual(self.report(project="/missing")["total"],0)

    def test_children_can_select_parent(self):
        main=self.session("main"); child=self.session("child","subagent","main",project="/child")
        self.usage(main); self.usage(child,model="child-model")
        self.assertEqual(self.report(model="child-model")["discussions"][0]["total_tokens"],250)
        self.assertEqual(self.report(model="child-model",include_children=False)["total"],0)

    def test_claude_embedded_children_not_double_counted(self):
        main=self.session("main",tool="claude")
        self.usage(main,cache_write=10);self.usage(main,agent="a",cache_write=10);self.usage(main,agent="a",cache_write=10)
        d=self.report(discussion_id=main)["discussions"][0]
        self.assertEqual(d["total_tokens"],405)
        self.assertEqual(d["child_count"],1)
        self.assertEqual(d["main"]["total_tokens"],135)
        self.assertEqual(d["children"]["total_tokens"],270)
        self.assertEqual(self.report(include_children=False)["discussions"][0]["total_tokens"],135)

    def test_unknown_zero_and_partial_pricing_sort(self):
        for sid,cost in (("unknown",None),("zero",0),("known",2)):
            main=self.session(sid);self.usage(main,cost=cost)
        items=self.report(sort="cost")["discussions"]
        self.assertEqual([d["id"] for d in items],["codex:known","codex:zero","codex:unknown"])
        self.assertIsNone(items[-1]["known_cost"])
        self.assertEqual(items[1]["pricing_status"],"complete")
        self.assertEqual(self.report(pricing="unavailable")["total"],1)

    def test_missing_parent_and_unattached(self):
        child=self.session("child","guardian","missing");self.usage(child)
        self.assertEqual(self.report()["total"],0)
        d=self.report(view="unattached")["discussions"][0]
        self.assertEqual(d["relationship_issue"],"parent_not_imported")
        self.assertEqual(d["total_tokens"],125)

    def test_cycle_and_conflicting_parent_quarantine(self):
        a=self.session("a","subagent","b");b=self.session("b","subagent","a")
        self.usage(a);self.usage(b)
        items=self.report(view="unattached")["discussions"]
        self.assertEqual(len(items),2)
        self.assertEqual(sum(d["total_tokens"] for d in items),250)
        self.assertTrue(all(d["relationship_issue"]=="parent_cycle" for d in items))
        roots,issues=_roots({"x":{"tool":"codex","session_kind":"user"}}, {"x":{"a","b"}})
        self.assertEqual(issues["x"],"conflicting_parents")

    def test_duplicate_metadata_representation(self):
        main=self.session("main");child=self.session("child","subagent","main")
        self.usage(main);self.usage(child)
        source=json.dumps({"subagent":{"thread_spawn":{"parent_thread_id":"main"}}})
        self.c.execute("UPDATE sessions SET source=? WHERE id=?",(source,child))
        self.assertEqual(self.report()["discussions"][0]["total_tokens"],250)

    def test_title_priority_rename_and_never_prompt(self):
        main=self.session("main");self.usage(main)
        with closing(sqlite3.connect(self.meta.home/"state_5.sqlite")) as c, c:
            c.execute("CREATE TABLE threads(id TEXT,name TEXT,title TEXT,first_user_message TEXT)")
            c.execute("INSERT INTO threads VALUES('main','State name','PRIVATE PROMPT','PRIVATE PROMPT')")
        index=self.meta.home/"session_index.jsonl"
        index.write_text('invalid\n'+json.dumps({"id":"main","thread_name":"Index name","updated_at":"2026-01-01"})+"\n")
        self.assertEqual(self.report()["discussions"][0]["title"],"Index name")
        with index.open("a") as f:f.write(json.dumps({"id":"main","thread_name":"Renamed","updated_at":"2026-01-02"})+"\n")
        self.meta.refreshed=-float("inf")
        self.assertEqual(self.report()["discussions"][0]["title"],"Renamed")
        index.unlink();self.meta.refreshed=-float("inf")
        self.assertEqual(self.report()["discussions"][0]["title"],"State name")

    def test_metadata_unavailable_fallback_and_file_change(self):
        main=self.session("main");self.usage(main)
        d=self.report()["discussions"][0]
        self.assertEqual(d["title"],"project · 2026-01-01")
        self.assertFalse(self.report()["title_metadata_available"])
        (self.root/"codex-main.jsonl").unlink()
        self.assertTrue(self.report()["discussions"][0]["metadata_incomplete"])

    def test_pagination_repeat_and_no_writes(self):
        for i in range(5):
            s=self.session(str(i));self.usage(s,fresh=i)
        before=self.c.total_changes
        a=self.report(limit=2,offset=2);b=self.report(limit=2,offset=2)
        self.assertEqual(a,b);self.assertEqual(a["total"],5)
        self.assertEqual(len(a["discussions"]),2)
        self.assertEqual(before,self.c.total_changes)

    def test_empty_and_ingestion_freshness(self):
        self.c.execute("INSERT INTO ingest_runs(started_at,finished_at,error) VALUES('a','2026-01-01',NULL),('b','2026-01-02','failed')")
        d=self.report()
        self.assertEqual(d["total"],0)
        self.assertEqual(d["last_successful_ingestion"],"2026-01-01")

    def test_state_edge_and_conflicting_log_evidence(self):
        main=self.session("main");child=self.session("child","subagent");other=self.session("other")
        self.usage(main);self.usage(child);self.usage(other)
        with closing(sqlite3.connect(self.meta.home/"state_6.sqlite")) as c, c:
            c.execute("CREATE TABLE threads(id TEXT,name TEXT)")
            c.execute("CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT)")
            c.execute("INSERT INTO thread_spawn_edges VALUES('main','child')")
        self.assertEqual(self.report(discussion_id=main)["discussions"][0]["total_tokens"],250)
        (self.root/"codex-child.jsonl").write_text(json.dumps({"type":"session_meta","payload":{"id":"child","parent_thread_id":"other"}})+"\n")
        self.assertEqual(self.report(view="unattached")["discussions"][0]["relationship_issue"],"conflicting_parents")
        self.assertEqual(self.report(discussion_id=main)["discussions"][0]["total_tokens"],125)

    def test_descendant_of_cycle_is_not_attached(self):
        sessions={key:{"tool":"codex","session_kind":"subagent"} for key in ("a","b","c")}
        roots,issues=_roots(sessions,{"a":{"b"},"b":{"a"},"c":{"b"}})
        self.assertEqual(roots,{"a":"a","b":"b","c":"c"})
        self.assertEqual(set(issues.values()),{"parent_cycle"})

    def test_column_sorts_and_direction_apply_before_pagination(self):
        a=self.session("a");b=self.session("b");c=self.session("c")
        child=self.session("child","subagent","a")
        self.usage(a,cost=2,fresh=100,ts="2026-09-01T12:00:00+02:00")
        self.usage(b,cost=0,fresh=300,ts="2026-09-01T11:00:00Z")
        self.usage(c,cost=None,fresh=200,ts="2026-09-01T09:00:00Z")
        self.usage(child,cost=1,fresh=10,ts="2026-09-01T10:00:00Z")
        (self.meta.home/"session_index.jsonl").write_text("".join(json.dumps({"id":sid,"thread_name":name})+"\n" for sid,name in (("a","Zulu"),("b","alpha"),("c","Beta"))))
        def ids(**kw):return [r["id"] for r in self.report(**kw)["discussions"]]
        self.assertEqual(ids(sort="title",direction="asc"),[b,c,a])
        self.assertEqual(ids(sort="title",direction="desc"),[a,c,b])
        self.assertEqual(ids(sort="cost",direction="asc"),[b,a,c])
        self.assertEqual(ids(sort="cost",direction="desc"),[a,b,c])
        self.assertEqual(ids(sort="activity",direction="asc"),[c,a,b])
        self.assertEqual(ids(sort="activity",direction="desc"),[b,a,c])
        self.assertEqual(ids(sort="subagents",direction="desc"),[a,b,c])
        self.assertEqual(ids(sort="tokens",direction="asc",limit=1,offset=1),[c])

    def test_rank_is_stable_across_sorting_and_pagination(self):
        a=self.session("a");b=self.session("b");c=self.session("c")
        self.usage(a,fresh=300);self.usage(b,fresh=100);self.usage(c,fresh=200)
        expected={a:1,c:2,b:3}
        for key in ("rank","title","activity","tokens","cost","subagents"):
            for direction in ("asc","desc"):
                rows=self.report(sort=key,direction=direction)["discussions"]
                self.assertEqual({r["id"]:r["rank"] for r in rows},expected)
        self.assertEqual([r["rank"] for r in self.report(sort="rank",direction="desc")["discussions"]],[3,2,1])
        self.assertEqual(self.report(sort="rank",direction="asc",limit=1,offset=1)["discussions"][0]["rank"],2)
        self.c.execute("UPDATE sessions SET cwd='/other' WHERE id=?",(a,))
        self.assertEqual({r["id"]:r["rank"] for r in self.report(project="/project")["discussions"]},{c:1,b:2})

    def test_title_search_threshold_case_pagination_and_rank(self):
        for sid,name,fresh in (("a","Portal billing",100),("b","PORTAL settings",200),("c","Other work",300)):
            session=self.session(sid);self.usage(session,fresh=fresh)
            with (self.meta.home/"session_index.jsonl").open("a") as f:
                f.write(json.dumps({"id":sid,"thread_name":name})+"\n")
        self.assertEqual(self.report(search="Po")["total"],3)
        self.assertEqual(self.report(search="  port  ")["total"],2)
        result=self.report(search="PORT",sort="rank",direction="asc",limit=1,offset=1)
        self.assertEqual(result["total"],2)
        self.assertEqual(result["discussions"][0]["id"],"codex:a")
        self.assertEqual(result["discussions"][0]["rank"],2)
        self.assertEqual(self.report(search="no match")["total"],0)
        self.assertEqual(self.report(search="%_xx")["total"],0)

    def test_search_matches_first_prompt_from_three_characters(self):
        a=self.session("a");b=self.session("b");c=self.session("c")
        for sid in (a,b,c):self.usage(sid)
        with (self.root/"codex-b.jsonl").open("a") as f:
            f.write(json.dumps({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Repair BADGE handling."}]}})+"\n")
        self.assertEqual(self.report(search="ba")["total"],3)
        found=self.report(search="bad")["discussions"]
        self.assertEqual([r["id"] for r in found],[b])
        self.assertEqual(found[0]["rank"],1)
        self.assertEqual(self.report(search="badge")["total"],1)
        self.assertEqual(self.report(search="project")["total"],3)
        self.assertEqual(self.report(search="missing")["total"],0)
        self.assertEqual(self.report(search="badge",project="/different")["total"],0)

    def test_api_validation_and_detail(self):
        import asyncio
        from tracker import api
        main=self.session("main");self.usage(main);self.c.commit()
        async def get(url):
            path, _, query = url.partition("?")
            messages=[]
            async def receive(): return {"type":"http.request", "body":b"", "more_body":False}
            async def send(message): messages.append(message)
            await api.app({"type":"http", "asgi":{"version":"3.0"}, "http_version":"1.1", "method":"GET", "scheme":"http", "path":path, "raw_path":path.encode(), "query_string":query.encode(), "headers":[], "server":("test",80), "client":("test",1), "root_path":""}, receive, send)
            return messages[0]["status"], json.loads(b"".join(m.get("body",b"") for m in messages))
        with patch.object(api,"DEFAULT_DB_PATH",self.path),patch("tracker.discussions.metadata",self.meta):
            for query in ("limit=0","offset=-1","sort=invalid","direction=invalid","search="+"a"*201,"view=invalid","pricing=invalid","start=invalid","start=2026-09-02&end=2026-09-01"):
                self.assertEqual(asyncio.run(get("/api/discussions?"+query))[0],422,query)
            self.assertEqual(asyncio.run(get("/api/discussions"))[1]["total"],1)
            self.assertEqual(asyncio.run(get("/api/discussions/codex:main"))[1]["discussion"]["total_tokens"],125)
            self.assertEqual(asyncio.run(get("/api/discussions/missing"))[0],404)



if __name__ == "__main__":
    unittest.main()
