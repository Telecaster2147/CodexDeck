from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_net_health.codex.events import normalize_rollout_record  # noqa: E402
from codex_net_health.models import (  # noqa: E402
    Confidence,
    LifecycleState,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
    RecoveryState,
    SocketInfo,
)
from codex_net_health.network.classifier import assess_process_network  # noqa: E402
from codex_net_health.network.sockets import parse_ss_output  # noqa: E402
from codex_net_health.state_machine import SessionStateMachine  # noqa: E402


def process(session_id: str = "session-1") -> ProcessInfo:
    return ProcessInfo(
        ProcessIdentity(42, 100),
        1,
        "codex",
        10,
        0.0,
        "S",
        "futex",
        "codex",
        "session",
        instance_id="instance-1",
        session_id=session_id,
    )


def event(timestamp: float, kind: str, source_id: str, detail: str = "") -> NormalizedEvent:
    return NormalizedEvent(
        timestamp,
        kind,
        kind,
        detail,
        "test",
        Confidence.HIGH,
        "turn-1",
        source_id,
    )


class EventNormalizationTests(unittest.TestCase):
    def test_stream_error_is_reconnecting_not_failed(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": "2026-07-15T00:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "stream_error",
                    "message": "idle timeout",
                    "additional_details": "retry 2/5",
                    "codex_error_info": "response_stream_disconnected",
                    "turn_id": "turn-1",
                },
            },
            "line:1",
        )
        self.assertEqual(events[0].kind, "RECONNECTING")
        self.assertEqual(events[0].failure.message, "idle timeout")

    def test_turn_complete_error_is_terminal_failure_with_message(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": "2026-07-15T00:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "error": {
                        "message": "retry budget exhausted",
                        "codex_error_info": {
                            "response_too_many_failed_attempts": {
                                "http_status_code": 502
                            }
                        },
                    },
                },
            },
            "line:2",
        )
        self.assertEqual(events[0].kind, "TURN_FAILED")
        self.assertEqual(events[0].failure.category, "response_too_many_failed_attempts")
        self.assertIn("retry budget", events[0].failure.message)

    def test_terminal_failure_is_complete_and_redacts_credentials(self) -> None:
        long_detail = "diagnostic-" + "x" * 700
        events = normalize_rollout_record(
            {
                "timestamp": "2026-07-15T00:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-secret",
                    "error": {
                        "message": "request failed token=secret-value",
                        "additional_details": long_detail,
                    },
                },
            },
            "line:secret",
        )
        failure = events[0].failure
        self.assertNotIn("secret-value", failure.message)
        self.assertIn("[REDACTED]", failure.message)
        self.assertEqual(failure.additional_details, long_detail)

    def test_non_turn_error_is_operation_error(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": "2026-07-15T00:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "error",
                    "message": "not steerable",
                    "codex_error_info": {"active_turn_not_steerable": {"turn_kind": "compact"}},
                },
            },
            "line:3",
        )
        self.assertEqual(events[0].kind, "OPERATION_ERROR")

    def test_sse_terminal_failure_has_error_message(self) -> None:
        from codex_net_health.codex.events import normalize_log
        from codex_net_health.codex.state_store import LogRecord

        body = (
            'SSE event: {"type":"response.failed","response":'
            '{"error":{"code":"server_error","message":"upstream failed"}}}'
        )
        events = normalize_log(
            LogRecord(1, 10.0, "ERROR", "codex_api::sse::responses", "s", "pid:1:x", body)
        )
        self.assertEqual(events[0].kind, "TURN_FAILED")
        self.assertEqual(events[0].failure.message, "upstream failed")


