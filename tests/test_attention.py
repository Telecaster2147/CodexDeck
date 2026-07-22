from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    AttentionRequest,
    AttentionState,
    FailureInfo,
    LifecycleState,
    NetworkEvidence,
    NetworkState,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
    SilenceAssessment,
    SilenceState,
)
from presentation.attention import attention_queue  # noqa: E402


def session(session_id: str) -> SessionHealth:
    process = ProcessInfo(
        ProcessIdentity(len(session_id), 100),
        1,
        "codex",
        1,
        0.0,
        "S",
        "futex",
        "codex",
        "session",
        cwd=f"/workspace-{session_id}",
        instance_id="INSTANCE_ID",
        session_id=session_id,
    )
    return SessionHealth("INSTANCE_ID", session_id, process)


class AttentionQueueTests(unittest.TestCase):
    def test_queue_prioritizes_direct_interaction_over_other_anomalies(self) -> None:
        generating = session("generating")
        generating.lifecycle = LifecycleState.GENERATING
        approval = session("approval")
        approval.attention_request = AttentionRequest(
            AttentionState.APPROVAL,
            summary="等待审批",
            detail="Approve command",
            started_at=10.0,
        )
        background = session("background")
        recovered = session("recovered")
        stalled = session("stalled")
        stalled.network = NetworkEvidence(NetworkState.STALLED, "two windows")
        blind = session("blind")
        blind.lifecycle = LifecycleState.RUNNING_TOOL
        blind.silence = SilenceAssessment(
            SilenceState.OBSERVER_BLIND,
            "collector stale",
            silence_started_at=12.0,
        )

        queue = attention_queue(
            (generating, approval, background, recovered, stalled, blind)
        )

        self.assertEqual([item.category for item in queue], [
            "approval",
            "network_stall",
            "observer_blind",
        ])
        self.assertEqual(queue[0].detail, "Approve command")
        self.assertEqual(queue[0].session.session_id, "approval")

    def test_resolved_or_inactive_blind_sessions_are_absent(self) -> None:
        failed = session("failed")
        failed.current_failure = FailureInfo("request", "failed", timestamp=10.0)
        inactive_blind = session("blind")
        inactive_blind.lifecycle = LifecycleState.IDLE
        inactive_blind.silence = SilenceAssessment(
            SilenceState.OBSERVER_BLIND,
            "collector stale",
        )

        self.assertEqual(
            [item.category for item in attention_queue((failed, inactive_blind))],
            ["failure"],
        )


if __name__ == "__main__":
    unittest.main()
