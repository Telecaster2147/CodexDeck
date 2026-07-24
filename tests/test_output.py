from __future__ import annotations

import json
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    AgentNode,
    AxisCompleteness,
    AttentionRequest,
    AttentionState,
    CodexPaths,
    CollectorHealth,
    CommandExecutionSummary,
    Confidence,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
    RateLimitSummary,
    RateLimitWindow,
    SessionHealth,
    SessionCompleteness,
    TerminalAssociationSummary,
    TerminalCapability,
    TerminalChunk,
    TerminalSessionSummary,
    TokenUsageSummary,
    ToolExecutionSummary,
    TurnSummary,
)
from presentation.json_output import render_json  # noqa: E402
from presentation.export import session_export  # noqa: E402
from presentation.doctor import render_doctor_json  # noqa: E402
from presentation.metrics import METRIC_FAMILIES, render_prometheus  # noqa: E402
from presentation.privacy import public_value  # noqa: E402
from presentation.text import render_text  # noqa: E402


def snapshot() -> MonitorSnapshot:
    home = Path("/home/test/.codex")
    paths = CodexPaths(
        home,
        home,
        home / "state_5.sqlite",
        home / "logs_2.sqlite",
        home / "session_index.jsonl",
        home / "sessions",
    )
    session_process = ProcessInfo(
        ProcessIdentity(42, 10),
        1,
        "codex",
        1,
        0.0,
        "S",
        "futex",
        "codex",
        "session",
        instance_id="i1",
        session_id="s1",
        session_title="Active session",
        model="gpt-test",
    )
    auxiliary = ProcessInfo(
        ProcessIdentity(43, 11),
        1,
        "node",
        1,
        0.0,
        "S",
        "futex",
        "node codex",
        "launcher",
        instance_id="i1",
    )
    health = SessionHealth(
        "i1",
        "s1",
        session_process,
        LifecycleState.GENERATING,
        network=NetworkEvidence(NetworkState.ACTIVE),
    )
    instance = InstanceSnapshot(
        "i1",
        paths,
        "~/.codex",
        "~/.codex",
        "environment",
        processes=[session_process, auxiliary],
        sessions=[health],
    )
    return MonitorSnapshot("2026-07-15T00:00:00+08:00", 2.0, [instance])


def snapshot_with_metrics() -> MonitorSnapshot:
    result = snapshot()
    session = result.sessions[0]
    usage = TokenUsageSummary(
        input_tokens=400,
        cached_input_tokens=100,
        output_tokens=80,
        reasoning_output_tokens=20,
        total_tokens=480,
        context_tokens=1200,
        context_window=4000,
    )
    tool = ToolExecutionSummary(
        "call-1",
        turn_id="turn-1",
        display_name="shell",
        duration_seconds=1.5,
        status="completed",
    )
    session.turns = [
        TurnSummary(
            "turn-1",
            duration_seconds=4.5,
            time_to_first_token_seconds=0.75,
            status="completed",
            token_usage=usage,
            tool_count=1,
            tool_duration_seconds=1.5,
            tools=(tool,),
        )
    ]
    session.tool_executions = [tool]
    session.token_usage = usage
    session.cumulative_token_usage = TokenUsageSummary(total_tokens=1500)
    session.rate_limits = RateLimitSummary(
        primary=RateLimitWindow(used_percent=42.5, reset_at=123.0, window_minutes=300),
        credits=7.5,
        reached=False,
    )
    session.agents = [
        AgentNode(
            "agent-1",
            status="running",
            children=[AgentNode("agent-2", parent_thread_id="agent-1", status="completed")],
        )
    ]
    return result


