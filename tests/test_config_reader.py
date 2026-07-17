from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.config_reader import CodexConfigReader  # noqa: E402


class CodexConfigReaderTests(unittest.TestCase):
    def test_reads_auto_compact_limit_and_refreshes_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            config = home / "config.toml"
            config.write_text(
                'model = "MODEL"\nmodel_auto_compact_token_limit = 220_000\n'
                'model_auto_compact_token_limit_scope = "model"\n'
                'compact_prompt = "PROMPT"\n',
                encoding="utf-8",
            )
            reader = CodexConfigReader()

            first = reader.read(home)
            self.assertEqual(first.auto_compact_token_limit, 220_000)
            self.assertEqual(first.auto_compact_token_limit_scope, "model")
            self.assertTrue(first.compact_prompt_overridden)
            self.assertEqual(first.source, "config.toml")

            config.write_text(
                'model = "MODEL"\nmodel_auto_compact_token_limit = 180_000\n',
                encoding="utf-8",
            )
            second = reader.read(home)
            self.assertEqual(second.auto_compact_token_limit, 180_000)

    def test_invalid_toml_is_reported_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "config.toml").write_text(
                'model_auto_compact_token_limit = "unterminated\n',
                encoding="utf-8",
            )

            result = CodexConfigReader().read(home)

            self.assertIsNone(result.auto_compact_token_limit)
            self.assertEqual(result.source, "config.toml")
            self.assertTrue(result.error)


if __name__ == "__main__":
    unittest.main()
