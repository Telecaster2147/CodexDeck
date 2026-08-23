from __future__ import annotations

import time
import unittest
from pathlib import Path

from codexdeck.diagnostics import CollectorTracker
from codexdeck.models import (
    CodexPaths,
    DiscoverySummary,
    InstanceSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
)
from codexdeck.snapshot_publisher import SnapshotPublisher


def mutable_snapshot_input() -> tuple[InstanceSnapshot, ProcessInfo, NormalizedEvent]:
    home = Path("/CODEX_HOME_A")
    paths = CodexPaths(
        home,
        home,
        home / "state.sqlite",
        home / "logs.sqlite",
        home / "session_index.jsonl",
        home / "sessions",
    )
    process = ProcessInfo(
        ProcessIdentity(42, 100),
        1,
        "codex",
        1,
        0.0,
        "S",
        "wait",
        "codex",
        "session",
        cwd="/workspace-a",
        instance_id="INSTANCE_ID",
        session_id="SESSION_ID",
    )
    event = NormalizedEvent(
        1.0,
        "MODEL_PROGRESS",
        "progress",
        metadata={"nested": {"count": 1}},
    )
    session = SessionHealth(
        "INSTANCE_ID",
        "SESSION_ID",
        process,
        network=NetworkEvidence(NetworkState.ACTIVE),
        events=[event],
    )
    instance = InstanceSnapshot(
        "INSTANCE_ID",
        paths,
        "CODEX_HOME_A",
        "SQLITE_HOME_A",
        "fixture",
        unknown_event_types={"future": 1},
        rollout_activity=[{"observed_at": 1.0}],
        processes=[process],
        sessions=[session],
    )
    return instance, process, event


class SnapshotPublicationTests(unittest.TestCase):
    def publish(self, instance: InstanceSnapshot):
        return SnapshotPublisher(2.0, CollectorTracker(2.0)).publish(
            instances=[instance],
            started=time.monotonic(),
            now_monotonic=time.monotonic(),
            diagnostics=[],
            discovery=DiscoverySummary(),
            discovery_stale_since=None,
            socket_stale_since=None,
        )

    def test_publication_freezes_outer_and_nested_values(self) -> None:
        instance, _, _ = mutable_snapshot_input()
        snapshot = self.publish(instance)
        session = snapshot.sessions[0]

        self.assertIsInstance(snapshot.instances, tuple)
        self.assertIsInstance(snapshot.sessions, tuple)
        self.assertIsInstance(snapshot.instances[0].sessions, tuple)
        self.assertIsInstance(session.events, tuple)
        self.assertIsInstance(session.network.connections, tuple)

        with self.assertRaises(TypeError):
            snapshot.generated_at = "changed"
        with self.assertRaises(TypeError):
            snapshot.instances[0].display_codex_home = "changed"
        with self.assertRaises(TypeError):
            session.phase = "changed"
        with self.assertRaises(TypeError):
            session.process.cwd = "/changed"
        with self.assertRaises(TypeError):
            session.network.state = NetworkState.CLOSED
        with self.assertRaises(TypeError):
            snapshot.instances[0].unknown_event_types["other"] = 2
        with self.assertRaises(TypeError):
            session.events[0].metadata["other"] = 2

    def test_publication_detaches_collector_builders_from_old_snapshot(self) -> None:
        instance, process, event = mutable_snapshot_input()
        snapshot = self.publish(instance)

        process.cwd = "/workspace-b"
        event.metadata["nested"]["count"] = 2
        instance.sessions.clear()
        instance.unknown_event_types["future"] = 9
        instance.rollout_activity[0]["observed_at"] = 99.0

        published = snapshot.instances[0]
        self.assertEqual(snapshot.sessions[0].process.cwd, "/workspace-a")
        self.assertEqual(snapshot.sessions[0].events[0].metadata["nested"]["count"], 1)
        self.assertEqual(len(published.sessions), 1)
        self.assertEqual(published.unknown_event_types["future"], 1)
        self.assertEqual(published.rollout_activity[0]["observed_at"], 1.0)


if __name__ == "__main__":
    unittest.main()
