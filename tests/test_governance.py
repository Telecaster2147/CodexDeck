from __future__ import annotations

import json
from pathlib import Path
import unittest

from codex.compatibility import COMPATIBILITY_HANDLERS, compatibility_stats


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


class CompatibilityBudgetTests(unittest.TestCase):
    def test_manifest_matches_production_registry_and_every_handler_has_fixture(self) -> None:
        manifest = json.loads((FIXTURES / "compatibility_manifest.json").read_text())
        by_id = {item["handler_id"]: item for item in manifest["handlers"]}

        self.assertEqual(set(by_id), {handler.handler_id for handler in COMPATIBILITY_HANDLERS})
        for handler in COMPATIBILITY_HANDLERS:
            with self.subTest(handler=handler.handler_id):
                self.assertTrue((FIXTURES / handler.fixture).is_file())
                self.assertEqual(by_id[handler.handler_id]["source"], handler.source)
                self.assertEqual(by_id[handler.handler_id]["fixture"], handler.fixture)
                self.assertEqual(
                    by_id[handler.handler_id]["last_observed_version"],
                    handler.last_observed_version,
                )
                self.assertEqual(by_id[handler.handler_id]["diagnostic_only"], handler.diagnostic_only)
                self.assertTrue(handler.deletion_condition)

    def test_diagnostic_only_handlers_do_not_claim_authoritative_state(self) -> None:
        for handler in COMPATIBILITY_HANDLERS:
            if not handler.diagnostic_only:
                continue
            with self.subTest(handler=handler.handler_id):
                self.assertNotIn(handler.semantics, {"lifecycle", "attention", "terminal ownership"})

    def test_compatibility_stats_are_bounded_maintenance_signals(self) -> None:
        stats = compatibility_stats()
        self.assertEqual(stats["handler_count"], len(COMPATIBILITY_HANDLERS))
        self.assertLessEqual(stats["diagnostic_only_count"], stats["handler_count"])
        self.assertLessEqual(stats["long_unobserved_count"], stats["handler_count"])


if __name__ == "__main__":
    unittest.main()