class StateMachineTests(unittest.TestCase):
    def test_reconnect_followed_by_progress_records_recovery(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        machine.ingest(
            "key",
            [
                event(now - 3, "TURN_STARTED", "1"),
                event(now - 2, "RECONNECTING", "2"),
            ],
        )
        machine.ingest("key", [event(now - 1, "MODEL_PROGRESS", "3")])
        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now)
        self.assertEqual(state.recovery, RecoveryState.RECOVERED)
        self.assertTrue(any(item.kind == "RECOVERED" for item in state.events))
        self.assertNotEqual(state.lifecycle, LifecycleState.FAILED)

    def test_terminal_failure_is_deduplicated_by_turn(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        first = normalize_rollout_record(
            {
                "timestamp": now,
                "type": "event_msg",
                "payload": {"type": "error", "turn_id": "t", "message": "short"},
            },
            "1",
        )[0]
        second = normalize_rollout_record(
            {
                "timestamp": now + 1,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "t",
                    "error": {
                        "message": "long message",
                        "additional_details": "full details",
                    },
                },
            },
            "2",
        )[0]
        machine.ingest("key", [first, second])
        failures = [item for item in machine.events["key"] if item.kind == "TURN_FAILED"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].failure.additional_details, "full details")

    def test_terminal_failure_does_not_remain_reconnecting(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        failed = normalize_rollout_record(
            {
                "timestamp": now,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "error": {"message": "failed"},
                },
            },
            "3",
        )[0]
        machine.ingest(
            "key",
            [event(now - 2, "TURN_STARTED", "1"), event(now - 1, "RECONNECTING", "2"), failed],
        )
        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now + 1)
        self.assertEqual(state.lifecycle, LifecycleState.FAILED)
        self.assertEqual(state.recovery, RecoveryState.NONE)

    def test_later_turn_clears_current_failure_but_keeps_latest_failure(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        failed = normalize_rollout_record(
            {
                "timestamp": now - 3,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "error": {"message": "first turn failed"},
                },
            },
            "failure",
        )[0]
        machine.ingest(
            "key",
            [
                event(now - 4, "TURN_STARTED", "start-1"),
                failed,
                NormalizedEvent(
                    now - 2,
                    "TURN_STARTED",
                    "TURN_STARTED",
                    source_id="start-2",
                    turn_id="turn-2",
                ),
                NormalizedEvent(
                    now - 1,
                    "TURN_COMPLETED",
                    "TURN_COMPLETED",
                    source_id="done-2",
                    turn_id="turn-2",
                ),
            ],
        )
        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now)
        self.assertEqual(state.lifecycle, LifecycleState.COMPLETED)
        self.assertIsNone(state.current_failure)
        self.assertEqual(state.latest_failure.message, "first turn failed")

    def test_event_retention_is_exactly_500(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [event(float(index), "MODEL_PROGRESS", str(index)) for index in range(550)],
        )
        self.assertEqual(len(machine.events["key"]), 500)
        self.assertEqual(machine.events["key"][0].source_id, "50")


class NetworkTests(unittest.TestCase):
    def test_active_connection_outweighs_isolated_suspect_connection(self) -> None:
        before = [
            SocketInfo("ESTAB", 0, 0, "a:1", "b:443", 42, bytes_received=10, route="external"),
            SocketInfo("ESTAB", 0, 10, "a:2", "c:443", 42, route="external"),
        ]
        after = [
            SocketInfo("ESTAB", 0, 0, "a:1", "b:443", 42, bytes_received=30, route="external"),
            SocketInfo("ESTAB", 0, 10, "a:2", "c:443", 42, route="external"),
        ]
        evidence = assess_process_network(before, after, 30)
        self.assertEqual(evidence.state, NetworkState.ACTIVE)

    def test_ss_multiline_metrics_are_merged(self) -> None:
        text = (
            'ESTAB 0 0 127.0.0.1:5000 203.0.113.1:443 users:(("codex",pid=42,fd=7))\n'
            ' cubic bytes_sent:12 bytes_acked:10\n'
            ' bytes_received:20 lastsnd:50 lastrcv:40 retrans:1/2 rtt:4.5\n'
        )
        socket = parse_ss_output(text, {42})[42][0]
        self.assertEqual(socket.bytes_sent, 12)
        self.assertEqual(socket.bytes_received, 20)
        self.assertEqual(socket.retrans_total, 2)
        self.assertEqual(socket.rtt_ms, 4.5)


if __name__ == "__main__":
    unittest.main()
