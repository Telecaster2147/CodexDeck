from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex.events import (  # noqa: E402
    normalize_attention_record,
    normalize_collaboration_record,
    normalize_compaction_record,
    normalize_log,
    normalize_response_item,
    normalize_rollout_record,
    normalize_tool_record,
)
from codex.state_store import LogRecord  # noqa: E402
from models import (  # noqa: E402
    AlertStatus,
    AttentionState,
    Confidence,
    EvidenceCoverage,
    InstanceIdentity,
    InstanceIdentityRegistry,
    LifecycleState,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
    RolloutIdentity,
    RecoveryState,
    SessionIdentity,
    SocketInfo,
    SocketFlowIdentity,
    TerminalIdentity,
)
from network.classifier import assess_process_network  # noqa: E402
from network.sockets import parse_ss_output  # noqa: E402
from state_machine import BoundedDedupeFilter, SessionStateMachine  # noqa: E402
from utils import redact_sensitive, redact_structured  # noqa: E402


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


class IdentityValueTests(unittest.TestCase):
    def test_composite_identities_keep_independent_domains_distinct(self) -> None:
        instance_a = InstanceIdentity(Path("/CODEX_HOME_A"), Path("/SQLITE_HOME_A"))
        instance_b = InstanceIdentity(Path("/CODEX_HOME_B"), Path("/SQLITE_HOME_B"))
        session_a = SessionIdentity(instance_a, "SESSION_ID")
        session_b = SessionIdentity(instance_b, "SESSION_ID")

        self.assertNotEqual(instance_a, instance_b)
        self.assertNotEqual(session_a, session_b)
        self.assertNotEqual(session_a.storage_key, session_b.storage_key)
        self.assertEqual(session_a.display_key, "SESSION_")
        self.assertNotEqual(ProcessIdentity(42, 100), ProcessIdentity(42, 101))
        self.assertNotEqual(
            RolloutIdentity(Path("/workspace-a/rollout.jsonl"), 1, 10),
            RolloutIdentity(Path("/workspace-a/rollout.jsonl"), 1, 11),
        )
        self.assertNotEqual(
            TerminalIdentity(session_a, "PROCESS_ID", "CALL_ID", 1),
            TerminalIdentity(session_a, "PROCESS_ID", "CALL_ID", 2),
        )
        self.assertNotEqual(
            SocketFlowIdentity("127.0.0.1:1", "192.0.2.1:443", 42, 3),
            SocketFlowIdentity("127.0.0.1:1", "192.0.2.1:443", 42, 4),
        )

    def test_forced_surrogate_collision_keeps_canonical_instances_separate(self) -> None:
        registry = InstanceIdentityRegistry()
        first = InstanceIdentity(Path("/CODEX_HOME_A"), Path("/SQLITE_HOME_A"), "COLLISION")
        second = InstanceIdentity(Path("/CODEX_HOME_B"), Path("/SQLITE_HOME_B"), "COLLISION")

        first_key, first_collision = registry.register(first)
        second_key, second_collision = registry.register(second)

        self.assertEqual(first_key, "COLLISION")
        self.assertFalse(first_collision)
        self.assertTrue(second_collision)
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(len(second_key), 32)
        self.assertNotEqual(first.canonical_key, second.canonical_key)


class PrivacyRedactionTests(unittest.TestCase):
    def test_best_effort_redaction_covers_known_credential_formats(self) -> None:
        source = "\n".join(
            (
                "Authorization: Basic dXNlcjpwYXNzd29yZA==",
                "Cookie: session=COOKIE_SECRET; theme=dark",
                "aws=AKIAABCDEFGHIJKLMNOP",
                "AWS_SECRET_ACCESS_KEY=aws-secret-value",
                "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
                "db=postgresql://user:DB_PASSWORD@db.example.invalid/app",
                "url=https://alice:BASIC_PASSWORD@example.invalid/path",
                "-----BEGIN PRIVATE KEY-----\nPEM_SECRET\n-----END PRIVATE KEY-----",
            )
        )

        redacted = redact_sensitive(source)

        for secret in (
            "dXNlcjpwYXNzd29yZA==",
            "COOKIE_SECRET",
            "AKIAABCDEFGHIJKLMNOP",
            "aws-secret-value",
            "eyJhbGciOiJIUzI1NiJ9",
            "DB_PASSWORD",
            "BASIC_PASSWORD",
            "PEM_SECRET",
        ):
            self.assertNotIn(secret, redacted)

    def test_best_effort_redaction_preserves_common_non_secret_identifiers(self) -> None:
        source = (
            "sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef "
            "uuid=123e4567-e89b-12d3-a456-426614174000 "
            "name=sketch-abcdefghijklmnop"
        )

        self.assertEqual(redact_sensitive(source), source)

    def test_unknown_custom_credential_format_documents_detection_limit(self) -> None:
        source = "opaque credential CUSTOMCRED_Z9Y8X7W6V5U4"

        self.assertEqual(redact_sensitive(source), source)
        self.assertEqual(
            redact_structured({"cookie": "value", "opaque": source}),
            {"cookie": "[REDACTED]", "opaque": source},
        )


