from __future__ import annotations

import json
from pathlib import Path
import unittest

from tools.verify_pty import run_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PtyEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (PROJECT_ROOT / "tests/fixtures/pty_manifest.json").read_text(encoding="utf-8")
        )

    def test_manifest_covers_replayable_wide_and_narrow_records(self) -> None:
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(
            {case["fixture_id"] for case in self.manifest["cases"]},
            {"PTY-WIDE-001", "PTY-NARROW-001"},
        )
        required = set(self.manifest["record_contract"]["required"])
        self.assertIn("actual_state", required)
        self.assertIn("expected_and_actual_conflict", self.manifest["record_contract"]["invalid_when"])
        for case in self.manifest["cases"]:
            self.assertIn("expected_terminal_identity", case)
            self.assertIn("exit_retained", case["expected_state"])
            self.assertIn("focus_preserved", case["expected_state"])
            self.assertIn("scroll_preserved", case["expected_state"])

    def test_real_pty_cases_replay_to_valid_records(self) -> None:
        for case in self.manifest["cases"]:
            with self.subTest(fixture_id=case["fixture_id"]):
                result = run_fixture(case)
                self.assertTrue(result["valid"], result)
                self.assertEqual(result["visibility_result"], "PASS")
                self.assertEqual(result["domain_correctness_result"], "PASS")
                self.assertGreater(result["observed_at"], 0)
                self.assertGreater(result["actual_state"]["captured_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
