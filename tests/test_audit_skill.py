"""Synthetic regression checks for the shipped read-only audit helper."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tracker.db import SCHEMA

HELPER = Path(__file__).resolve().parents[1] / "skills/token-tracker-audit/scripts/summarize.py"
spec = importlib.util.spec_from_file_location("audit_summary", HELPER)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "usage.sqlite3"
        with sqlite3.connect(self.db) as c:
            c.executescript(SCHEMA)
            c.execute("INSERT INTO sessions(id,tool,session_uuid,cwd,session_kind) VALUES ('codex:a','codex','a','/project','user')")
            c.execute("INSERT INTO sessions(id,tool,session_uuid,cwd) VALUES ('codex:b','codex','b','/other')")

    def add(self, ts, model="model-a", cost=1.0, session="codex:a", line=1):
        with sqlite3.connect(self.db) as c:
            c.execute("""INSERT INTO messages(session_id,tool,ts,model,input_tokens,cache_read,
                cache_write_5m,cache_write_1h,output_tokens,reasoning_tokens,cache_write_input_tokens,
                est_cost_usd,source_file,source_line) VALUES (?,'codex',?,?,10,100,3,2,20,5,7,?,'missing.jsonl',?)""",
                (session,ts,model,cost,line))

    def run_summary(self):
        return audit.summarize(self.db, "/project", "2026-09-01T00:00:00Z", "2026-09-03T00:00:00Z")

    def test_period_accounting_mixed_prices_and_no_writes(self):
        self.add("2026-09-01T02:00:00+02:00")
        self.add("2026-09-02T00:00:00Z", model="unknown", cost=None, line=2)
        self.add("2026-09-03T00:00:00Z", line=3)  # exclusive end
        self.add("2026-08-31T23:00:00Z", line=4)  # preceding period
        self.add("2026-09-02T00:00:00Z", session="codex:b", line=5)
        with sqlite3.connect(self.db) as c:
            for call in ("one", "two"):
                c.execute("""INSERT INTO mcp_calls(session_id,tool,ts,server,tool_name,call_id,is_error,source_file,source_line)
                    VALUES ('codex:a','codex','2026-09-02T00:00:00Z','mcp','search',?,1,'missing.jsonl',1)""", (call,))
        before = self.db.read_bytes()
        result = self.run_summary()
        t = result["current"]["totals"]
        self.assertEqual((t["fresh_input"],t["cached_input"],t["output"],t["total_tokens"]),(30,200,40,270))
        self.assertEqual(t["reasoning_in_output"],10)
        self.assertEqual((t["known_api_estimate_usd"],t["unpriced_tokens"],t["pricing_coverage"]),(1,135,"partial"))
        self.assertEqual(len(result["current"]["top_models"]),2)
        self.assertEqual(result["current"]["mcp_totals"], {"calls":2,"errors":2})
        self.assertEqual(result["previous"]["totals"]["total_tokens"],135)
        self.assertTrue(result["current"]["caution_indicators"])
        self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(result,self.run_summary())

    def test_empty_unknown_zero_and_missing_archives(self):
        self.assertEqual(self.run_summary()["current"]["totals"]["pricing_coverage"],"no_usage")
        self.add("2026-09-01T00:00:00Z",cost=None)
        t=self.run_summary()["current"]["totals"]
        self.assertIsNone(t["known_api_estimate_usd"])
        self.assertEqual(t["pricing_coverage"],"unpriced")
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE messages SET est_cost_usd=0")
        self.assertEqual(self.run_summary()["current"]["totals"]["known_api_estimate_usd"],0)

    def test_missing_legacy_and_invalid_input(self):
        missing=Path(self.tmp.name)/"absent.db"
        with self.assertRaises(sqlite3.OperationalError):
            audit.summarize(missing,"/project","2026-09-01T00:00:00Z","2026-09-02T00:00:00Z")
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError):
            audit.summarize(self.db,"/project","2026-09-01","2026-09-02")
        with sqlite3.connect(self.db) as c:
            c.execute("DROP TABLE messages")
        with self.assertRaisesRegex(ValueError,"Unsupported schema"):
            self.run_summary()

    def test_parameterized_project(self):
        self.add("2026-09-01T00:00:00Z")
        r=audit.summarize(self.db,"' OR 1=1 --","2026-09-01T00:00:00Z","2026-09-03T00:00:00Z")
        self.assertEqual(r["current"]["totals"]["steps"],0)


if __name__ == "__main__":
    unittest.main()
