from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    AlertStatus,
    Confidence,
    ConnectionAssessment,
    FailureInfo,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
    TokenUsageSummary,
)
from presentation.export import (  # noqa: E402
    current_incidents_export,
    render_export_json,
    session_export,
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
        "codex --api-key=process-secret",
        "session",
        instance_id="home-1",
        session_id="session-1",
    )


def event(
    timestamp: float,
    kind: str,
    source_id: str,
    *,
    detail: str = "",
    metadata: dict[str, object] | None = None,
    failure: FailureInfo | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp,
        kind,
        kind,
        detail,
        "test",
        Confidence.HIGH,
        "turn-1",
        source_id,
        failure,
        metadata=metadata or {},
    )


class AlertLifecycleTests(unittest.TestCase):
    def test_occurrence_is_stable_escalates_acknowledges_and_resolves(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest("key", [event(100, "TURN_STARTED", "start")])

        opened = machine.derive("key", process(), NetworkEvidence(), 161)
        self.assertEqual(len(opened.alerts), 1)
        alert_id = opened.alerts[0].id
        self.assertEqual(opened.alerts[0].status, AlertStatus.OPENED)

        repeated = machine.derive("key", process(), NetworkEvidence(), 170)
        self.assertEqual([item.id for item in repeated.alerts], [alert_id])

        escalated = machine.derive("key", process(), NetworkEvidence(), 281)
        self.assertEqual(escalated.alerts[0].status, AlertStatus.ESCALATED)
        self.assertEqual(escalated.alerts[0].escalated_at, 281)
        self.assertTrue(machine.acknowledge_alert("key", alert_id, 282))
        self.assertFalse(machine.acknowledge_alert("key", "missing", 282))

        acknowledged = machine.derive("key", process(), NetworkEvidence(), 282.5)
        self.assertEqual(acknowledged.alerts[0].status, AlertStatus.ACKNOWLEDGED)
        machine.ingest(
            "key",
            [
                event(283, "REQUEST_SENT", "request"),
                event(284, "RESPONSE_STARTED", "response"),
                event(285, "MODEL_PROGRESS", "progress"),
            ],
        )
        resolved = machine.derive("key", process(), NetworkEvidence(), 286)
        occurrence = resolved.alerts[0]
        self.assertEqual(occurrence.status, AlertStatus.RESOLVED)
        self.assertEqual(occurrence.resolved_at, 286)
        self.assertEqual(
            [item.status for item in occurrence.transitions],
            [
                AlertStatus.OPENED,
                AlertStatus.ESCALATED,
                AlertStatus.ACKNOWLEDGED,
                AlertStatus.RESOLVED,
            ],
        )

    def test_repeated_condition_after_recovery_opens_new_occurrence(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest("key", [event(100, "TURN_STARTED", "start-1")])
        first = machine.derive("key", process(), NetworkEvidence(), 161).alerts[0]
        machine.ingest(
            "key",
            [event(162, "REQUEST_SENT", "request"), event(163, "MODEL_PROGRESS", "progress")],
        )
        machine.derive("key", process(), NetworkEvidence(), 164)
        machine.ingest(
            "key",
            [
                NormalizedEvent(
                    200,
                    "TURN_STARTED",
                    "TURN_STARTED",
                    source="test",
                    turn_id="turn-2",
                    source_id="start-2",
                )
            ],
        )
        latest = machine.derive("key", process(), NetworkEvidence(), 261)
        self.assertEqual(len(latest.alerts), 2)
        self.assertNotEqual(first.id, latest.alerts[-1].id)


class ExportTests(unittest.TestCase):
    def test_attention_is_exported_as_current_incident(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest(
            "attention",
            [
                event(100, "TURN_STARTED", "start"),
                event(
                    101,
                    "ACTION_REQUIRED",
                    "approval",
                    detail="Approve command",
                    metadata={"attention_state": "APPROVAL", "call_id": "call-1"},
                ),
            ],
        )
        session = machine.derive("attention", process(), NetworkEvidence(), 102)

        payload = current_incidents_export([session])
        self.assertEqual(
            set(payload),
            {
                "export_schema_version",
                "export_type",
                "generated_at",
                "incident_count",
                "incidents",
            },
        )

        self.assertEqual(payload["incident_count"], 1)
        self.assertEqual(payload["incidents"][0]["attention"], "APPROVAL")
        self.assertEqual(
            payload["incidents"][0]["attention_request"]["call_id"], "call-1"
        )

    def test_session_export_uses_full_retention_and_redacts_nested_secrets(self) -> None:
        machine = SessionStateMachine(10)
        failure = FailureInfo(
            "upstream",
            "request failed token=event-secret",
            "api_key=detail-secret",
            "turn-1",
            3,
            "test",
        )
        machine.ingest(
            "key",
            [
                event(1, "TURN_STARTED", "1", metadata={"api_key": "metadata-secret"}),
                event(2, "RECONNECTING", "2", detail="Bearer bearer-secret"),
                event(3, "TURN_FAILED", "3", failure=failure),
            ],
        )
        network = NetworkEvidence(
            NetworkState.STALLED,
            "send queue is not draining",
            connections=[
                ConnectionAssessment(
                    "connection",
                    "ESTAB",
                    "127.0.0.1:1000",
                    "203.0.113.1:443",
                    "external",
                    0,
                    42,
                    0,
                    0,
                    0,
                    1,
                    30.0,
                    NetworkState.STALLED,
                    "stalled",
                )
            ],
        )
        session = machine.derive("key", process(), network, 100)
        session.token_usage = TokenUsageSummary(total_tokens=123, context_tokens=100)
        self.assertEqual(session.events, [])

        payload = session_export(
            session,
            machine.retained_events("key"),
            generated_at="2026-07-16T00:00:00Z",
        )
        self.assertEqual(
            set(payload),
            {
                "export_schema_version",
                "export_type",
                "generated_at",
                "incident_summary",
                "session",
                "turns",
                "compactions",
                "tool_executions",
                "agents",
                "retry_recovery",
                "failures",
                "tcp_evidence",
                "events",
                "retention",
            },
        )
        self.assertIn("observation", payload["session"])
        self.assertIn("silence", payload["session"])
        self.assertIn("compactions", payload)
        self.assertEqual(payload["export_schema_version"], 2)
        self.assertEqual(payload["incident_summary"]["first_abnormal_at"], 2)
        self.assertFalse(payload["incident_summary"]["recovered"])
        self.assertEqual(
            payload["incident_summary"]["last_reliable_evidence"]["event"],
            "TURN_FAILED",
        )
        self.assertEqual(
            payload["incident_summary"]["current_axes"]["network"], "STALLED"
        )
        self.assertEqual(payload["retention"]["event_count"], 3)
        self.assertEqual(len(payload["events"]), 3)
        self.assertEqual(payload["events"][0]["provenance"]["source"], "test")
        self.assertEqual(payload["events"][0]["metadata"]["api_key"], "[REDACTED]")
        self.assertEqual(payload["session"]["token_usage"]["total_tokens"], 123)
        self.assertEqual(payload["session"]["token_usage"]["context_tokens"], 100)
        self.assertEqual(payload["retry_recovery"][0]["kind"], "RECONNECTING")
        self.assertEqual(payload["tcp_evidence"]["connections"][0]["send_q"], 42)
        self.assertIn("provenance", payload["turns"][0])
        encoded = render_export_json(payload)
        self.assertNotIn("event-secret", encoded)
        self.assertNotIn("detail-secret", encoded)
        self.assertNotIn("metadata-secret", encoded)
        self.assertNotIn("process-secret", encoded)
        self.assertEqual(json.loads(encoded)["export_type"], "session_review")

    def test_current_incidents_contains_only_unresolved_sessions(self) -> None:
        machine = SessionStateMachine(900)
        machine.ingest("active", [event(100, "TURN_STARTED", "active-start")])
        active = machine.derive("active", process(), NetworkEvidence(), 161)
        quiet = machine.derive("quiet", process(), NetworkEvidence(), 161)

        payload = current_incidents_export(
            [active, quiet], generated_at="2026-07-16T00:00:00Z"
        )
        self.assertEqual(payload["incident_count"], 1)
        self.assertEqual(payload["incidents"][0]["alerts"][0]["status"], "OPENED")
        self.assertIn("incident_summary", payload["incidents"][0])


if __name__ == "__main__":
    unittest.main()
