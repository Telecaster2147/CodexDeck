from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex.events import normalize_log
from codex.hook_events import HookEventReader, receive_hook_event
from codex.ingress import (
    MAX_INGRESS_BYTES_PER_TICK,
    MAX_INGRESS_RECORDS_PER_TICK,
    MAX_JSONL_RECORD_BYTES,
)
from codex.rollout import RolloutReader
from codex.state_store import LogRecord
from codex.tui_session_log import TuiSessionLogReader
from models import Confidence, NetworkEvidence, NormalizedEvent, ProcessIdentity, ProcessInfo
from state_machine import SessionStateMachine


FIXTURES = Path(__file__).parent / "fixtures"


def _process() -> ProcessInfo:
    return ProcessInfo(
        ProcessIdentity(10, 20),
        1,
        "codex",
        10,
        0.0,
        "S",
        "wait",
        "codex",
        "session",
        instance_id="INSTANCE_ID",
        session_id="SESSION_ID",
    )


class TuiSessionLogTests(unittest.TestCase):
    def test_session_log_generation_separates_offset_reuse_and_marks_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"

            def record(session_id: str) -> str:
                return (
                    json.dumps(
                        {
                            "timestamp": 10,
                            "direction": "from_tui",
                            "type": "compact",
                            "session_id": session_id,
                        }
                    )
                    + "\n"
                )

            path.write_text(record("SESSION_A"))
            reader = TuiSessionLogReader()
            first = reader.read(path)
            first_source = first.events[0][1].source_id

            path.write_text(record("SESSION_B"))
            rewritten = reader.read(path)

            self.assertEqual(len(rewritten.events), 1)
            self.assertNotEqual(rewritten.events[0][1].source_id, first_source)
            self.assertEqual(rewritten.generation, 1)
            self.assertTrue(rewritten.stream_uncertain)
            self.assertEqual(
                rewritten.stream_uncertainty_reason,
                "content_anchor_mismatch",
            )
            self.assertEqual(rewritten.events[0][1].metadata["stream_generation"], 1)

            before = path.stat()
            path.write_text(record("SESSION_B"))
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            same_content = reader.read(path)
            self.assertEqual(same_content.events, ())
            self.assertEqual(same_content.generation, 1)
            self.assertTrue(same_content.stream_uncertain)
            self.assertEqual(
                same_content.stream_uncertainty_reason,
                "same_size_mtime_change_anchor_unchanged",
            )

    def test_session_log_file_replacement_increments_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            payload = (
                json.dumps(
                    {
                        "timestamp": 10,
                        "direction": "from_tui",
                        "type": "compact",
                        "session_id": "SESSION_ID",
                    }
                )
                + "\n"
            )
            path.write_text(payload)
            reader = TuiSessionLogReader()
            first = reader.read(path)
            replacement = root / "replacement.jsonl"
            replacement.write_text(payload)
            replacement.replace(path)

            second = reader.read(path)

            self.assertEqual(len(second.events), 1)
            self.assertEqual(second.generation, first.generation + 1)
            self.assertNotEqual(second.inode, first.inode)
            self.assertNotEqual(second.events[0][1].source_id, first.events[0][1].source_id)

    def test_session_log_parse_wall_budget_preserves_remaining_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(
                "".join(
                    json.dumps(
                        {
                            "timestamp": index + 1,
                            "direction": "from_tui",
                            "type": "compact",
                            "session_id": "SESSION_ID",
                        }
                    )
                    + "\n"
                    for index in range(3)
                )
            )
            reader = TuiSessionLogReader()

            with patch(
                "codex.tui_session_log.time.monotonic",
                side_effect=[0.0, 0.0, 1.0, 1.0],
            ):
                limited = reader.read(path)

            self.assertEqual(limited.record_count, 1)
            self.assertGreater(limited.backlog_records_lower_bound, 0)
            self.assertEqual(len(reader.read(path).events), 2)

    def test_reader_catches_up_in_record_bounded_quanta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(
                "".join(
                    json.dumps(
                        {
                            "timestamp": index + 1,
                            "direction": "from_tui",
                            "type": "compact",
                            "session_id": "SESSION_ID",
                        }
                    )
                    + "\n"
                    for index in range(MAX_INGRESS_RECORDS_PER_TICK + 20)
                )
            )
            reader = TuiSessionLogReader()
            events = []
            results = []

            while True:
                result = reader.read(path)
                results.append(result)
                events.extend(result.events)
                if not result.backlog_bytes:
                    break

            self.assertEqual(results[0].record_count, MAX_INGRESS_RECORDS_PER_TICK)
            self.assertTrue(results[0].budget_exceeded)
            self.assertTrue(
                all(result.bytes_read <= MAX_INGRESS_BYTES_PER_TICK for result in results)
            )
            self.assertEqual(len(events), MAX_INGRESS_RECORDS_PER_TICK + 20)
            self.assertEqual(results[-1].backlog_bytes, 0)

    def test_oversize_session_log_record_publishes_gap_then_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_bytes(b"x" * (MAX_JSONL_RECORD_BYTES + 1))
            reader = TuiSessionLogReader()

            oversized = reader.read(path)
            self.assertEqual(oversized.oversize_record_count, 1)
            self.assertEqual(oversized.gap_count, 1)
            self.assertEqual(len(reader.cursors[str(path)].partial), 0)

            with path.open("ab") as handle:
                handle.write(
                    b"\n"
                    + json.dumps(
                        {
                            "timestamp": 10,
                            "direction": "from_tui",
                            "type": "compact",
                            "session_id": "SESSION_ID",
                        }
                    ).encode()
                    + b"\n"
                )
            recovered = reader.read(path)

            self.assertEqual(len(recovered.events), 1)
            self.assertEqual(recovered.backlog_bytes, 0)

    def test_only_outbound_typed_compact_survives_high_fidelity_log(self) -> None:
        reader = TuiSessionLogReader()
        result = reader.read(FIXTURES / "compact_session_log.jsonl")
        self.assertTrue(result.configured)
        self.assertTrue(result.readable)
        self.assertEqual(len(result.events), 1)
        session_id, event = result.events[0]
        self.assertEqual(session_id, "SESSION_ID")
        self.assertEqual(event.kind, "COMPACT_REQUESTED")
        serialized = json.dumps(event.metadata)
        self.assertNotIn("PROMPT_MUST_NOT_SURVIVE", serialized)
        self.assertNotIn("RESPONSE_MUST_NOT_SURVIVE", serialized)