class EventNormalizationTests(unittest.TestCase):
    def test_rollout_handlers_own_their_record_families(self) -> None:
        cases = (
            (
                "attention",
                normalize_attention_record(1.0, "request_user_input", {}, "source", "turn"),
                "ACTION_REQUIRED",
            ),
            (
                "tool",
                normalize_tool_record(
                    1.0,
                    "exec_command_begin",
                    {"type": "exec_command_begin", "command": "true"},
                    "source",
                    "turn",
                ),
                "TOOL_RUNNING",
            ),
            (
                "collaboration",
                normalize_collaboration_record(
                    1.0,
                    "collab_agent_spawn_end",
                    {"type": "collab_agent_spawn_end", "thread_id": "child"},
                    "source",
                    "turn",
                ),
                "AGENT_SPAWNED",
            ),
            (
                "response",
                normalize_response_item(
                    1.0,
                    "agent_message",
                    {"type": "agent_message", "message": "progress"},
                    "source",
                    "turn",
                ),
                "MODEL_PROGRESS",
            ),
            (
                "compaction",
                normalize_compaction_record(
                    1.0,
                    "event_msg",
                    "item_started",
                    {"type": "item_started", "item": {"type": "compaction"}},
                    "source",
                    "turn",
                ),
                "COMPACTING",
            ),
        )

        for name, events, expected_kind in cases:
            with self.subTest(name=name):
                self.assertIsNotNone(events)
                self.assertEqual(events[0].kind, expected_kind)  # type: ignore[index]

    def test_action_required_protocol_events_are_typed(self) -> None:
        fixtures = {
            "exec_approval_request": AttentionState.APPROVAL,
            "apply_patch_approval_request": AttentionState.APPROVAL,
            "request_permissions": AttentionState.PERMISSIONS,
            "request_user_input": AttentionState.USER_INPUT,
            "elicitation_request": AttentionState.MCP_ELICITATION,
            "auth_elicitation_request": AttentionState.AUTH_ELICITATION,
        }
        for item_type, expected in fixtures.items():
            with self.subTest(item_type=item_type):
                events = normalize_rollout_record(
                    {
                        "timestamp": 10.0,
                        "type": "event_msg",
                        "payload": {
                            "type": item_type,
                            "turn_id": "turn-1",
                            "call_id": "call-1",
                            "reason": "需要确认",
                        },
                    },
                    item_type,
                )
                self.assertEqual(events[0].kind, "ACTION_REQUIRED")
                self.assertEqual(events[0].metadata["attention_state"], expected.value)

    def test_url_elicitation_is_classified_as_auth(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": 10.0,
                "type": "event_msg",
                "payload": {
                    "type": "elicitation_request",
                    "request": {"mode": "url", "message": "Sign in"},
                },
            },
            "auth-url",
        )

        self.assertEqual(events[0].metadata["attention_state"], "AUTH_ELICITATION")

    def test_attention_response_protocol_events_are_typed(self) -> None:
        for item_type in (
            "exec_approval",
            "patch_approval",
            "resolve_elicitation",
            "user_input_answer",
            "request_permissions_response",
        ):
            with self.subTest(item_type=item_type):
                events = normalize_rollout_record(
                    {
                        "timestamp": 11.0,
                        "type": "event_msg",
                        "payload": {"type": item_type, "call_id": "call-1"},
                    },
                    item_type,
                )
                self.assertEqual([event.kind for event in events], ["ACTION_RESOLVED"])

    def test_explicit_compact_user_message_requests_compaction(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": "2026-07-17T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "/compact"},
            },
            "compact-command",
        )

        self.assertEqual([event.kind for event in events], ["COMPACT_REQUESTED"])
        self.assertEqual(events[0].metadata["trigger"], "manual")

    def test_context_compacted_can_backfill_manual_start(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": "2026-07-17T00:01:00Z",
                "type": "event_msg",
                "payload": {"type": "context_compacted"},
            },
            "compact-completed",
            inferred_manual_compact=True,
            context_tokens=216_402,
            context_window=353_400,
            compact_started_at=1784246402.0,
            compact_started_source_id="compact-started",
            compact_started_turn_id="compact-turn",
        )

        self.assertEqual(
            [event.kind for event in events],
            ["COMPACTING", "COMPACT_COMPLETED"],
        )
        self.assertEqual(events[0].timestamp, 1784246402.0)
        self.assertEqual(events[1].metadata["trigger"], "manual")

    def test_reasoning_summary_preserves_official_summary_text(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": "2026-07-16T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "检查刷新路径"}],
                    "encrypted_content": "ciphertext",
                },
            },
            "reasoning",
        )

        self.assertEqual(events[0].kind, "REASONING_SUMMARY")
        self.assertEqual(events[0].detail, "检查刷新路径")
        self.assertTrue(events[0].metadata["summary_available"])
        self.assertTrue(events[0].metadata["encrypted"])

    def test_known_protocol_records_are_not_unparsed(self) -> None:
        for role in ("user", "system", "developer", None):
            with self.subTest(role=role):
                payload = {
                    "type": "message",
                    "content": [{"type": "input_text", "text": "protocol context"}],
                }
                if role is not None:
                    payload["role"] = role
                events = normalize_rollout_record(
                    {
                        "timestamp": "2026-07-16T00:00:00Z",
                        "type": "response_item",
                        "payload": payload,
                    },
                    f"message:{role}",
                )

                self.assertEqual(events, [])

        fixtures = {
            "web_search_call": ("web_search", "网页搜索"),
            "image_generation_call": ("image_generation", "生成图片"),
        }
        for item_type, (tool_name, display_name) in fixtures.items():
            with self.subTest(item_type=item_type):
                events = normalize_rollout_record(
                    {
                        "timestamp": 10.0,
                        "type": "response_item",
                        "payload": {
                            "type": item_type,
                            "status": "completed",
                            "action": {"query": "protocol fixture"},
                        },
                    },
                    item_type,
                )
                self.assertEqual([event.kind for event in events], ["TOOL_COMPLETED"])
                self.assertEqual(events[0].metadata["tool_name"], tool_name)
                self.assertEqual(events[0].metadata["display_name"], display_name)
                self.assertTrue(events[0].complete)

        for item_type in ("web_search_end", "image_generation_end"):
            with self.subTest(item_type=item_type):
                self.assertEqual(
                    normalize_rollout_record(
                        {
                            "timestamp": 10.0,
                            "type": "event_msg",
                            "payload": {"type": item_type, "call_id": "call-1"},
                        },
                        item_type,
                    ),
                    [],
                )

    def test_unparsed_record_retains_full_redacted_diagnostic_payload(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": 10.0,
                "type": "event_msg",
                "payload": {
                    "type": "future_protocol_event",
                    "token": "secret-value",
                    "access_token": "access-value",
                    "client_secret": "client-value",
                    "nested": {"refresh-token": "refresh-value"},
                    "detail": "x" * 400,
                },
            },
            "future",
        )

        self.assertEqual([event.kind for event in events], ["UNPARSED_PAYLOAD"])
        payload = events[0].metadata["diagnostic_payload"]
        self.assertIn('"detail": "' + "x" * 300, payload)
        self.assertIn('"token": "[REDACTED]"', payload)
        self.assertNotIn("secret-value", payload)
        self.assertNotIn("access-value", payload)
        self.assertNotIn("client-value", payload)
        self.assertNotIn("refresh-value", payload)
        self.assertTrue(events[0].unparsed.truncated)
        self.assertEqual(
            events[0].metadata["diagnostic_redaction"],
            "best_effort_known_formats",
        )

    def test_unparsed_diagnostic_payload_has_a_hard_retention_limit(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": 10.0,
                "type": "event_msg",
                "payload": {"type": "future_protocol_event", "detail": "x" * 10_000},
            },
            "future",
        )

        event = events[0]
        self.assertEqual(len(event.metadata["diagnostic_payload"]), 4 * 1024)
        self.assertGreater(event.metadata["diagnostic_payload_dropped_chars"], 0)

    def test_unparsed_records_distinguish_semantic_and_auxiliary_unknowns(self) -> None:
        phase = normalize_rollout_record(
            {
                "timestamp": 10.0,
                "type": "event_msg",
                "payload": {"type": "future_phase_transition", "status": "running"},
            },
            "phase",
        )[0]
        telemetry = normalize_rollout_record(
            {
                "timestamp": 11.0,
                "type": "event_msg",
                "payload": {"type": "future_usage_telemetry", "count": 3},
            },
            "telemetry",
        )[0]

        self.assertEqual(phase.metadata["semantic_scope"], "lifecycle")
        self.assertEqual(telemetry.metadata["semantic_scope"], "auxiliary")

    def test_nested_exec_and_patch_input_exposes_command_cwd_and_files(self) -> None:
        tool_input = (
            'const result = await tools.exec_command({"cmd":"rg -n refresh src",'
            '"workdir":"/workspace/project"});\n'
            'const patch = "*** Begin Patch\\n*** Update File: '
            '/workspace/project/src/app.py\\n*** End Patch";\n'
            "text(await tools.apply_patch(patch));"
        )
        events = normalize_rollout_record(
            {
                "timestamp": 10.0,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-1",
                    "name": "exec",
                    "input": tool_input,
                },
            },
            "tool",
        )
        events += normalize_rollout_record(
            {
                "timestamp": 20.0,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": (
                        "Script running with cell ID 199\nWall time 10.0 seconds\nOutput:\n"
                    ),
                },
            },
            "tool-output",
        )

        metadata = events[0].metadata
        self.assertEqual(events[0].kind, "TOOL_RUNNING")
        self.assertEqual(metadata["command"], "rg -n refresh src")
        self.assertEqual(metadata["cwd"], "/workspace/project")
        self.assertEqual(metadata["files"], ["/workspace/project/src/app.py"])
        self.assertEqual(metadata["nested_tools"], ["exec_command", "apply_patch"])
        self.assertEqual(metadata["tool_name"], "exec_command + apply_patch")
        self.assertEqual(metadata["display_name"], "Shell 命令 + 应用补丁")
        self.assertEqual(metadata["category"], "shell")
        output = events[1].metadata
        self.assertEqual(output["output"], "")
        self.assertEqual(output["tool_name"], "")
        self.assertEqual(output["display_name"], "")
        self.assertTrue(output["display_name_is_fallback"])
        self.assertTrue(output["background_running"])
        self.assertEqual(output["background_cell_id"], "199")
        self.assertEqual(output["background_wait_seconds"], 10.0)
        self.assertTrue(output["background_output_empty"])

    def test_patch_apply_end_records_exact_file_operations(self) -> None:
        events = normalize_rollout_record(
            {
                "timestamp": 20.0,
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "turn_id": "turn-1",
                    "call_id": "call-1",
                    "success": True,
                    "status": "completed",
                    "changes": {
                        "/workspace/project/src/app.py": {
                            "type": "update",
                            "unified_diff": "@@ -1 +1 @@",
                        },
                        "/workspace/project/tests/test_app.py": {
                            "type": "add",
                            "unified_diff": "@@ -0,0 +1 @@",
                        },
                    },
                },
            },
            "patch",
        )

        self.assertEqual(events[0].kind, "FILE_CHANGE_APPLIED")
        self.assertEqual(len(events[0].metadata["files"]), 2)
        self.assertEqual(
            events[0].metadata["change_types"]["/workspace/project/src/app.py"],
            "update",
        )

    def test_tool_names_are_specific_for_plan_and_mcp_calls(self) -> None:
        plan = normalize_rollout_record(
            {
                "timestamp": 10.0,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-plan",
                    "name": "update_plan",
                    "input": '{"plan":[]}',
                },
            },
            "plan",
        )[0]
        mcp = normalize_rollout_record(
            {
                "timestamp": 11.0,
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_begin",
                    "call_id": "call-mcp",
                    "server": "filesystem",
                    "tool": "read_file",
                },
            },
            "mcp",
        )[0]

        self.assertEqual(plan.metadata["tool_name"], "update_plan")
        self.assertEqual(plan.metadata["display_name"], "更新计划")
        self.assertEqual(plan.metadata["category"], "plan")
        self.assertEqual(mcp.metadata["tool_name"], "read_file")
        self.assertEqual(mcp.metadata["display_name"], "MCP filesystem/read_file")
        self.assertEqual(mcp.metadata["category"], "mcp")

    def test_real_compaction_log_shapes_mark_start_and_completion(self) -> None:
        start = normalize_log(
            LogRecord(
                1,
                100.0,
                "INFO",
                "codex_http_client::transport",
                "thread-1",
                "pid:42:session",
                "session_task.run:run_turn:run_auto_compact{reason=ContextLimit "
                "phase=MidTurn}: POST to https://example.test/responses",
            )
        )
        completed = normalize_log(
            LogRecord(
                2,
                180.0,
                "INFO",
                "codex_api::sse::responses",
                "thread-1",
                "pid:42:session",
                'SSE event: {"type":"response.completed","response":'
                '{"object":"response.compaction"}}',
            )
        )

        self.assertEqual(start[0].kind, "COMPACTING")
        self.assertEqual(completed[0].kind, "COMPACT_COMPLETED")

    def test_rollout_model_configuration_is_normalized_from_current_shapes(self) -> None:
        turn_context = normalize_rollout_record(
            {
                "timestamp": "2026-07-16T00:00:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-current", "reasoning_effort": "high"},
            },
            "context",
        )
        settings = normalize_rollout_record(
            {
                "timestamp": "2026-07-16T00:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {
                        "model": "gpt-updated",
                        "reasoning_effort": "medium",
                    },
                },
            },
            "settings",
        )

        self.assertEqual(turn_context[0].kind, "MODEL_CONFIG")
        self.assertEqual(turn_context[0].metadata["model"], "gpt-current")
        self.assertEqual(settings[0].metadata["model"], "gpt-updated")
        self.assertEqual(settings[0].metadata["reasoning_effort"], "medium")

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
                            "response_too_many_failed_attempts": {"http_status_code": 502}
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
        from codex.events import normalize_log
        from codex.state_store import LogRecord

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
    @staticmethod
    def _unknown(timestamp: float, scope: str, source_id: str) -> NormalizedEvent:
        return NormalizedEvent(
            timestamp,
            "UNPARSED_PAYLOAD",
            "unknown",
            source="rollout",
            confidence=Confidence.LOW,
            source_id=source_id,
            complete=False,
            metadata={"semantic_scope": scope},
        )

    def test_explicit_evidence_gap_recovers_only_axes_with_trusted_baselines(self) -> None:
        machine = SessionStateMachine(3600)
        key = "coverage"
        observed_at = 1.0
        machine.update_coverage(
            key,
            EvidenceCoverage(
                observed_at,
                source_epoch="stream:1",
                gap_count=1,
                terminal_probe_complete=False,
                network_probe_complete=True,
                silence_probe_complete=True,
            ),
        )

        initial = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE))
        self.assertFalse(initial.completeness.lifecycle.complete)
        self.assertFalse(initial.completeness.attention.complete)
        self.assertFalse(initial.completeness.failure_recovery.complete)
        self.assertFalse(initial.completeness.terminal_ownership.complete)
        self.assertTrue(initial.completeness.network.complete)
        self.assertFalse(initial.completeness.silence.complete)
        self.assertEqual(initial.attention, AttentionState.NONE)

        machine.ingest(key, [event(10, "ACTION_RESOLVED", "resolved")])
        attention = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE))
        self.assertTrue(attention.completeness.attention.complete)
        self.assertFalse(attention.completeness.lifecycle.complete)
        self.assertFalse(attention.completeness.failure_recovery.complete)

        machine.ingest(key, [event(11, "REQUEST_SENT", "request")])
        lifecycle = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE))
        self.assertTrue(lifecycle.completeness.lifecycle.complete)
        self.assertFalse(lifecycle.completeness.failure_recovery.complete)
        self.assertTrue(lifecycle.completeness.silence.complete)

        machine.ingest(key, [event(12, "TURN_STARTED", "turn")])
        failure = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE))
        self.assertTrue(failure.completeness.failure_recovery.complete)

        machine.update_coverage(
            key,
            EvidenceCoverage(
                20.0,
                source_epoch="stream:2",
                generation_changed=True,
                network_probe_complete=True,
                silence_probe_complete=True,
            ),
        )
        replaced = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE))
        self.assertFalse(replaced.completeness.lifecycle.complete)
        self.assertFalse(replaced.completeness.attention.complete)
        self.assertFalse(replaced.completeness.failure_recovery.complete)
        self.assertFalse(replaced.completeness.terminal_ownership.complete)
        self.assertTrue(replaced.completeness.network.complete)

        machine.update_coverage(
            key,
            EvidenceCoverage(
                21.0,
                source_epoch="stream:2",
                terminal_probe_complete=True,
                network_probe_complete=True,
                silence_probe_complete=True,
            ),
        )
        terminal = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE))
        self.assertTrue(terminal.completeness.terminal_ownership.complete)
        self.assertFalse(terminal.completeness.lifecycle.complete)

    def test_bootstrap_tail_recovers_axes_after_current_baselines(self) -> None:
        machine = SessionStateMachine(3600)
        key = "bootstrap-tail"
        machine.update_coverage(
            key,
            EvidenceCoverage(
                1.0,
                source_epoch="stream:1",
                bootstrap_truncated=True,
                terminal_probe_complete=True,
                network_probe_complete=True,
                silence_probe_complete=True,
            ),
        )

        initial = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE))
        self.assertFalse(initial.completeness.lifecycle.complete)
        self.assertFalse(initial.completeness.attention.complete)
        self.assertFalse(initial.completeness.failure_recovery.complete)
        self.assertTrue(initial.completeness.terminal_ownership.complete)

        machine.ingest(key, [event(2.0, "MODEL_PROGRESS", "current-baseline")])
        state = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE), now=3.0)

        self.assertEqual(state.completeness.incomplete_axes, ())

    def test_backlog_is_temporary_and_retention_preserves_axis_baselines(self) -> None:
        machine = SessionStateMachine(3600)
        key = "backlog"
        machine.update_coverage(
            key,
            EvidenceCoverage(time.time(), source_epoch="stream:1", backlog_pending=True),
        )
        pending = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE))
        self.assertFalse(pending.completeness.lifecycle.complete)
        self.assertFalse(pending.completeness.attention.complete)

        machine.update_coverage(
            key,
            EvidenceCoverage(time.time() + 1, source_epoch="stream:1"),
        )
        caught_up = machine.derive(key, process(), NetworkEvidence(NetworkState.IDLE))
        self.assertTrue(caught_up.completeness.lifecycle.complete)
        self.assertTrue(caught_up.completeness.attention.complete)

        retained_key = "retention"
        records = [event(1, "ACTION_REQUIRED", "attention")]
        records.extend(event(index + 2, "KEEPALIVE", f"keepalive-{index}") for index in range(501))
        machine.ingest(retained_key, records)
        retained = machine.derive(
            retained_key,
            process(),
            NetworkEvidence(NetworkState.IDLE),
            now=600,
        )
        self.assertEqual(len(retained.events), 500)
        self.assertEqual(retained.attention, AttentionState.USER_INPUT)
        self.assertTrue(retained.completeness.attention.complete)

        lifecycle_key = "retained-lifecycle"
        lifecycle_records = [event(1, "REQUEST_SENT", "request")]
        lifecycle_records.extend(
            event(index + 2, "KEEPALIVE", f"keepalive-{index}") for index in range(501)
        )
        machine.ingest(lifecycle_key, lifecycle_records)
        lifecycle = machine.derive(
            lifecycle_key,
            process(),
            NetworkEvidence(NetworkState.IDLE),
            now=600,
        )
        self.assertEqual(lifecycle.lifecycle, LifecycleState.WAITING_RESPONSE)
        self.assertTrue(lifecycle.completeness.lifecycle.complete)

        machine.ingest(retained_key, [event(604, "ACTION_RESOLVED", "retained-clear")])
        cleared = machine.derive(
            retained_key,
            process(),
            NetworkEvidence(NetworkState.IDLE),
            now=605,
        )
        self.assertTrue(cleared.completeness.attention.complete)

    def test_fresh_semantic_unknown_degrades_lifecycle_until_known_progress(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                event(10.0, "MODEL_PROGRESS", "progress"),
                self._unknown(11.0, "lifecycle", "future-phase"),
            ],
        )

        uncertain = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=12.0)
        self.assertEqual(uncertain.lifecycle, LifecycleState.GENERATING)
        self.assertTrue(uncertain.protocol_uncertain)
        self.assertEqual(uncertain.lifecycle_confidence, Confidence.LOW)
        self.assertEqual(uncertain.phase, "协议状态不确定")

        machine.ingest("key", [event(13.0, "MODEL_PROGRESS", "recovered")])
        recovered = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=14.0)
        self.assertFalse(recovered.protocol_uncertain)
        self.assertEqual(recovered.lifecycle_confidence, Confidence.HIGH)
        self.assertEqual(recovered.phase, "模型正在生成")

    def test_unknown_attention_candidate_is_visible_in_main_phase(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                event(10.0, "MODEL_PROGRESS", "progress"),
                self._unknown(11.0, "attention", "future-approval"),
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=12.0)

        self.assertEqual(state.attention, AttentionState.NONE)
        self.assertEqual(state.attention_confidence, Confidence.LOW)
        self.assertEqual(state.phase, "协议不确定（可能等待交互）")
        self.assertEqual(state.diagnosis[0].conclusion, state.phase)

    def test_auxiliary_or_older_unknown_does_not_degrade_newer_progress(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                self._unknown(9.0, "lifecycle", "old-phase"),
                event(10.0, "MODEL_PROGRESS", "progress"),
                self._unknown(11.0, "auxiliary", "telemetry"),
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=12.0)

        self.assertFalse(state.protocol_uncertain)
        self.assertEqual(state.lifecycle_confidence, Confidence.HIGH)

    def test_action_required_is_independent_from_lifecycle(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                event(10.0, "TURN_STARTED", "turn"),
                event(11.0, "MODEL_PROGRESS", "progress"),
                NormalizedEvent(
                    12.0,
                    "ACTION_REQUIRED",
                    "等待用户操作",
                    "Allow command?",
                    "rollout",
                    Confidence.HIGH,
                    "turn-1",
                    "approval",
                    metadata={
                        "attention_state": "APPROVAL",
                        "call_id": "call-1",
                    },
                ),
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=13.0)

        self.assertEqual(state.lifecycle, LifecycleState.GENERATING)
        self.assertEqual(state.attention, AttentionState.APPROVAL)
        self.assertEqual(state.attention_request.call_id, "call-1")

    def test_later_progress_clears_action_required(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                event(10.0, "TURN_STARTED", "turn"),
                NormalizedEvent(
                    11.0,
                    "ACTION_REQUIRED",
                    "等待用户操作",
                    source_id="question",
                    metadata={"attention_state": "USER_INPUT"},
                ),
                event(12.0, "MODEL_PROGRESS", "continued"),
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=13.0)

        self.assertEqual(state.attention, AttentionState.NONE)
        self.assertIsNone(state.attention_request)

    def test_future_action_uses_observation_order_and_trusted_resolution_clears_it(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    1_300.0,
                    "ACTION_REQUIRED",
                    "等待用户操作",
                    source="rollout",
                    source_id="future-action",
                    observed_at=1_000.0,
                    metadata={"attention_state": "APPROVAL"},
                )
            ],
        )
        uncertain = machine.derive(
            "key", process(), NetworkEvidence(NetworkState.IDLE), now=1_000.0
        )
        action = machine.retained_events("key")[0]

        self.assertEqual(uncertain.attention, AttentionState.APPROVAL)
        self.assertTrue(uncertain.clock_uncertain)
        self.assertEqual(action.source_timestamp, 1_300.0)
        self.assertEqual(action.decision_timestamp, 1_000.0)
        self.assertEqual(action.presentation_timestamp, 1_300.0)

        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    1_001.0,
                    "ACTION_RESOLVED",
                    "已处理",
                    source="rollout",
                    source_id="resolution",
                    observed_at=1_001.0,
                )
            ],
        )
        resolved = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=1_002.0)

        self.assertIsNone(resolved.attention_request)
        self.assertFalse(resolved.clock_uncertain)

    def test_cross_source_future_skew_cannot_override_later_progress(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    1_100.0,
                    "ACTION_REQUIRED",
                    "等待用户操作",
                    source="compact_hook",
                    source_id="hook-action",
                    observed_at=1_000.0,
                    metadata={"attention_state": "USER_INPUT"},
                )
            ],
        )
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    1_000.5,
                    "MODEL_PROGRESS",
                    "继续生成",
                    source="rollout",
                    source_id="rollout-progress",
                    observed_at=1_001.0,
                )
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=1_002.0)

        self.assertIsNone(state.attention_request)
        self.assertTrue(state.clock_uncertain)
        self.assertEqual(state.clock_assessments[0].source, "compact_hook")

    def test_terminal_completion_and_process_exit_clear_future_attention(self) -> None:
        for clear_kind in ("TURN_COMPLETED", "PROCESS_EXITED"):
            with self.subTest(clear_kind=clear_kind):
                machine = SessionStateMachine(900)
                machine.ingest(
                    "key",
                    [
                        NormalizedEvent(
                            1_300.0,
                            "ACTION_REQUIRED",
                            "等待用户操作",
                            source="rollout",
                            source_id=f"action-{clear_kind}",
                            observed_at=1_000.0,
                            metadata={"attention_state": "APPROVAL"},
                        )
                    ],
                )
                machine.ingest(
                    "key",
                    [
                        NormalizedEvent(
                            1_001.0,
                            clear_kind,
                            "单调终止证据",
                            source="process" if clear_kind == "PROCESS_EXITED" else "rollout",
                            source_id=f"clear-{clear_kind}",
                            observed_at=1_001.0,
                        )
                    ],
                )

                state = machine.derive(
                    "key", process(), NetworkEvidence(NetworkState.IDLE), now=1_002.0
                )

                self.assertIsNone(state.attention_request)

    def test_clock_rollback_does_not_revive_failure_and_clear_resets_epoch(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    200.0,
                    "ACTION_REQUIRED",
                    "等待用户操作",
                    source="rollout",
                    source_id="action",
                    observed_at=200.0,
                    metadata={"attention_state": "APPROVAL"},
                )
            ],
        )
        for clock_event in (
            NormalizedEvent(
                150.0,
                "ACTION_RESOLVED",
                "已处理",
                source="rollout",
                source_id="rollback-resolution",
                observed_at=150.0,
            ),
            NormalizedEvent(
                151.0,
                "TURN_STARTED",
                "新时钟纪元 turn",
                source="rollout",
                source_id="post-rollback-turn",
                observed_at=201.0,
            ),
            NormalizedEvent(
                152.0,
                "MODEL_PROGRESS",
                "新时钟纪元进展",
                source="rollout",
                source_id="post-rollback-progress",
                observed_at=202.0,
            ),
            NormalizedEvent(
                100.0,
                "TURN_FAILED",
                "延迟旧失败",
                source="rollout",
                source_id="old-failure",
                observed_at=203.0,
            ),
        ):
            machine.ingest("key", [clock_event])

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=204.0)

        self.assertIsNone(state.attention_request)
        self.assertEqual(state.lifecycle, LifecycleState.GENERATING)
        self.assertTrue(state.clock_uncertain)

    def test_zero_timestamp_clear_and_sleep_resume_use_monotonic_adjudication(self) -> None:
        machine = SessionStateMachine(20_000)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    10.0,
                    "ACTION_REQUIRED",
                    "等待用户操作",
                    source="rollout",
                    source_id="action",
                    observed_at=10.0,
                    metadata={"attention_state": "USER_INPUT"},
                )
            ],
        )
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    0.0,
                    "ACTION_RESOLVED",
                    "已处理",
                    source="rollout",
                    source_id="zero-resolution",
                    observed_at=11.0,
                )
            ],
        )
        cleared = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=12.0)
        self.assertIsNone(cleared.attention_request)
        self.assertTrue(cleared.clock_uncertain)

        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    10_000.0,
                    "MODEL_PROGRESS",
                    "休眠恢复后继续",
                    source="rollout",
                    source_id="resume-progress",
                    observed_at=10_000.0,
                ),
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=10_001.0)

        self.assertIsNone(state.attention_request)
        self.assertEqual(state.lifecycle, LifecycleState.GENERATING)
        self.assertFalse(state.clock_uncertain)

    def test_each_attention_state_clears_on_terminal_or_explicit_resolution(self) -> None:
        for index, attention in enumerate(
            (
                AttentionState.APPROVAL,
                AttentionState.PERMISSIONS,
                AttentionState.USER_INPUT,
                AttentionState.MCP_ELICITATION,
                AttentionState.AUTH_ELICITATION,
            )
        ):
            with self.subTest(attention=attention):
                machine = SessionStateMachine(900)
                machine.ingest(
                    "key",
                    [
                        NormalizedEvent(
                            10.0,
                            "ACTION_REQUIRED",
                            "等待用户操作",
                            source_id=f"request-{index}",
                            metadata={"attention_state": attention.value},
                        ),
                        NormalizedEvent(
                            12.0,
                            "ACTION_RESOLVED" if index % 2 else "TURN_COMPLETED",
                            "已处理",
                            source_id=f"resolved-{index}",
                        ),
                        NormalizedEvent(
                            10.5,
                            "ACTION_REQUIRED",
                            "延迟重复",
                            source_id=f"duplicate-{index}",
                            metadata={"attention_state": attention.value},
                        ),
                    ],
                )
                state = machine.derive(
                    "key", process(), NetworkEvidence(NetworkState.IDLE), now=13.0
                )
                self.assertEqual(state.attention, AttentionState.NONE)

    def test_current_operation_prioritizes_attention_over_running_tool(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                event(10.0, "TOOL_RUNNING", "tool"),
                NormalizedEvent(
                    11.0,
                    "ACTION_REQUIRED",
                    "等待用户操作",
                    "Approve command",
                    source_id="approval",
                    metadata={"attention_state": "APPROVAL"},
                ),
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.ACTIVE), now=12.0)

        self.assertEqual(state.current_operation.category, "attention")
        self.assertEqual(state.current_operation.detail, "Approve command")

    def test_delayed_old_action_does_not_resurrect_after_resolution(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    10.0,
                    "ACTION_REQUIRED",
                    "等待用户操作",
                    source_id="approval",
                    metadata={"attention_state": "APPROVAL"},
                ),
                event(12.0, "TOOL_RUNNING", "tool"),
            ],
        )
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    10.5,
                    "ACTION_REQUIRED",
                    "等待用户操作",
                    source_id="delayed-duplicate",
                    metadata={"attention_state": "APPROVAL"},
                )
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=13.0)

        self.assertEqual(state.attention, AttentionState.NONE)

    def test_latest_rollout_model_configuration_updates_session_process(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    now - 1,
                    "MODEL_CONFIG",
                    "模型配置更新",
                    source_id="model",
                    metadata={"model": "gpt-live", "reasoning_effort": "high"},
                )
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(), now)

        self.assertEqual(state.process.model, "gpt-live")
        self.assertEqual(state.process.reasoning_effort, "high")

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

    def test_model_progress_resolves_pre_request_alert_without_request_event(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        machine.ingest("key", [event(now - 80, "TURN_STARTED", "turn")])

        alerted = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now)
        self.assertEqual(alerted.alert, "PRE_REQUEST_STALL")

        machine.ingest("key", [event(now - 5, "MODEL_PROGRESS", "progress")])
        recovered = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now)

        self.assertEqual(recovered.lifecycle, LifecycleState.GENERATING)
        self.assertEqual(recovered.alert, "")
        self.assertEqual(recovered.alerts[-1].status, AlertStatus.RESOLVED)

    def test_non_phase_events_do_not_replace_the_latest_live_phase(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        machine.ingest(
            "key",
            [
                event(now - 10, "TURN_STARTED", "start"),
                event(now - 8, "MODEL_PROGRESS", "progress"),
                event(now - 6, "TOKEN_USAGE", "tokens"),
                event(now - 4, "KEEPALIVE", "keepalive"),
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now)

        self.assertEqual(state.lifecycle, LifecycleState.GENERATING)
        self.assertEqual(state.phase, "模型正在生成")
        self.assertEqual(state.phase_since, now - 8)

    def test_progress_after_terminal_recovers_when_turn_start_is_not_loaded(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        machine.ingest(
            "key",
            [
                event(now - 20, "TURN_COMPLETED", "old-terminal"),
                event(now - 8, "REQUEST_SENT", "request"),
                event(now - 3, "RESPONSE_STARTED", "response"),
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now)

        self.assertEqual(state.lifecycle, LifecycleState.GENERATING)
        self.assertEqual(state.phase, "上游已接收请求")
        self.assertEqual(state.phase_since, now - 3)

    def test_compacting_remains_visible_during_its_model_stream(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        machine.ingest(
            "key",
            [
                event(now - 20, "TURN_STARTED", "start"),
                event(now - 18, "COMPACTING", "compact-start"),
                event(now - 16, "RESPONSE_STARTED", "compact-response"),
                event(now - 8, "MODEL_PROGRESS", "compact-progress"),
            ],
        )

        compacting = machine.derive("key", process(), NetworkEvidence(NetworkState.ACTIVE), now)

        self.assertEqual(compacting.lifecycle, LifecycleState.COMPACTING)
        self.assertEqual(compacting.phase, "正在压缩上下文")
        self.assertEqual(compacting.phase_since, now - 18)

        machine.ingest(
            "key",
            [
                event(now - 4, "COMPACT_COMPLETED", "compact-done"),
                event(now - 2, "MODEL_PROGRESS", "normal-progress"),
            ],
        )
        resumed = machine.derive("key", process(), NetworkEvidence(NetworkState.ACTIVE), now)

        self.assertEqual(resumed.lifecycle, LifecycleState.GENERATING)
        self.assertEqual(resumed.phase, "模型正在生成")

    def test_duplicate_compaction_sources_count_as_one_transition(self) -> None:
        machine = SessionStateMachine(900)
        now = time.time()
        machine.ingest(
            "key",
            [
                event(now - 20, "TURN_STARTED", "start"),
                event(now - 18, "COMPACTING", "compact-log"),
                event(now - 17.8, "COMPACTING", "compact-log-duplicate"),
                event(now - 2, "COMPACT_COMPLETED", "compact-sse"),
                event(now - 1.9, "COMPACT_COMPLETED", "compact-rollout"),
            ],
        )

        retained = machine.retained_events("key")

        self.assertEqual(sum(item.kind == "COMPACTING" for item in retained), 1)
        self.assertEqual(sum(item.kind == "COMPACT_COMPLETED" for item in retained), 1)

    def test_compaction_summary_survives_event_retention_trim(self) -> None:
        machine = SessionStateMachine(10_000)
        now = time.time()
        compact_start = NormalizedEvent(
            now - 900,
            "COMPACTING",
            "正在压缩上下文",
            source_id="compact-start",
            turn_id="compact-turn",
            metadata={
                "trigger": "manual",
                "context_tokens": 216_402,
                "context_window": 353_400,
            },
        )
        compact_done = NormalizedEvent(
            now - 840,
            "COMPACT_COMPLETED",
            "上下文压缩完成",
            source_id="compact-done",
            turn_id="compact-turn",
            metadata={"trigger": "manual"},
        )
        noise = [
            event(now - 800 + index, "MODEL_PROGRESS", f"noise-{index}") for index in range(501)
        ]

        machine.ingest("key", [compact_start, compact_done, *noise])
        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now)

        self.assertFalse(
            any(item.kind.startswith("COMPACT") for item in machine.retained_events("key"))
        )
        self.assertEqual(len(state.compactions), 1)
        self.assertEqual(state.compactions[0].trigger, "manual")
        self.assertEqual(state.compactions[0].started_at, now - 900)
        self.assertEqual(state.compactions[0].completed_at, now - 840)
        self.assertEqual(state.compactions[0].context_tokens, 216_402)

    def test_event_retention_is_exactly_500(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [event(float(index), "MODEL_PROGRESS", str(index)) for index in range(550)],
        )
        self.assertEqual(len(machine.events["key"]), 500)
        self.assertEqual(machine.events["key"][0].source_id, "50")

    def test_bounded_dedupe_filter_never_forgets_inserted_identity(self) -> None:
        dedupe = BoundedDedupeFilter(bit_count=64)
        dedupe.add("old-source")
        for index in range(10_000):
            dedupe.add(f"source-{index}")

        self.assertIn("old-source", dedupe)
        self.assertLessEqual(len(dedupe), 64)
        self.assertTrue(dedupe.degraded)

    def test_state_private_dedupe_and_clock_domains_stay_bounded_under_churn(self) -> None:
        machine = SessionStateMachine(900, dedupe_filter_bits=64)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    float(index + 1),
                    "MODEL_PROGRESS",
                    "progress",
                    source=f"arbitrary-source-{index}",
                    source_id=f"source-{index}",
                    observed_at=float(index + 1),
                )
                for index in range(2_000)
            ],
        )
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    1.0,
                    "MODEL_PROGRESS",
                    "replayed",
                    source="arbitrary-source-0",
                    source_id="source-0",
                    observed_at=3_000.0,
                )
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=3_000.0)
        self.assertLessEqual(len(machine.seen["key"]), 64)
        self.assertEqual(set(machine.clock_state["key"]), {"external_wall_clock"})
        self.assertLessEqual(
            sum(event.source_id == "source-0" for event in machine.events["key"]), 1
        )
        self.assertTrue(state.event_telemetry.dedupe_filter_degraded)
        self.assertGreater(state.event_telemetry.dedupe_filter_matches, 0)
        self.assertTrue(
            any(finding.conclusion == "事件去重记忆接近饱和" for finding in state.diagnosis)
        )

    def test_stale_mutable_stream_generation_cannot_reopen_completed_compact(self) -> None:
        machine = SessionStateMachine(900)
        path_hash = "a" * 64

        def stream_event(
            generation: int,
            kind: str,
            timestamp: float,
            source_id: str,
        ) -> NormalizedEvent:
            return NormalizedEvent(
                timestamp,
                kind,
                kind,
                source="compact_hook",
                source_id=source_id,
                observed_at=timestamp + 0.1,
                metadata={
                    "stream_path_sha256": path_hash,
                    "stream_generation": generation,
                },
            )

        machine.ingest(
            "key",
            [
                stream_event(1, "COMPACTING", 10.0, "generation-1-start"),
                stream_event(1, "COMPACT_COMPLETED", 11.0, "generation-1-complete"),
            ],
        )
        machine.ingest(
            "key",
            [stream_event(0, "COMPACTING", 12.0, "delayed-generation-0")],
        )
        machine.ingest(
            "key",
            [stream_event(2, "COMPACTING", 11.0, "generation-2-same-time")],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=13.0)
        self.assertNotIn("delayed-generation-0", {item.source_id for item in machine.events["key"]})
        self.assertIn("generation-2-same-time", {item.source_id for item in machine.events["key"]})
        self.assertEqual(state.compactions[-1].status, "completed")
        self.assertEqual(state.event_telemetry.stale_stream_generation_dropped, 1)
        self.assertEqual(state.event_telemetry.stream_generation_advances, 1)
        self.assertTrue(
            any(finding.conclusion == "可变证据流 generation 已降级" for finding in state.diagnosis)
        )

    def test_mutable_stream_identity_guard_has_fixed_cardinality(self) -> None:
        machine = SessionStateMachine(900)
        events = [
            NormalizedEvent(
                float(index + 1),
                "MODEL_PROGRESS",
                "progress",
                source="rollout",
                source_id=f"stream-{index}",
                observed_at=float(index + 1),
                metadata={
                    "stream_path_sha256": f"{index:064x}",
                    "stream_generation": 0,
                },
            )
            for index in range(40)
        ]
        machine.ingest("key", events)

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=50.0)
        self.assertEqual(len(machine.stream_generations["key"]), 32)
        self.assertEqual(state.event_telemetry.stream_identity_limit_dropped, 8)

    def test_event_telemetry_reports_observation_latency_and_unknown_rate(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    10.0,
                    "MODEL_PROGRESS",
                    "progress",
                    source_id="known",
                    observed_at=10.1,
                ),
                NormalizedEvent(
                    11.0,
                    "UNPARSED_PAYLOAD",
                    "unknown",
                    source_id="unknown",
                    observed_at=11.3,
                ),
            ],
        )

        state = machine.derive("key", process(), NetworkEvidence(NetworkState.IDLE), now=12.0)

        self.assertEqual(state.event_telemetry.total_events, 2)
        self.assertEqual(state.event_telemetry.unparsed_events, 1)
        self.assertEqual(state.event_telemetry.unknown_rate, 0.5)
        self.assertAlmostEqual(state.event_telemetry.observation_p50_seconds, 0.2)


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
            " cubic bytes_sent:12 bytes_acked:10\n"
            " bytes_received:20 lastsnd:50 lastrcv:40 retrans:1/2 rtt:4.5\n"
        )
        socket = parse_ss_output(text, {42})[42][0]
        self.assertEqual(socket.bytes_sent, 12)
        self.assertEqual(socket.bytes_received, 20)
        self.assertEqual(socket.retrans_total, 2)
        self.assertEqual(socket.rtt_ms, 4.5)


if __name__ == "__main__":
    unittest.main()
