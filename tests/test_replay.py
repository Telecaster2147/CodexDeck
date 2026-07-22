from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.replay import ProtocolReplayRunner, ReplayOperation  # noqa: E402


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


class ProtocolReplayTests(unittest.TestCase):
    def test_manifest_replays_supported_shapes_incrementally(self) -> None:
        manifest = json.loads((FIXTURES / "replay_manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], 1)
        runner = ProtocolReplayRunner()

        for fixture in manifest["fixtures"]:
            with self.subTest(fixture=fixture["name"]):
                summary = runner.replay_file(
                    FIXTURES / fixture["file"],
                    chunk_sizes=fixture["chunk_sizes"],
                )
                expected = fixture["expected"]
                self.assertEqual(summary.lifecycle, expected["lifecycle"])
                self.assertEqual(
                    len(summary.terminal_sessions), expected["terminal_count"]
                )
                self.assertEqual(
                    sum(count for _, count in summary.unknown_events),
                    expected["unknown_total"],
                )
                self.assertTrue(summary.shape_families)

    def test_replay_handles_invalid_utf8_partial_line_and_copy_truncate(self) -> None:
        first = (
            b'{"timestamp":1,"type":"event_msg","payload":{"type":"task_started",'
            b'"turn_id":"TURN_ID"}}\n'
        )
        replacement = (
            b'{"invalid":"\xff"}\n'
            b'{"timestamp":2,"type":"event_msg","payload":{"type":"task_complete",'
            b'"turn_id":"TURN_ID"}}\n'
        )
        summary = ProtocolReplayRunner().replay(
            (
                ReplayOperation("append", first[:-3]),
                ReplayOperation("append", first[-3:]),
                ReplayOperation("replace", replacement),
            )
        )

        self.assertEqual(summary.lifecycle, "COMPLETED")
        self.assertTrue(summary.copy_truncated)
        self.assertEqual(summary.ignored_records, 1)
        self.assertEqual(summary.normalized_kinds, ("TURN_STARTED", "TURN_COMPLETED"))

    def test_replay_summary_never_contains_terminal_transcript(self) -> None:
        summary = ProtocolReplayRunner().replay_file(
            FIXTURES / "replay_lifecycle_terminal.jsonl"
        )
        serialized = repr(summary)
        self.assertNotIn("ready", serialized)
        self.assertNotIn("done", serialized)
        self.assertGreater(summary.terminal_sessions[0].retained_bytes, 0)


if __name__ == "__main__":
    unittest.main()
