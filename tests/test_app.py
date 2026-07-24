from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from app import (  # noqa: E402
    AppOptions,
    _validate_explicit_filters,
    exit_code,
)
from models import (  # noqa: E402
    AxisCompleteness,
    CodexPaths,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
    SessionCompleteness,
    ObserverHealth,
)


def options(*, homes: set[Path] | None = None) -> AppOptions:
    return AppOptions(2.0, 30.0, 900, None, homes, True, False, True, False, False)


def instance(home: Path) -> InstanceSnapshot:
    paths = CodexPaths(
        home,
        home,
        home / "state_5.sqlite",
        home / "logs_2.sqlite",
        home / "session_index.jsonl",
        home / "sessions",
    )
    return InstanceSnapshot("instance", paths, str(home), str(home), "environment")


class AppTests(unittest.TestCase):
    def test_partial_home_filter_match_reports_missing_home(self) -> None:
        found = Path("/tmp/codex-found")
        missing = Path("/tmp/codex-missing")
        snapshot = MonitorSnapshot("now", 2.0, [instance(found)])
        with self.assertRaisesRegex(RuntimeError, str(missing)):
            _validate_explicit_filters(options(homes={found, missing}), snapshot)

    def test_terminal_failure_precedes_network_stall(self) -> None:
        process = ProcessInfo(
            ProcessIdentity(10, 20),
            1,
            "codex",
            1,
            0.0,
            "S",
            "futex",
            "codex",
            "session",
            instance_id="instance",
            session_id="session",
        )
        health = SessionHealth(
            "instance",
            "session",
            process,
            LifecycleState.FAILED,
            network=NetworkEvidence(NetworkState.STALLED),
        )
        snapshot = MonitorSnapshot("now", 2.0, [instance(Path("/tmp/home"))])
        snapshot.instances[0].sessions.append(health)
        self.assertEqual(exit_code(snapshot), 3)

    def test_incomplete_state_axis_uses_degraded_exit(self) -> None:
        process = ProcessInfo(
            ProcessIdentity(10, 20),
            1,
            "codex",
            1,
            0.0,
            "S",
            "futex",
            "codex",
            "session",
            instance_id="instance",
            session_id="session",
        )
        health = SessionHealth(
            "instance",
            "session",
            process,
            completeness=SessionCompleteness(
                attention=AxisCompleteness("attention", complete=False)
            ),
        )
        snapshot = MonitorSnapshot("now", 2.0, [instance(Path("/tmp/home"))])
        snapshot.instances[0].sessions.append(health)
        self.assertEqual(exit_code(snapshot), 2)
        self.assertEqual(exit_code(snapshot, strict_observation=True), 5)

    def test_strict_observation_has_distinct_code_and_workload_priority(self) -> None:
        snapshot = MonitorSnapshot(
            "now",
            2.0,
            [instance(Path("/tmp/home"))],
            observer=ObserverHealth(degraded=True, reason="snapshot_age_exceeded"),
        )
        self.assertEqual(exit_code(snapshot), 0)
        self.assertEqual(exit_code(snapshot, strict_observation=True), 5)

        process = ProcessInfo(
            ProcessIdentity(10, 20),
            1,
            "codex",
            1,
            0.0,
            "S",
            "futex",
            "codex",
            "session",
            instance_id="instance",
            session_id="session",
        )
        snapshot.instances[0].sessions.append(
            SessionHealth("instance", "session", process, LifecycleState.FAILED)
        )
        self.assertEqual(exit_code(snapshot, strict_observation=True), 3)


if __name__ == "__main__":
    unittest.main()
