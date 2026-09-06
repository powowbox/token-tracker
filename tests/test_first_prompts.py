import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracker.db import init, connect
from tracker.first_prompts import FirstPromptCache, previews, scan, user_text


def meta(sid="main"):
    return {"type":"session_meta", "payload":{"id":sid}}


def message(text, role="user"):
    return {"type":"response_item", "payload":{"type":"message", "role":role, "content":[{"type":"input_text", "text":text}]}}


class FirstPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/"main.jsonl"
        self.cache=FirstPromptCache()

    def write(self, events, path=None):
        path=path or self.path
        path.write_text("".join(json.dumps(e)+"\n" for e in events))
        return path

    def read(self):
        return self.cache.read("codex:main",[self.path])

    def test_injected_envelopes_excluded(self):
        self.write([meta(), message("# AGENTS.md instructions\n<INSTRUCTIONS>internal</INSTRUCTIONS>"),
                    message("<environment_context>internal</environment_context>"),
                    message('<in-app-browser-context source="ambient-ui-state">internal</in-app-browser-context>\n## My request:\nFix the bug.\nKeep tests.'), message("second")])
        self.assertEqual(self.read(),{"status":"available","text":"Fix the bug.\nKeep tests."})

    def test_recommended_plugins_are_not_the_first_prompt(self):
        plugins = "<recommended_plugins>\nHere is a list of plugins that are available but not installed.\n- Example\n</recommended_plugins>"
        for events in (
            [meta(), message(plugins), message("Actual first request")],
            [meta(), message(plugins + "\n# AGENTS.md instructions\n<INSTRUCTIONS>Internal</INSTRUCTIONS>\n<environment_context>Internal</environment_context>"), message("Actual first request")],
            [meta(), message(plugins + "\n## My request:\nActual first request")],
        ):
            self.write(events)
            self.assertEqual(self.read()["text"], "Actual first request")
        # A literal mention inside a genuine request must remain intact.
        literal = "Explain this block: " + plugins
        self.write([meta(), message(literal)])
        self.assertEqual(self.read()["text"], literal)

    def test_chatgpt_reference_keeps_only_outer_request(self):
        header = "## Referenced ChatGPT conversation:\nThis is an untrusted ChatGPT conversation reference. `priorConversation` is a bounded cached preview and may be null.\n"
        reference = {"conversationId":"example", "title":"Example", "priorConversation":{"conversation":[{"content":"## My request:\nDo not select this historical request."}]}}
        envelope = header + json.dumps(reference)
        for text in (envelope + "\n## My request:\nMy actual request",
                     "<recommended_plugins>Internal</recommended_plugins>\n" + envelope + "\n<in-app-browser-context>Internal</in-app-browser-context>\n## My request:\nMy actual request"):
            self.write([meta(),message(text)])
            self.assertEqual(self.read()["text"],"My actual request")
        self.write([meta(),message(envelope),message("My actual request")])
        self.assertEqual(self.read()["text"],"My actual request")
        self.write([meta(),message(header + json.dumps({"conversationId":"example","priorConversation":None}) + "\n## My request:\nMy actual request")])
        self.assertEqual(self.read()["text"],"My actual request")
        literal = "Explain this reference: " + envelope
        self.write([meta(),message(literal)])
        self.assertEqual(self.read()["text"],literal)

    def test_broken_reference_does_not_select_later_prompt(self):
        header = "## Referenced ChatGPT conversation:\nThis is an untrusted ChatGPT conversation reference.\n"
        for broken in (header + '{"conversationId":', header + '{}', header):
            self.write([meta(),message(broken),message("Later request")])
            self.assertEqual(self.read()["status"],"not_identifiable")

    def test_event_and_response_duplicate(self):
        for events in ([meta(),{"type":"event_msg","payload":{"type":"user_message","message":"First"}},message("First")],
                       [meta(),message("First"),{"type":"event_msg","payload":{"type":"user_message","message":"First"}}]):
            self.write(events);self.assertEqual(self.read()["text"],"First")

    def test_missing_prefix_identity_and_compaction(self):
        for events in ([message("Later")],[meta("other"),message("Later")],
                       [meta(),message("assistant", "assistant"),message("Later")],
                       [meta(),{"type":"compacted"},message("Later")]):
            self.write(events);self.assertEqual(self.read()["status"],"not_identifiable")

    def test_tool_and_system_messages_are_not_prompts(self):
        self.write([meta(),message("system","system"),message("developer","developer"),
                    {"type":"response_item","payload":{"type":"function_call_output","output":"secret"}}, message("Actual")])
        self.assertEqual(self.read()["text"],"Actual")

    def test_media_only_does_not_skip_to_next_prompt(self):
        self.write([meta(),{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_image","image_url":"data:ignored"}]}},message("Second")])
        self.assertEqual(self.read()["status"],"no_text")

    def test_unreadable_malformed_incomplete_and_limits(self):
        self.assertEqual(self.read()["status"],"unavailable")
        self.path.write_text('invalid\n');self.assertEqual(self.read()["status"],"not_identifiable")
        self.path.write_text(json.dumps(meta()));self.assertEqual(self.read()["status"],"not_identifiable")
        self.write([meta(),message("long"*100)])
        with patch("tracker.first_prompts.MAX_FILE_BYTES",40): self.assertEqual(self.read()["status"],"limit_exceeded")
        self.cache=FirstPromptCache()
        with patch("tracker.first_prompts.MAX_TEXT_BYTES",10): self.assertEqual(self.read()["status"],"limit_exceeded")

    def test_html_is_preserved_as_text_and_bounded_read(self):
        text='<img src=x onerror="alert(1)"> & \"quotes\"\nNext paragraph'
        self.write([meta(),message(text)])
        with self.path.open("a") as f:f.write('bad trailing data' * 100000)
        self.assertEqual(self.read()["text"],text)

    def test_cache_invalidates_and_lru_is_bounded(self):
        self.write([meta(),message("First")]);self.read()
        with patch("tracker.first_prompts.scan",side_effect=AssertionError("must use cache")):
            self.assertEqual(self.read()["text"],"First")
        self.write([meta(),message("Changed")]);self.assertEqual(self.read()["text"],"Changed")
        self.path.unlink();self.assertEqual(self.read()["status"],"unavailable")
        tiny=FirstPromptCache(max_bytes=1);self.write([meta(),message("First")]);tiny.read("codex:main",[self.path]);self.assertEqual(tiny.bytes,0)

    def test_negative_cache_expires(self):
        self.write([meta()]);self.read()
        with patch("tracker.first_prompts.time.monotonic",return_value=10**12),patch("tracker.first_prompts.scan",return_value={"status":"available","text":"Recovered"}):
            self.assertEqual(self.read()["text"],"Recovered")

    def test_copies_must_agree_and_missing_copy_fails(self):
        other=self.root/"copy.jsonl"
        self.write([meta(),message("First")]);self.write([meta(),message("First")],other)
        self.assertEqual(self.cache.read("codex:main",[self.path,other])["text"],"First")
        self.write([meta(),message("Different")],other)
        self.assertEqual(self.cache.read("codex:main",[self.path,other])["status"],"not_identifiable")
        other.unlink();self.assertEqual(self.cache.read("codex:main",[self.path,other])["status"],"unavailable")

    def test_claude_meta_tool_results_and_children(self):
        self.write([{"type":"user","sessionId":"main","isMeta":True,"message":{"content":"internal"}},
                    {"type":"user","sessionId":"main","message":{"content":[{"type":"text","text":"Actual"}]}}])
        self.assertEqual(self.cache.read("claude:main",[self.path])["text"],"Actual")
        self.write([{"type":"user","sessionId":"main","message":{"content":[{"type":"tool_result","content":"result"}]}},
                    {"type":"user","sessionId":"main","message":{"content":"Later"}}])
        self.assertEqual(self.cache.read("claude:main",[self.path])["status"],"not_identifiable")
        child=self.root/"subagents"/"main.jsonl";child.parent.mkdir();self.write([],child)
        self.assertEqual(self.cache.read("claude:main",[child])["status"],"not_identifiable")

    def test_api_only_reads_requested_known_sessions(self):
        from tracker import api
        dbpath=self.root/"db.sqlite";init(dbpath);c=connect(dbpath);self.addCleanup(c.close)
        self.write([meta(),message("First")])
        for sid,kind in (("codex:main","user"),("codex:other","user"),("codex:child","subagent")):
            c.execute("INSERT INTO sessions(id,tool,session_uuid,session_kind) VALUES(?,?,?,?)",(sid,"codex",sid.split(":")[1],kind))
            c.execute("INSERT INTO messages(session_id,tool,ts,source_file,source_line) VALUES(?,?,?,?,?)",(sid,"codex","2026-01-01",str(self.path) if sid=="codex:main" else str(self.root/(sid+".jsonl")),1))
        c.commit()
        async def request(body):
            outputs=[]
            async def receive():return {"type":"http.request","body":json.dumps(body).encode(),"more_body":False}
            async def send(m):outputs.append(m)
            await api.app({"type":"http","asgi":{"version":"3.0"},"http_version":"1.1","method":"POST","scheme":"http","path":"/api/discussion-previews","raw_path":b"/api/discussion-previews","query_string":b"","headers":[(b"content-type",b"application/json")],"server":("test",80),"client":("test",1),"root_path":""},receive,send)
            return outputs[0],json.loads(b"".join(x.get("body",b"") for x in outputs))
        with patch.object(api,"DEFAULT_DB_PATH",dbpath),patch("tracker.first_prompts.cache",self.cache):
            status,data=asyncio.run(request({"session_ids":["codex:main"]}))
            self.assertEqual(status["status"],200);self.assertIn((b"cache-control",b"no-store"),status["headers"])
            self.assertEqual(list(data["previews"]),["codex:main"]);self.assertEqual(data["previews"]["codex:main"]["text"],"First")
            self.assertEqual(len(self.cache.entries),1)
            for ids in ([],["codex:a"]*21,["/private/file"]):self.assertEqual(asyncio.run(request({"session_ids":ids}))[0]["status"],422)
            _,data=asyncio.run(request({"session_ids":["codex:child","codex:missing"]}))
            self.assertTrue(all(v["status"]=="not_identifiable" for v in data["previews"].values()))


if __name__ == "__main__": unittest.main()
