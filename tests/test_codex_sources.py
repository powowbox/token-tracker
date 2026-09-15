import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracker import parse_codex
from tracker.pricing import cost_usd
from tracker.usage_snapshots import SnapshotStore


class CodexSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config = self.base / 'sources.json'
        self.patcher = patch.object(parse_codex, 'CONFIG_PATH', self.config)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def log(self, home, identifier):
        p = self.base / home / 'sessions/2026/09/15' / f'rollout-{identifier}.jsonl'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': identifier}}) + '\n')
        return p

    def configure(self, homes):
        self.config.write_text(json.dumps({'codex_homes': homes}))

    def test_multiple_homes_missing_and_snapshot_identity(self):
        gpt = self.log('gpt', 'gpt-session')
        deepseek = self.log('deepseek', 'deepseek-session')
        self.configure(['gpt', 'deepseek', 'missing'])
        self.assertEqual(set(parse_codex.discover_files()), {gpt, deepseek})
        store = SnapshotStore(self.base / 'snapshots.sqlite3')
        self.assertEqual(store.sources('codex:deepseek-session'), [deepseek])
        self.assertEqual(store.sources('codex:gpt-session'), [gpt])

    def test_absent_config_keeps_legacy_source(self):
        log = self.log('legacy', 'old')
        with patch.object(parse_codex, 'CODEX_ROOT', self.base / 'legacy/sessions'):
            self.assertEqual(parse_codex.discover_files(), [log])

    def test_duplicate_and_symlink_do_not_change_original_path(self):
        log = self.log('gpt', 'old')
        (self.base / 'alias').symlink_to(self.base / 'gpt', target_is_directory=True)
        self.configure(['gpt', 'gpt', 'alias'])
        self.assertEqual(parse_codex.discover_files(), [log])

    def test_config_reload_and_empty_list(self):
        log = self.log('gpt', 'old')
        self.configure([])
        self.assertEqual(parse_codex.discover_files(), [])
        self.configure(['gpt'])
        self.assertEqual(parse_codex.discover_files(), [log])

    def test_tilde_and_absolute_paths(self):
        log = self.log('gpt', 'old')
        self.configure(['~/gpt', str(self.base / 'gpt')])
        with patch.dict('os.environ', {'HOME': str(self.base)}):
            self.assertEqual(parse_codex.discover_files(), [log])

    def test_invalid_config_fails_explicitly(self):
        for value in ('{', '[]', '{}', '{"codex_homes": "bad"}', '{"codex_homes": [null]}', '{"codex_homes": [""]}'):
            with self.subTest(value=value):
                self.config.write_text(value)
                with self.assertRaisesRegex(ValueError, 'sources.json'):
                    parse_codex.discover_files()

    def test_deepseek_peak_estimate_and_gpt_unchanged(self):
        self.assertEqual(cost_usd('codex', 'deepseek-flash', input_tokens=1000000,
                                 cache_read=1000000, output_tokens=1000000), 1.506)
        self.assertEqual(cost_usd('codex', 'gpt-5.6-sol', input_tokens=100,
                                 cache_read=100, output_tokens=10), .00064)
        self.assertIsNone(cost_usd('codex', 'deepseek-flash-unknown', input_tokens=100))
