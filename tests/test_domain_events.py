from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.events import normalize_rollout_record  # noqa: E402
from models import (  # noqa: E402
    CapabilityMode,
    NetworkEvidence,
    ProcessIdentity,
    ProcessInfo,
)
from state_machine import SessionStateMachine  # noqa: E402


def process() -> ProcessInfo:
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
        session_id="thread-root",
        model="gpt-test",
        reasoning_effort="high",
    )


def normalized(timestamp: float, item_type: str, source_id: str, **payload):
    return normalize_rollout_record(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": item_type, **payload},
        },
        source_id,
    )


class ProtocolAggregationTests(unittest.TestCase):
    def test_direct_turn_tool_timing_and_ttft_are_aggregated(self) -> None:
        events = []
        events += normalized(
            100.0,
            "turn_started",
            "turn:start",
            turn_id="turn-1",
            trace_id="trace-1",
            collaboration_mode="default",
            model="gpt-direct",
        )
        events += normalized(
            101.0,
            "agent_message",
            "turn:first-token",
            turn_id="turn-1",
            message="hello",
        )
        events += normalized(
            102.0,
            "exec_command_begin",
            "tool:start",
            turn_id="turn-1",
            call_id="call-1",
            command="git status",
            started_at_ms=102000,
        )
        events += normalized(
            104.5,
            "exec_command_end",
            "tool:end",
            turn_id="turn-1",
            call_id="call-1",
            command="git status",
            duration_ms=2500,
            exit_code=0,
            status="completed",
        )
        events += normalized(
            105.0,
            "turn_complete",
            "turn:end",
            turn_id="turn-1",
            duration_ms=5000,
        )

        machine = SessionStateMachine(900)
        machine.ingest("key", events)
        state = machine.derive("key", process(), NetworkEvidence(), 106.0)

        self.assertEqual(len(state.turns), 1)
        turn = state.turns[0]
        self.assertEqual(turn.duration_seconds, 5.0)
        self.assertEqual(turn.time_to_first_token_seconds, 1.0)
        self.assertEqual(turn.model, "gpt-direct")
        self.assertEqual(turn.tool_count, 1)
        self.assertEqual(turn.tool_duration_seconds, 2.5)
        self.assertEqual(turn.longest_tool.call_id, "call-1")
        self.assertFalse(turn.tools[0].provenance.derived)
        self.assertTrue(turn.tools[0].provenance.complete)
        self.assertEqual(state.protocol_capabilities.turn_timing.mode, CapabilityMode.DIRECT)
        self.assertEqual(state.protocol_capabilities.tool_timing.mode, CapabilityMode.DIRECT)

    def test_legacy_tool_boundaries_derive_duration_and_unmatched_stays_running(self) -> None:
        records = [
            {
                "timestamp": 10.0,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "turn_id": "turn-1",
                    "call_id": "legacy-1",
                    "name": "lookup",
                },
            },
            {
                "timestamp": 12.0,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "turn_id": "turn-1",
                    "call_id": "legacy-1",
                },
            },
            {
                "timestamp": 13.0,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "turn_id": "turn-1",
                    "call_id": "legacy-running",
                    "name": "pending",
                },
            },
        ]
        events = [
            event
            for index, record in enumerate(records)
            for event in normalize_rollout_record(record, f"legacy:{index}")
        ]
        machine = SessionStateMachine(900)
        machine.ingest("key", events)
        state = machine.derive("key", process(), NetworkEvidence(), 14.0)

        completed = next(item for item in state.tool_executions if item.call_id == "legacy-1")
        running = next(
            item for item in state.tool_executions if item.call_id == "legacy-running"
        )
        self.assertEqual(completed.duration_seconds, 2.0)
        self.assertTrue(completed.provenance.derived)
        self.assertTrue(completed.provenance.complete)
        self.assertEqual(running.status, "running")
        self.assertIsNone(running.duration_seconds)
        self.assertFalse(running.provenance.complete)
        self.assertEqual(state.protocol_capabilities.tool_timing.mode, CapabilityMode.DERIVED)

    def test_token_context_and_rate_limit_snapshot_preserve_official_values(self) -> None:
        events = normalized(
            20.0,
            "token_count",
            "tokens",
            turn_id="turn-1",
            info={
                "total_token_usage": {
                    "input_tokens": 1200,
                    "cached_input_tokens": 200,
                    "output_tokens": 300,
                    "reasoning_output_tokens": 50,
                    "total_tokens": 1500,
                },
                "last_token_usage": {
                    "input_tokens": 400,
                    "cached_input_tokens": 100,
                    "output_tokens": 80,
                    "reasoning_output_tokens": 20,
                    "total_tokens": 480,
                },
                "context_tokens": 1200,
                "model_context_window": 4000,
                "rate_limits": {
                    "primary": {"used_percent": 42.5, "reset_at": 1000},
                    "secondary": {"used_percent": 11, "window_minutes": 10080},
                    "credits": {"balance": 7.5, "has_credits": True},
                    "reached": False,
                },
            },
        )
        machine = SessionStateMachine(900)
        machine.ingest("key", events)
        state = machine.derive("key", process(), NetworkEvidence(), 21.0)

        self.assertEqual(state.token_usage.input_tokens, 400)
        self.assertEqual(state.cumulative_token_usage.input_tokens, 1200)
        self.assertEqual(state.token_usage.context_tokens, 1200)
        self.assertEqual(state.token_usage.context_window, 4000)
        self.assertEqual(state.token_usage.context_percent, 30.0)
        self.assertEqual(state.token_used, 1200)
        self.assertEqual(state.rate_limits.primary.used_percent, 42.5)
        self.assertEqual(state.rate_limits.credits, 7.5)
        self.assertFalse(state.rate_limits.reached)
        self.assertEqual(state.protocol_capabilities.token_usage.mode, CapabilityMode.DIRECT)
        self.assertEqual(state.protocol_capabilities.rate_limits.mode, CapabilityMode.DIRECT)
        self.assertEqual(events[0].detail, "")

    def test_context_fallback_uses_latest_turn_not_cumulative_input(self) -> None:
        events = normalized(
            20.0,
            "token_count",
            "tokens:fallback-context",
            info={
                "total_token_usage": {"input_tokens": 50_000_000},
                "last_token_usage": {"input_tokens": 120_000},
                "model_context_window": 360_000,
            },
        )
        machine = SessionStateMachine(900)
        machine.ingest("key", events)
        state = machine.derive("key", process(), NetworkEvidence(), 21.0)

        self.assertEqual(state.token_usage.context_tokens, 120_000)
        self.assertAlmostEqual(state.token_usage.context_percent, 33.333, places=2)

    def test_collaboration_events_build_tree_and_surface_child_error(self) -> None:
        events = []
        events += normalized(
            30.0,
            "collab_agent_spawn_end",
            "spawn:child",
            sender_thread_id="thread-root",
            receiver_thread_id="thread-child",
            agent_path="reviewer",
            nickname="Reviewer",
            role="review",
            model="gpt-agent",
        )
        events += normalized(
            31.0,
            "collab_agent_spawn_end",
            "spawn:grandchild",
            sender_thread_id="thread-child",
            receiver_thread_id="thread-grandchild",
            agent_path="reviewer/tests",
            nickname="Tests",
        )
        events += normalized(
            31.5,
            "collab_agent_interaction_begin",
            "interaction:start",
            sender_thread_id="thread-root",
            receiver_thread_id="thread-child",
        )
        events += normalized(
            31.75,
            "collab_agent_interaction_end",
            "interaction:end",
            sender_thread_id="thread-root",
            receiver_thread_id="thread-child",
            duration_ms=250,
        )
        events += normalized(
            32.0,
            "subagent_status",
            "status:error",
            receiver_thread_id="thread-child",
            status="errored",
            error={"message": "tool failed token=secret"},
        )
        machine = SessionStateMachine(900)
        machine.ingest("key", events)
        state = machine.derive("key", process(), NetworkEvidence(), 33.0)

        self.assertEqual(len(state.agents), 1)
        child = state.agents[0]
        self.assertEqual(child.thread_id, "thread-child")
        self.assertEqual(child.agent_path, "reviewer")
        self.assertEqual(child.status, "errored")
        self.assertEqual(child.children[0].thread_id, "thread-grandchild")
        self.assertEqual(child.interaction_count, 1)
        self.assertEqual(child.interaction_seconds, 0.25)
        self.assertIn("[REDACTED]", child.error.message)
        self.assertEqual(state.alert, "SUBAGENT_ERROR")
        self.assertEqual(state.protocol_capabilities.collab_status.mode, CapabilityMode.DIRECT)
        self.assertEqual(state.protocol_capabilities.subagent_path.mode, CapabilityMode.DIRECT)

    def test_current_sub_agent_activity_shape_builds_agent(self) -> None:
        events = normalized(
            1.0,
            "sub_agent_activity",
            "activity:start",
            event_id="call-1",
            occurred_at_ms=30000,
            agent_thread_id="thread-child",
            agent_path="/root/reviewer",
            kind="started",
        )
        events += normalized(
            1.0,
            "sub_agent_activity",
            "activity:interact",
            occurred_at_ms=31000,
            agent_thread_id="thread-child",
            agent_path="/root/reviewer",
            kind="interacted",
        )
        response = normalize_rollout_record(
            {
                "timestamp": 32.0,
                "type": "response_item",
                "payload": {"type": "agent_message", "message": "working"},
            },
            "response:agent-message",
        )
        machine = SessionStateMachine(900)
        machine.ingest("key", events + response)
        state = machine.derive("key", process(), NetworkEvidence(), 33.0)

        self.assertEqual(len(state.agents), 1)
        self.assertEqual(state.agents[0].thread_id, "thread-child")
        self.assertEqual(state.agents[0].agent_path, "/root/reviewer")
        self.assertEqual(state.agents[0].status, "running")
        self.assertEqual(state.agents[0].interaction_count, 1)
        self.assertEqual(state.agents[0].spawned_at, 30.0)
        self.assertEqual(response[0].kind, "MODEL_PROGRESS")
        self.assertEqual(state.protocol_capabilities.collab_status.mode, CapabilityMode.DIRECT)

    def test_absent_protocol_events_leave_each_capability_unavailable(self) -> None:
        machine = SessionStateMachine(900)
        state = machine.derive("empty", process(), NetworkEvidence(), 1.0)
        capabilities = state.protocol_capabilities
        self.assertTrue(
            all(
                value.mode == CapabilityMode.UNAVAILABLE
                for value in capabilities.__dict__.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
