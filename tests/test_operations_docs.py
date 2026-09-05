import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from tracker import api


class OperationsDocsTests(unittest.TestCase):
    def test_only_operations_section_without_database(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "README.md").write_text("# Intro\nprivate unrelated data\n## Server operations\nRestart guidance.\n### Console\nUse launcher.\n## Other\nother data\n")
            with patch.object(api, "ROOT", root), patch.object(api, "db", side_effect=AssertionError("DB access")):
                response = api.operations_documentation()
            self.assertEqual(response.media_type, "text/markdown")
            self.assertEqual(response.body.decode(), "## Server operations\nRestart guidance.\n### Console\nUse launcher.\n")

    def test_missing_documentation_is_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(api, "ROOT", Path(d)):
                with self.assertRaises(HTTPException) as e:
                    api.operations_documentation()
                self.assertEqual(e.exception.status_code, 503)
