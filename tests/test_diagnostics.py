from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from diagnostics import CollectorTracker  # noqa: E402


class CollectorTrackerTests(unittest.TestCase):
    def test_success_and_failure_expose_duration_staleness_and_budget(self) -> None:
        wall = [100.0]
        monotonic = [10.0]
        tracker = CollectorTracker(
            1.0,
            wall_clock=lambda: wall[0],
            monotonic=lambda: monotonic[0],
        )

        monotonic[0] = 10.5
        tracker.record("process", 10.0)
        health = tracker.snapshot()[0]
        self.assertEqual(health.last_success_at, 100.0)
        self.assertEqual(health.consecutive_failures, 0)
        self.assertFalse(health.budget_exceeded)

        wall[0] = 106.0
        monotonic[0] = 12.5
        tracker.record("process", 11.0, "timed out")
        health = tracker.snapshot()[0]
        self.assertEqual(health.consecutive_failures, 1)
        self.assertEqual(health.stale_age_seconds, 6.0)
        self.assertEqual(health.error, "timed out")
        self.assertTrue(health.budget_exceeded)


if __name__ == "__main__":
    unittest.main()
