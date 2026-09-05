from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracker.parse_codex import parse_file, _result_stats
from tracker.ingest import _insert_mcp
from tracker.db import init
from tracker.backfill_codex_mcp import run


def event(kind, **payload):
    return {"timestamp": "2026-09-04T10:00:00Z", "type": kind, "payload": payload}


def completion(call_id="c", result=None):
    return event("event_msg", type="mcp_tool_call_end", call_id=call_id,
                 invocation={"server": "phpstorm", "tool": "read_file"},
                 result=result if result is not None else {"Ok": {"content": [{"type": "text", "text": "hello"}], "isError": False}})


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rollout-2026-09-04T10-00-00-00000000-0000-0000-0000-000000000000.jsonl"

    def write(self, events):
        self.path.write_text("".join(json.dumps(e) + "\n" for e in events))

    def test_wrapped_completion_and_errors(self):
        self.write([completion(), completion("err", {"Err": "unavailable"}), completion("empty", {"Ok": {"isError": True, "content": []}})])
        parsed, _ = parse_file(self.path)
        self.assertEqual([(r.result_chars, r.is_error) for r in parsed.mcp_calls], [(5, 0), (11, 1), (0, 1)])

    def test_direct_and_completion_deduplicated(self):
        for kind in ("function_call", "custom_tool_call"):
            with self.subTest(kind=kind):
                self.write([event("response_item", type=kind, name="mcp__phpstorm__read_file", call_id="c"), completion(), event("response_item", type=kind + "_output", call_id="c", output="duplicate representation")])
                parsed, _ = parse_file(self.path)
                self.assertEqual(len(parsed.mcp_calls), 1)
                self.assertEqual(parsed.mcp_calls[0].result_chars, 5)

    def test_incremental_output_restores_call_and_no_old_usage(self):
        usage = event("event_msg", type="token_count", info={"last_token_usage": {"input_tokens": 10}})
        prefix = [event("turn_context", model="gpt-5.6-sol"), usage, event("response_item", type="function_call", name="mcp__phpstorm__read_file", call_id="c")]
        self.write(prefix)
        _, offset = parse_file(self.path)
        self.write(prefix + [event("response_item", type="function_call_output", call_id="c", output="hello"), usage])
        parsed, final = parse_file(self.path, start_offset=offset)
        self.assertEqual(len(parsed.messages), 1)
        self.assertEqual(parsed.messages[0].model, "gpt-5.6-sol")
        self.assertEqual(parsed.mcp_calls[0].result_chars, 5)
        self.assertEqual(final, self.path.stat().st_size)
        self.assertEqual(parse_file(self.path, start_offset=final)[0].mcp_calls, [])

    def test_partial_line(self):
        self.write([completion()])
        offset = self.path.stat().st_size
        with self.path.open("a") as f:
            f.write(json.dumps(completion("later")))
        parsed, end = parse_file(self.path)
        self.assertEqual(end, offset)
        self.assertEqual(len(parsed.mcp_calls), 1)
        with self.path.open("a") as f:
            f.write("\n")
        self.assertEqual(len(parse_file(self.path, start_offset=end)[0].mcp_calls), 1)

    def test_text_stats_exclude_media(self):
        self.assertEqual(_result_stats({"Ok": {"content": [{"type": "image", "data": "x" * 1000}, {"type": "text", "text": "abc"}]}}), (3, 0))
        self.assertEqual(_result_stats('{"isError":true,"content":[{"text":"bad"}]}'), (3, 1))

    def test_upsert_errors_even_with_existing_result(self):
        database = Path(self.tmp.name) / "tokens.db"
        init(database)
        self.write([completion()])
        rows = parse_file(self.path)[0].mcp_calls
        with closing(sqlite3.connect(database)) as conn, conn:
            self.assertEqual(_insert_mcp(conn, rows), 1)
            rows[0].is_error = 1
            rows[0].result_chars = 0
            self.assertEqual(_insert_mcp(conn, rows), 0)
            self.assertEqual(conn.execute("SELECT result_chars,is_error FROM mcp_calls").fetchone(), (0, 1))

    def test_backfill_dry_run_and_idempotence(self):
        database = Path(self.tmp.name) / "tokens.db"
        init(database)
        self.write([completion()])
        sid = parse_file(self.path)[0].session.session_id
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("INSERT INTO sessions(id,tool,session_uuid) VALUES(?, 'codex', 'test')", (sid,))
        with patch("tracker.backfill_codex_mcp.discover_files", return_value=[self.path]):
            self.assertEqual(run(database, dry_run=True)["new_calls"], 1)
            first = run(database)
            self.assertTrue(Path(first["backup"]).exists())
            self.assertEqual(first["new_calls"], 1)
            self.assertEqual(run(database)["new_calls"], 0)
        with closing(sqlite3.connect(database)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM mcp_calls").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