class HookEventTests(unittest.TestCase):
    def test_hook_generation_separates_same_timestamp_offset_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.jsonl"

            def record(event: str) -> str:
                return (
                    json.dumps(
                        {
                            "timestamp": 10,
                            "event": event,
                            "session_id": "SESSION_ID",
                            "trigger": "manual",
                            "outcome": "success",
                        }
                    )
                    + "\n"
                )

            path.write_text(record("PreCompact"))
            path.chmod(0o600)
            reader = HookEventReader(path)
            first = reader.read()[0][1]

            path.write_text(record("PostCompact"))
            path.chmod(0o600)
            second = reader.read()[0][1]

            self.assertNotEqual(first.source_id, second.source_id)
            self.assertEqual(reader.cursor.generation, 1)
            self.assertTrue(reader.cursor.stream_uncertain)
            self.assertEqual(second.metadata["stream_generation"], 1)
            self.assertEqual(second.metadata["stream_offset"], 0)

    def test_hook_file_replacement_increments_generation_without_timestamp_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "hooks.jsonl"
            payload = (
                json.dumps(
                    {
                        "timestamp": 10,
                        "event": "PreCompact",
                        "session_id": "SESSION_ID",
                        "trigger": "manual",
                        "outcome": "success",
                    }
                )
                + "\n"
            )
            path.write_text(payload)
            path.chmod(0o600)
            reader = HookEventReader(path)
            first = reader.read()[0][1]
            first_inode = reader.cursor.inode
            replacement = root / "replacement.jsonl"
            replacement.write_text(payload)
            replacement.chmod(0o600)
            replacement.replace(path)

            second = reader.read()[0][1]

            self.assertEqual(reader.cursor.generation, 1)
            self.assertNotEqual(reader.cursor.inode, first_inode)
            self.assertNotEqual(first.source_id, second.source_id)

    def test_hook_parse_wall_budget_preserves_remaining_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.jsonl"
            path.write_text(
                "".join(
                    json.dumps(
                        {
                            "timestamp": index + 1,
                            "event": "PreCompact",
                            "session_id": "SESSION_ID",
                            "trigger": "manual",
                            "outcome": "success",
                        }
                    )
                    + "\n"
                    for index in range(3)
                )
            )
            path.chmod(0o600)
            reader = HookEventReader(path)

            with patch(
                "codex.hook_events.time.monotonic",
                side_effect=[0.0, 0.0, 1.0, 1.0],
            ):
                limited = reader.read()

            self.assertEqual(len(limited), 1)
            self.assertGreater(reader.backlog_records_lower_bound, 0)
            self.assertEqual(len(reader.read()), 2)

    def test_hook_reader_bounds_burst_and_oversize_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.jsonl"
            records = [
                {
                    "timestamp": index + 1,
                    "event": "PreCompact",
                    "session_id": "SESSION_ID",
                    "turn_id": f"turn-{index}",
                    "trigger": "manual",
                    "outcome": "success",
                }
                for index in range(MAX_INGRESS_RECORDS_PER_TICK + 10)
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            path.chmod(0o600)
            reader = HookEventReader(path)

            first = reader.read()

            self.assertEqual(len(first), MAX_INGRESS_RECORDS_PER_TICK)
            self.assertEqual(reader.record_count, MAX_INGRESS_RECORDS_PER_TICK)
            self.assertTrue(reader.budget_exceeded)
            self.assertGreater(reader.backlog_bytes, 0)
            self.assertLessEqual(reader.bytes_read, MAX_INGRESS_BYTES_PER_TICK)

            remaining = reader.read()
            self.assertEqual(len(remaining), 10)
            self.assertEqual(reader.backlog_bytes, 0)

            path.write_bytes(b"x" * (MAX_JSONL_RECORD_BYTES + 1))
            path.chmod(0o600)
            reader = HookEventReader(path)
            self.assertEqual(reader.read(), [])
            self.assertEqual(reader.cursor.oversize_records, 1)
            self.assertEqual(reader.cursor.gap_count, 1)
            self.assertTrue(reader.cursor.gap_hash)

    def test_receiver_tightens_existing_mode_and_rejects_symlink_or_fifo(self) -> None:
        payload = io.StringIO('{"hook_event_name":"PreCompact"}')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "hooks.jsonl"
            path.touch(mode=0o644)
            receive_hook_event(path, payload)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            target = root / "target.jsonl"
            target.touch()
            symlink = root / "symlink.jsonl"
            symlink.symlink_to(target)
            with self.assertRaises(RuntimeError):
                receive_hook_event(
                    symlink,
                    io.StringIO('{"hook_event_name":"PreCompact"}'),
                )

            fifo = root / "hooks.fifo"
            os.mkfifo(fifo)
            with self.assertRaises(RuntimeError):
                receive_hook_event(
                    fifo,
                    io.StringIO('{"hook_event_name":"PreCompact"}'),
                )

    def test_receiver_writes_only_minimal_whitelisted_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.jsonl"
            receive_hook_event(
                path,
                io.StringIO(
                    json.dumps(
                        {
                            "hook_event_name": "PreCompact",
                            "session_id": "SESSION_ID",
                            "turn_id": "TURN_ID",
                            "trigger": "manual",
                            "prompt": "PROMPT_MUST_NOT_SURVIVE",
                            "transcript_path": "PATH_MUST_NOT_SURVIVE",
                        }
                    )
                ),
            )
            raw = path.read_text()
            self.assertNotIn("PROMPT_MUST_NOT_SURVIVE", raw)
            self.assertNotIn("PATH_MUST_NOT_SURVIVE", raw)
            self.assertEqual(
                set(json.loads(raw)),
                {
                    "timestamp",
                    "event",
                    "session_id",
                    "turn_id",
                    "trigger",
                    "outcome",
                },
            )

    def test_pre_and_post_hooks_form_running_and_completed_edges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compact-hooks.jsonl"
            path.write_bytes((FIXTURES / "compact_hooks.jsonl").read_bytes())
            path.chmod(0o600)
            events = HookEventReader(path).read()
        self.assertEqual([event.kind for _, event in events], ["COMPACTING", "COMPACT_COMPLETED"])
        self.assertTrue(all(event.parse_validity.value == "high" for _, event in events))
        self.assertTrue(all(event.source_authenticity.value == "low" for _, event in events))
        self.assertTrue(all(event.identity_binding.value == "low" for _, event in events))
        self.assertTrue(all(event.semantic_confidence.value == "medium" for _, event in events))

    def test_hook_fixtures_cover_failed_and_aborted_terminals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compact-terminal-hooks.jsonl"
            path.write_bytes((FIXTURES / "compact_terminal_hooks.jsonl").read_bytes())
            path.chmod(0o600)
            events = HookEventReader(path).read()
        self.assertEqual(
            [event.kind for _, event in events],
            ["COMPACTING", "COMPACT_FAILED", "COMPACTING", "COMPACT_ABORTED"],
        )

    def test_reader_rejects_insecure_mode_and_symlink_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "hooks.jsonl"
            path.write_text('{"event":"PreCompact"}\n', encoding="utf-8")
            path.chmod(0o644)
            reader = HookEventReader(path)
            self.assertEqual(reader.read(), [])
            self.assertIn("0600", reader.error)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

            target = root / "target.jsonl"
            target.write_text('{"event":"PreCompact"}\n', encoding="utf-8")
            target.chmod(0o600)
            symlink = root / "symlink.jsonl"
            symlink.symlink_to(target)
            reader = HookEventReader(symlink)
            self.assertEqual(reader.read(), [])
            self.assertTrue(reader.error)


class CompactFixtureTests(unittest.TestCase):
    def test_manual_rollout_fixture_reconstructs_start_after_completion(self) -> None:
        events = RolloutReader().read(FIXTURES / "manual_compact_rollout.jsonl")
        kinds = [event.kind for event in events]
        self.assertNotIn("COMPACT_CANDIDATE", kinds)
        self.assertEqual(kinds[-2:], ["COMPACTING", "COMPACT_COMPLETED"])
        self.assertTrue(events[-2].metadata["reconstructed"])

    def test_auto_and_completion_only_rollout_fixtures(self) -> None:
        auto = RolloutReader().read(FIXTURES / "auto_compact_rollout.jsonl")
        self.assertEqual([event.kind for event in auto], ["COMPACTING", "COMPACT_COMPLETED"])
        completion = RolloutReader().read(FIXTURES / "completion_only_rollout.jsonl")
        self.assertEqual([event.kind for event in completion], ["COMPACT_COMPLETED"])

    def test_auto_completion_reconstructs_start_from_pre_compact_token_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": "continue"},
                },
                {
                    "timestamp": "2026-01-01T00:00:10Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "context_tokens": 229_274,
                            "model_context_window": 258_400,
                        },
                    },
                },
                {
                    "timestamp": "2026-01-01T00:00:54Z",
                    "type": "compacted",
                    "payload": {"window_number": 2},
                },
                {
                    "timestamp": "2026-01-01T00:00:54.010Z",
                    "type": "event_msg",
                    "payload": {"type": "context_compacted"},
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            events = RolloutReader().read(path)

        compact_events = [event for event in events if event.kind.startswith("COMPACT")]
        self.assertEqual(
            [event.kind for event in compact_events],
            ["COMPACTING", "COMPACT_COMPLETED"],
        )
        self.assertEqual(compact_events[0].timestamp, 1_767_225_610.0)
        self.assertEqual(compact_events[1].timestamp, 1_767_225_654.0)
        self.assertEqual(compact_events[0].metadata["trigger"], "auto")
        self.assertEqual(
            compact_events[0].metadata["reconstruction_basis"],
            "pre_compact_token_snapshot",
        )
        self.assertTrue(compact_events[0].derived)
        self.assertTrue(compact_events[0].metadata["reconstructed"])

    def test_compact_completion_companion_is_deduplicated_in_reverse_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            records = [
                {
                    "timestamp": "2026-01-01T00:00:54Z",
                    "type": "event_msg",
                    "payload": {"type": "context_compacted"},
                },
                {
                    "timestamp": "2026-01-01T00:00:54.010Z",
                    "type": "compacted",
                    "payload": {"window_number": 2},
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            events = RolloutReader().read(path)

        compact_events = [event for event in events if event.kind == "COMPACT_COMPLETED"]
        self.assertEqual(len(compact_events), 1)

    def test_remote_and_retry_structured_log_fixtures(self) -> None:
        records = []
        for line in (FIXTURES / "compact_structured_logs.jsonl").read_text().splitlines():
            value = json.loads(line)
            records.append(
                LogRecord(
                    value["id"],
                    value["timestamp"],
                    value["level"],
                    value["target"],
                    value["thread_id"],
                    value["process_uuid"],
                    value["body"],
                )
            )
        self.assertEqual(
            [event.kind for record in records for event in normalize_log(record)],
            ["COMPACTING", "RECONNECTING"],
        )

    def test_rollout_failure_and_abort_have_explicit_compact_edges(self) -> None:
        failed = RolloutReader().read(FIXTURES / "compact_failure_rollout.jsonl")
        aborted = RolloutReader().read(FIXTURES / "compact_abort_rollout.jsonl")
        self.assertEqual(
            [event.kind for event in failed],
            ["COMPACT_REQUESTED", "TURN_FAILED", "COMPACT_FAILED"],
        )
        self.assertEqual(
            [event.kind for event in aborted],
            ["COMPACT_REQUESTED", "TURN_ABORTED", "COMPACT_ABORTED"],
        )


class CompactStateMachineTests(unittest.TestCase):
    def test_unverified_hook_cannot_override_rollout_compact_state(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    10,
                    "COMPACTING",
                    "rollout started",
                    source="rollout",
                    source_id="rollout-start",
                ),
                NormalizedEvent(
                    20,
                    "COMPACT_FAILED",
                    "hook failed",
                    source="compact_hook",
                    source_id="hook-failed",
                    confidence=Confidence.MEDIUM,
                    source_authenticity=Confidence.LOW,
                    identity_binding=Confidence.MEDIUM,
                    semantic_confidence=Confidence.MEDIUM,
                    binding_evidence=("producer_not_authenticated",),
                ),
            ],
        )

        state = machine.derive("key", _process(), NetworkEvidence(), 21)

        self.assertEqual(state.lifecycle.value, "COMPACTING")
        self.assertIsNone(state.latest_failure)
        self.assertEqual(state.compactions[-1].status, "running")
        self.assertEqual(
            {item.source for item in state.compactions[-1].evidence},
            {"rollout", "compact_hook"},
        )
        self.assertTrue(
            any(finding.conclusion == "Hook 证据与 rollout 冲突" for finding in state.diagnosis)
        )

    def test_requested_running_progress_and_completed_merge_evidence(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    10,
                    "COMPACT_REQUESTED",
                    "requested",
                    source="tui_session_log",
                    source_id="request",
                    turn_id="TURN_ID",
                    metadata={"trigger": "manual"},
                ),
                NormalizedEvent(
                    11,
                    "COMPACTING",
                    "started",
                    source="compact_hook",
                    source_id="start",
                    turn_id="TURN_ID",
                    metadata={"trigger": "manual"},
                ),
            ],
        )
        machine.observe_compaction("key", timestamp=12, source="network", detail="TCP RX +8192 B")
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    20,
                    "COMPACT_COMPLETED",
                    "completed",
                    source="rollout",
                    source_id="complete",
                    turn_id="TURN_ID",
                    metadata={"trigger": "manual"},
                )
            ],
        )
        state = machine.derive("key", _process(), NetworkEvidence(), 21)
        compact = state.compactions[-1]
        self.assertEqual(compact.status, "completed")
        self.assertEqual(compact.requested_at, 10)
        self.assertEqual(compact.started_at, 11)
        self.assertEqual(compact.duration_seconds, 9)
        self.assertEqual(
            {item.source for item in compact.evidence},
            {"tui_session_log", "compact_hook", "network", "rollout"},
        )

    def test_candidate_is_dismissed_by_ordinary_progress(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(10, "COMPACT_CANDIDATE", "candidate", source_id="candidate"),
                NormalizedEvent(11, "MODEL_PROGRESS", "ordinary", source_id="progress"),
            ],
        )
        state = machine.derive("key", _process(), NetworkEvidence(), 12)
        self.assertEqual(state.compactions[-1].status, "dismissed")

    def test_completion_only_does_not_invent_start_timestamp(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    20,
                    "COMPACT_COMPLETED",
                    "completed",
                    source="rollout",
                    source_id="complete-only",
                )
            ],
        )
        state = machine.derive("key", _process(), NetworkEvidence(), 21)
        compact = state.compactions[-1]
        self.assertEqual(compact.status, "completed")
        self.assertIsNone(compact.started_at)
        self.assertIsNone(compact.duration_seconds)
        self.assertNotIn(
            "compact protocol drift",
            {finding.conclusion for finding in state.diagnosis},
        )

    def test_retry_is_attached_to_open_compact_operation(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(10, "COMPACTING", "started", source_id="start"),
                NormalizedEvent(11, "RECONNECTING", "retry", source="log", source_id="retry"),
            ],
        )
        compact = machine.derive("key", _process(), NetworkEvidence(), 12).compactions[-1]
        self.assertEqual(compact.retry_count, 1)
        self.assertIn("log", {item.source for item in compact.evidence})

    def test_context_boundary_is_expected_observation_not_compact_fact(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    10,
                    "TOKEN_USAGE",
                    "tokens",
                    metadata={
                        "context_tokens": 220000,
                        "context_window": 300000,
                        "auto_compact_token_limit": 200000,
                    },
                )
            ],
        )
        state = machine.derive("key", _process(), NetworkEvidence(), 11)
        self.assertTrue(state.observation.auto_compact_expected)
        self.assertEqual(state.compactions, [])
        self.assertIn(
            "AUTO_COMPACT_EXPECTED",
            {finding.conclusion for finding in state.diagnosis},
        )

    def test_post_compact_token_snapshot_records_context_after(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    10,
                    "COMPACTING",
                    "start",
                    source_id="start",
                    metadata={"context_tokens": 240000},
                ),
                NormalizedEvent(20, "COMPACT_COMPLETED", "done", source_id="done"),
                NormalizedEvent(
                    21,
                    "TOKEN_USAGE",
                    "tokens",
                    source_id="tokens",
                    metadata={"context_tokens": 60000},
                ),
            ],
        )
        compact = machine.derive("key", _process(), NetworkEvidence(), 22).compactions[-1]
        self.assertEqual(compact.context_tokens, 240000)
        self.assertEqual(compact.context_tokens_after, 60000)


if __name__ == "__main__":
    unittest.main()
