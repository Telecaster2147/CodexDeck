from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from codex.hook_events import HookEventReader, receive_hook_event
from codex.rollout import RolloutReader
from codex.state_store import LogRecord
from codex.tui_session_log import TuiSessionLogReader
from codex.events import normalize_log
from models import NetworkEvidence, NormalizedEvent, ProcessIdentity, ProcessInfo
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
            self.assertEqual(set(json.loads(raw)), {
                "timestamp",
                "event",
                "session_id",
                "turn_id",
                "trigger",
                "outcome",
            })

    def test_pre_and_post_hooks_form_running_and_completed_edges(self) -> None:
        events = HookEventReader(FIXTURES / "compact_hooks.jsonl").read()
        self.assertEqual([event.kind for _, event in events], ["COMPACTING", "COMPACT_COMPLETED"])

    def test_hook_fixtures_cover_failed_and_aborted_terminals(self) -> None:
        events = HookEventReader(FIXTURES / "compact_terminal_hooks.jsonl").read()
        self.assertEqual(
            [event.kind for _, event in events],
            ["COMPACTING", "COMPACT_FAILED", "COMPACTING", "COMPACT_ABORTED"],
        )


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
        machine.observe_compaction(
            "key", timestamp=12, source="network", detail="TCP RX +8192 B"
        )
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
                NormalizedEvent(
                    11, "RECONNECTING", "retry", source="log", source_id="retry"
                ),
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
