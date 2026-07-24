from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.replay import ProtocolReplayRunner, ReplayOperation  # noqa: E402
from config import MAX_SESSION_TAIL  # noqa: E402


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


class ProtocolReplayTests(unittest.TestCase):
    @staticmethod
    def _cold_start_operations(*, resolve: bool = False) -> tuple[ReplayOperation, ...]:
        hidden_attention = (
            b'{"timestamp":1,"type":"event_msg","payload":'
            b'{"type":"exec_approval_request","turn_id":"TURN_ID"}}\n'
        )
        payload = hidden_attention + (b"x" * (MAX_SESSION_TAIL + 1024)) + b"\n"
        operations = [ReplayOperation("replace", payload)]
        operations.extend(ReplayOperation("append", b"") for _ in range(10))
        if resolve:
            operations.append(
                ReplayOperation(
                    "append",
                    b'{"timestamp":2,"type":"event_msg","payload":'
                    b'{"type":"exec_approval","call_id":"CALL_ID"}}\n',
                )
            )
        return tuple(operations)

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
                self.assertEqual(len(summary.terminal_sessions), expected["terminal_count"])
                self.assertEqual(
                    sum(count for _, count in summary.unknown_events),
                    expected["unknown_total"],
                )
                self.assertEqual(
                    summary.protocol_uncertain,
                    expected["protocol_uncertain"],
                )
                self.assertEqual(
                    summary.protocol_uncertainty_scope,
                    expected["protocol_uncertainty_scope"],
                )
                self.assertEqual(
                    summary.lifecycle_confidence,
                    expected["lifecycle_confidence"],
                )
                self.assertEqual(
                    summary.terminal_association.association_coverage,
                    expected["association_coverage"],
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
        summary = ProtocolReplayRunner().replay_file(FIXTURES / "replay_lifecycle_terminal.jsonl")
        serialized = repr(summary)
        self.assertNotIn("ready", serialized)
        self.assertNotIn("done", serialized)
        self.assertGreater(summary.terminal_sessions[0].retained_bytes, 0)

    def test_cold_start_gap_does_not_assert_hidden_attention_absent(self) -> None:
        summary = ProtocolReplayRunner().replay(self._cold_start_operations())
        completeness = {axis: complete for axis, complete, _ in summary.completeness}

        self.assertTrue(summary.context_truncated)
        self.assertEqual(summary.attention, "NONE")
        self.assertFalse(completeness["attention"])
        self.assertFalse(completeness["lifecycle"])
        self.assertTrue(completeness["network"])

    def test_replay_resolution_restores_only_supported_cold_start_axes(self) -> None:
        summary = ProtocolReplayRunner().replay(self._cold_start_operations(resolve=True))
        completeness = {axis: complete for axis, complete, _ in summary.completeness}

        self.assertEqual(summary.attention, "NONE")
        self.assertTrue(completeness["attention"])
        self.assertFalse(completeness["lifecycle"])
        self.assertFalse(completeness["failure_recovery"])

    def test_generation_change_without_baseline_resets_rollout_axes(self) -> None:
        first = (
            b'{"timestamp":1,"type":"event_msg","payload":'
            b'{"type":"task_started","turn_id":"TURN_ID"}}\n'
        )
        replacement = b'{"timestamp":2,"type":"event_msg","payload":{"type":"keepalive"}}\n'
        summary = ProtocolReplayRunner().replay(
            (
                ReplayOperation("append", first),
                ReplayOperation("replace", replacement),
            )
        )
        completeness = {axis: complete for axis, complete, _ in summary.completeness}

        self.assertFalse(completeness["lifecycle"])
        self.assertFalse(completeness["attention"])
        self.assertFalse(completeness["failure_recovery"])
        self.assertTrue(completeness["network"])

    def test_replay_retention_gap_keeps_hidden_attention_unknown(self) -> None:
        attention = (
            b'{"timestamp":1,"type":"event_msg","payload":'
            b'{"type":"exec_approval_request","turn_id":"TURN_ID"}}\n'
        )
        keepalives = b"".join(
            (
                '{"timestamp":%d,"type":"event_msg","payload":{"type":"keepalive"}}\n' % (index + 2)
            ).encode()
            for index in range(501)
        )
        summary = ProtocolReplayRunner().replay(
            (ReplayOperation("append", attention + keepalives),)
        )
        completeness = {axis: complete for axis, complete, _ in summary.completeness}

        self.assertEqual(summary.attention, "NONE")
        self.assertFalse(completeness["attention"])


if __name__ == "__main__":
    unittest.main()
