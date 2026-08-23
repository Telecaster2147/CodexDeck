from __future__ import annotations

import unittest
from pathlib import Path

from codexdeck.models import (
    CodexPaths,
    Confidence,
    FailureInfo,
    InstanceSnapshot,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ObservationPulse,
    ProcessIdentity,
    ProcessInfo,
    ProcessTreeActivity,
)
from codexdeck.presentation.export import session_export
from codexdeck.presentation.json_output import snapshot_dict
from codexdeck.state_machine import SessionStateMachine


def process() -> ProcessInfo:
    return ProcessInfo(
        ProcessIdentity(42, 100),
        1,
        "codex",
        1,
        0.0,
        "S",
        "wait",
        "codex",
        "session",
        instance_id="INSTANCE_ID",
        session_id="SESSION_ID",
    )


def event(timestamp: float, kind: str, **values: object) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp,
        kind,
        kind,
        source="rollout",
        source_id=f"{kind}:{timestamp}",
        **values,
    )


class ReasonProjectionTests(unittest.TestCase):
    def test_attention_reason_is_shared_by_diagnosis_json_and_export(self) -> None:
        machine = SessionStateMachine(900)
        records = [
            event(10, "MODEL_PROGRESS"),
            event(11, "ACTION_REQUIRED", metadata={"attention_state": "APPROVAL"}),
        ]
        machine.ingest("SESSION_ID", records)
        session = machine.derive(
            "SESSION_ID", process(), NetworkEvidence(NetworkState.IDLE), now=12
        )
        code = "attention.approval_pending"

        home = Path("/CODEX_HOME_A")
        paths = CodexPaths(
            home,
            home,
            home / "state.sqlite",
            home / "logs.sqlite",
            home / "session_index.jsonl",
            home / "sessions",
        )
        snapshot = MonitorSnapshot(
            "2026-08-23T00:00:00+08:00",
            2.0,
            instances=[
                InstanceSnapshot(
                    "INSTANCE_ID",
                    paths,
                    "CODEX_HOME_A",
                    "SQLITE_HOME_A",
                    "fixture",
                    sessions=[session],
                )
            ],
        )
        json_session = snapshot_dict(snapshot)["instances"][0]["sessions"][0]  # type: ignore[index]
        exported = session_export(session, records, generated_at="2026-08-23T00:00:00+08:00")

        self.assertIn(code, {reason.reason_code for reason in session.reasons})
        self.assertIn(code, {finding.reason for finding in session.diagnosis})
        self.assertIn(code, {reason["reason_code"] for reason in json_session["reasons"]})
        self.assertIn(
            code,
            {reason["reason_code"] for reason in exported["session"]["reasons"]},
        )

    def test_critical_states_have_stable_versioned_reason_codes(self) -> None:
        cases: list[tuple[str, list[NormalizedEvent], NetworkEvidence, ObservationPulse, float]] = [
            (
                "failure.current_turn",
                [
                    event(
                        10,
                        "TURN_FAILED",
                        failure=FailureInfo("upstream", "failed", timestamp=10),
                    )
                ],
                NetworkEvidence(NetworkState.IDLE),
                ObservationPulse(),
                11,
            ),
            (
                "silence.no_progress_with_live_process",
                [event(10, "MODEL_PROGRESS")],
                NetworkEvidence(NetworkState.IDLE),
                ObservationPulse(
                    quiet_full_samples=2,
                    process_activity=ProcessTreeActivity(available=True),
                ),
                140,
            ),
            (
                "silence.observer_blind",
                [event(10, "MODEL_PROGRESS")],
                NetworkEvidence(NetworkState.IDLE),
                ObservationPulse(
                    collector_stale=True,
                    collector_stale_reason="process probe stale",
                    process_activity=ProcessTreeActivity(available=True),
                ),
                140,
            ),
            (
                "unknown.unsupported_protocol_family",
                [
                    event(
                        10,
                        "UNPARSED_PAYLOAD",
                        confidence=Confidence.LOW,
                        complete=False,
                        metadata={"semantic_scope": "lifecycle"},
                    )
                ],
                NetworkEvidence(NetworkState.IDLE),
                ObservationPulse(),
                11,
            ),
            (
                "network.two_window_no_progress",
                [event(10, "REQUEST_SENT")],
                NetworkEvidence(NetworkState.STALLED, "two windows"),
                ObservationPulse(process_activity=ProcessTreeActivity(available=True)),
                11,
            ),
        ]
        for expected, records, network, pulse, now in cases:
            with self.subTest(reason=expected):
                machine = SessionStateMachine(900)
                machine.ingest("SESSION_ID", records)
                session = machine.derive(
                    "SESSION_ID", process(), network, now=now, observation=pulse
                )
                matching = [reason for reason in session.reasons if reason.reason_code == expected]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0].schema_version, 1)
                self.assertTrue(matching[0].supporting_evidence)

    def test_evidence_timeline_is_bounded_deterministic_and_transcript_free(self) -> None:
        machine = SessionStateMachine(900)
        records = [
            event(index, "MODEL_PROGRESS", detail="TRANSCRIPT_BODY")
            for index in range(1, 80)
        ]
        machine.ingest("SESSION_ID", records)

        first = machine.derive(
            "SESSION_ID", process(), NetworkEvidence(NetworkState.ACTIVE), now=100
        )
        second = machine.derive(
            "SESSION_ID", process(), NetworkEvidence(NetworkState.ACTIVE), now=100
        )

        self.assertEqual(first.evidence_timeline, second.evidence_timeline)
        self.assertEqual(len(first.evidence_timeline), 64)
        rendered = repr(first.evidence_timeline)
        self.assertNotIn("TRANSCRIPT_BODY", rendered)
        self.assertTrue(
            all(entry.reason_code == "lifecycle.protocol_generating" for entry in first.evidence_timeline)
        )


if __name__ == "__main__":
    unittest.main()