class OutputTests(unittest.TestCase):
    def test_command_collector_budget_stats_are_machine_readable(self) -> None:
        result = snapshot()
        result.instances[0].collector_health = [
            CollectorHealth(
                "process",
                error="ps: stdout_byte_budget",
                command=CommandExecutionSummary(
                    command_name="ps",
                    reason="stdout_byte_budget",
                    stdout_bytes_read=1025,
                    stdout_bytes_retained=100,
                    stdout_bytes_filtered=900,
                    records_retained=2,
                    records_filtered=20,
                    records_dropped=1,
                ),
            )
        ]
        result.collector_health = list(result.instances[0].collector_health)

        payload = json.loads(render_json(result, pretty=False))
        command = payload["instances"][0]["collector_health"][0]["command"]
        metrics = render_prometheus(result)

        self.assertEqual(command["reason"], "stdout_byte_budget")
        self.assertEqual(command["stdout_bytes_filtered"], 900)
        self.assertIn(
            'codexdeck_command_bytes{category="process",disposition="filtered",'
            'stream="stdout"} 900',
            metrics,
        )
        self.assertIn(
            'codexdeck_command_complete{category="process"} 0',
            metrics,
        )

    def test_public_projection_denies_unknown_dataclass_and_dynamic_fields(self) -> None:
        @dataclass
        class Canary:
            secret: str

        self.assertEqual(public_value(Canary("NESTED_CANARY_SECRET")), {})
        result = snapshot()
        result.sessions[0].events = [
            NormalizedEvent(
                1.0,
                "MODEL_PROGRESS",
                "progress",
                metadata={
                    "phase": "generating",
                    "future_private_field": "NESTED_CANARY_SECRET",
                },
            )
        ]
        result.instances[0].rollout_activity = [
            {
                "backlog_bytes": 10,
                "future_private_field": "NESTED_CANARY_SECRET",
            }
        ]

        payload = json.loads(render_json(result, pretty=False))
        event = payload["instances"][0]["sessions"][0]["events"][0]
        activity = payload["instances"][0]["rollout_activity"][0]
        self.assertEqual(event["metadata"], {"phase": "generating"})
        self.assertEqual(activity, {"backlog_bytes": 10})
        rendered = "\n".join(
            (
                json.dumps(payload),
                json.dumps(session_export(result.sessions[0], result.sessions[0].events)),
                render_doctor_json(result),
                render_text(result),
                render_prometheus(result),
            )
        )
        self.assertNotIn("NESTED_CANARY_SECRET", rendered)

    def test_json_exposes_terminal_summary_without_transcript_body(self) -> None:
        result = snapshot()
        transcript = "TRANSCRIPT_SENTINEL_84721"
        result.sessions[0].terminal_sessions = [
            TerminalSessionSummary(
                "terminal-1",
                process_id="321",
                command="server --watch",
                status="running",
                process_active=True,
                capability=TerminalCapability.POLL_TRANSCRIPT,
                retained_bytes=12,
                chunks=(TerminalChunk("source", 1.0, text=transcript),),
            )
        ]
        tool = ToolExecutionSummary(
            "call-1",
            status="completed",
            output=transcript,
        )
        result.sessions[0].tool_executions = [tool]
        result.sessions[0].turns = [TurnSummary("turn-1", tools=(tool,))]
        result.sessions[0].events = [
            NormalizedEvent(
                1.0,
                "TOOL_COMPLETED",
                "shell",
                source="rollout",
                confidence=Confidence.HIGH,
                metadata={
                    "output": transcript,
                    "diagnostic_payload": transcript,
                    "nested": {"stderr": transcript},
                },
            )
        ]

        rendered_json = render_json(result, pretty=False)
        terminal = json.loads(rendered_json)["instances"][0]["sessions"][0]["terminal_sessions"][0]
        rendered_text = render_text(result)
        metrics = render_prometheus(result)
        exported = json.dumps(session_export(result.sessions[0], result.sessions[0].events))

        self.assertEqual(terminal["process_id"], "321")
        self.assertTrue(terminal["process_active"])
        self.assertEqual(terminal["capability"], "POLL_TRANSCRIPT")
        self.assertNotIn("chunks", terminal)
        self.assertIn("Terminal：1 个，运行中 1 个 | POLL_TRANSCRIPT", rendered_text)
        self.assertIn(
            'codexdeck_terminal_sessions{capability="POLL_TRANSCRIPT",instance="i1"} 1',
            metrics,
        )
        self.assertNotIn(transcript, rendered_json + rendered_text + metrics)
        public_session = json.loads(rendered_json)["instances"][0]["sessions"][0]
        self.assertIsNone(public_session["tool_executions"][0]["output"])
        self.assertIsNone(public_session["turns"][0]["tools"][0]["output"])
        self.assertEqual(public_session["events"][0]["metadata"], {})
        self.assertNotIn("terminal_sessions", exported)
        self.assertNotIn(transcript, exported)

    def test_attention_is_present_in_json_text_and_metrics(self) -> None:
        result = snapshot()
        session = result.sessions[0]
        session.attention = AttentionState.APPROVAL
        session.attention_request = AttentionRequest(
            AttentionState.APPROVAL,
            call_id="call-1",
            summary="等待用户操作",
            detail="Approve command",
        )

        payload = json.loads(render_json(result, pretty=False))
        rendered_text = render_text(result)
        metrics = render_prometheus(result)

        self.assertEqual(payload["summary"]["action_required"], 1)
        self.assertEqual(payload["instances"][0]["sessions"][0]["attention"], "APPROVAL")
        self.assertIn("待操作 1", rendered_text)
        self.assertIn("Approve command", rendered_text)
        self.assertIn(
            'codexdeck_attention_sessions{instance="i1",state="APPROVAL"} 1',
            metrics,
        )

    def test_protocol_uncertainty_is_shared_by_text_and_machine_output(self) -> None:
        result = snapshot()
        session = result.sessions[0]
        session.protocol_uncertain = True
        session.protocol_uncertainty_scope = "attention"
        session.protocol_uncertainty_reason = "future approval shape"
        session.lifecycle_confidence = Confidence.LOW
        session.attention_confidence = Confidence.LOW
        session.phase = "协议不确定（可能等待交互）"

        payload = json.loads(render_json(result, pretty=False))
        public_session = payload["instances"][0]["sessions"][0]

        self.assertTrue(public_session["protocol_uncertain"])
        self.assertEqual(public_session["protocol_uncertainty_scope"], "attention")
        self.assertEqual(public_session["lifecycle_confidence"], "low")
        self.assertIn("协议不确定（可能等待交互）", render_text(result))

    def test_terminal_association_counts_and_coverage_are_machine_readable(self) -> None:
        result = snapshot()
        result.sessions[0].terminal_association = TerminalAssociationSummary(
            eligible_operations=4,
            associated_operations=3,
            confirmed=2,
            ambiguous=1,
            unresolved=1,
            reasons=(("missing_process_and_call_id", 1),),
            association_coverage=0.75,
            unresolved_rate=0.25,
        )

        payload = json.loads(render_json(result, pretty=False))
        association = payload["instances"][0]["sessions"][0]["terminal_association"]

        self.assertEqual(association["eligible_operations"], 4)
        self.assertEqual(association["association_coverage"], 0.75)
        self.assertIsNone(association["precision"])
        self.assertIn("coverage 75.0%", render_text(result))
        metrics = render_prometheus(result)
        self.assertIn(
            'codexdeck_terminal_association_operations{instance="i1",status="unresolved"} 1',
            metrics,
        )
        self.assertIn(
            'codexdeck_terminal_association_coverage{instance="i1"} 0.75',
            metrics,
        )
        self.assertNotIn('codexdeck_terminal_association_precision{instance="i1"}', metrics)

    def test_json_has_versioned_instance_shape(self) -> None:
        payload = json.loads(render_json(snapshot(), pretty=True))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "generated_at",
                "interval_seconds",
                "collection_duration_seconds",
                "summary",
                "diagnostics",
                "collector_health",
                "observer",
                "temporal",
                "history",
                "instances",
            },
        )
        self.assertEqual(payload["instances"][0]["instance_id"], "i1")
        self.assertEqual(payload["instances"][0]["sessions"][0]["network"]["state"], "ACTIVE")
        self.assertEqual(len(payload["instances"][0]["processes"]), 1)
        self.assertIsNone(payload["instances"][0]["sessions"][0]["alert"])
        self.assertIsNone(payload["instances"][0]["sessions"][0]["process"]["current_task"])
        self.assertEqual(
            payload["instances"][0]["sessions"][0]["silence"]["state"],
            "NORMAL",
        )

    def test_structured_diagnostics_are_private_and_shared_with_metrics(self) -> None:
        value = snapshot()
        value.diagnostics = ["failed at /home/USER/private.sqlite TOKEN=SECRET_VALUE"]
        value.instances[0].unknown_event_types = {"future_shape": 2}

        payload = json.loads(render_json(value, pretty=False))
        rendered = json.dumps(payload["diagnostics"], ensure_ascii=False)
        self.assertNotIn("/home/USER", rendered)
        self.assertNotIn("SECRET_VALUE", rendered)
        codes = {item["code"] for item in payload["diagnostics"]}
        self.assertIn("SOURCE_REPORTED_DEGRADED", codes)
        self.assertIn("PROTOCOL_UNKNOWN", codes)
        metrics = render_prometheus(value)
        self.assertIn('codexdeck_diagnostics{code="PROTOCOL_UNKNOWN"', metrics)
        self.assertIn("last_semantic_at", payload["instances"][0]["sessions"][0]["observation"])
        self.assertNotIn("identity", payload["instances"][0])
        self.assertNotIn("identity", payload["instances"][0]["sessions"][0])
        self.assertNotIn(
            "instance_identity",
            payload["instances"][0]["sessions"][0]["process"],
        )

    def test_metrics_family_contract_is_frozen(self) -> None:
        output = render_prometheus(snapshot_with_metrics())
        families = tuple(
            line.split()[2] for line in output.splitlines() if line.startswith("# HELP ")
        )
        self.assertEqual(families, METRIC_FAMILIES)

    def test_axis_completeness_is_shared_by_json_text_doctor_and_metrics(self) -> None:
        value = snapshot()
        session = value.sessions[0]
        session.completeness = SessionCompleteness(
            lifecycle=AxisCompleteness(
                "lifecycle",
                complete=False,
                confidence=Confidence.LOW,
                reason="bootstrap gap",
                baseline_kind="missing_after_gap",
                evidence=("bootstrap_tail_truncated",),
            )
        )

        payload = json.loads(render_json(value, pretty=False))
        doctor = json.loads(render_doctor_json(value))
        text = render_text(value)
        metrics = render_prometheus(value)

        lifecycle = payload["instances"][0]["sessions"][0]["completeness"]["lifecycle"]
        self.assertFalse(lifecycle["complete"])
        self.assertEqual(
            doctor["instances"][0]["state_completeness"][0]["incomplete_axes"],
            ["lifecycle"],
        )
        self.assertIn("证据不完整(lifecycle)", text)
        self.assertIn(
            'codexdeck_state_axis_completeness{axis="lifecycle",instance="i1",status="incomplete"} 1',
            metrics,
        )

    def test_ingress_backlog_gap_and_budget_are_shared_by_json_and_metrics(self) -> None:
        value = snapshot()
        instance = value.instances[0]
        instance.rollout_activity = [
            {
                "path": "/workspace-a/rollout.jsonl",
                "backlog_bytes": 2048,
                "backlog_records_lower_bound": 3,
                "backlog_age_seconds": 1.5,
                "budget_exceeded": True,
                "gap_count": 1,
                "skipped_bytes": 300000,
                "gap_reason": "oversize_jsonl_record",
            }
        ]

        payload = json.loads(render_json(value, pretty=False))
        metrics = render_prometheus(value)

        activity = payload["instances"][0]["rollout_activity"][0]
        self.assertEqual(activity["backlog_bytes"], 2048)
        self.assertEqual(activity["backlog_records_lower_bound"], 3)
        self.assertEqual(activity["gap_reason"], "oversize_jsonl_record")
        self.assertIn(
            'codexdeck_ingress_backlog_bytes{instance="i1",source="rollout"} 2048',
            metrics,
        )
        self.assertIn(
            'codexdeck_ingress_backlog_records_lower_bound{instance="i1",source="rollout"} 3',
            metrics,
        )
        self.assertIn(
            'codexdeck_ingress_gap_total{instance="i1",source="rollout"} 1',
            metrics,
        )

    def test_all_includes_auxiliary_processes_in_json_and_text(self) -> None:
        payload = json.loads(render_json(snapshot(), pretty=False, show_auxiliary=True))
        self.assertEqual(len(payload["instances"][0]["processes"]), 2)
        self.assertIn("辅助进程", render_text(snapshot(), show_auxiliary=True))

    def test_json_additive_metrics_keep_schema_one_and_nullable_strings(self) -> None:
        payload = json.loads(render_json(snapshot_with_metrics(), pretty=False))
        self.assertEqual(payload["schema_version"], 1)
        session = payload["instances"][0]["sessions"][0]
        self.assertEqual(session["turns"][0]["time_to_first_token_seconds"], 0.75)
        self.assertEqual(session["turns"][0]["tools"][0]["display_name"], "shell")
        self.assertIsNone(session["turns"][0]["tools"][0]["command"])
        self.assertIsNone(session["turns"][0]["tools"][0]["arguments"])
        self.assertIsNone(session["turns"][0]["tools"][0]["output"])
        self.assertEqual(session["token_usage"]["context_tokens"], 1200)
        self.assertEqual(session["rate_limits"]["primary"]["used_percent"], 42.5)
        self.assertEqual(session["agents"][0]["children"][0]["thread_id"], "agent-2")
        self.assertIsNone(session["turns"][0]["collaboration_mode"])
        self.assertIsNone(session["turns"][0]["trace_id"])
        self.assertIsNone(session["agents"][0]["parent_thread_id"])
        self.assertIsNone(session["agents"][0]["provenance"]["source"])

    def test_text_summarizes_turn_tools_tokens_limits_and_subagents(self) -> None:
        output = render_text(snapshot_with_metrics())
        self.assertIn("Turn turn-1 | completed | 耗时 4s | TTFT 0.75s | 工具 1 / 1s", output)
        self.assertIn("Token：本 Turn in 400 / cached 100 / out 80", output)
        self.assertIn("累计 total 1500；上下文 1200/4000 (30.0%)", output)
        self.assertIn("Rate limit：primary 42.5% | credits 7.5 | 未触限", output)
        self.assertIn("Subagent：2 个 | completed 1 / running 1", output)

    def test_compact_json_is_one_line(self) -> None:
        output = render_json(snapshot(), pretty=False)
        self.assertNotIn("\n", output)
        json.loads(output)


if __name__ == "__main__":
    unittest.main()
