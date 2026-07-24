from __future__ import annotations

from pathlib import Path
import unittest

from models import (
    CodexPaths,
    CollectorHealth,
    InstanceSnapshot,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
    TerminalCapability,
    TerminalSessionSummary,
)
from temporal import apply_temporal_completeness, build_temporal_cut


def instance(observed_at: float) -> InstanceSnapshot:
    home = Path("/tmp/CODEX_HOME_A")
    paths = CodexPaths(
        home,
        home,
        home / "state.sqlite",
        home / "logs.sqlite",
        home / "session_index.jsonl",
        home / "sessions",
    )
    value = InstanceSnapshot("INSTANCE", paths, "HOME", "HOME", "fixture")
    value.rollout_activity = [{"observed_at": observed_at}]
    return value


class TemporalCutTests(unittest.TestCase):
    def test_fast_refresh_preserves_full_source_times_and_updates_rollout_generation(self) -> None:
        collectors = [
            CollectorHealth("process", last_success_at=100.0),
            CollectorHealth("socket", last_success_at=101.0),
            CollectorHealth("state_db:INSTANCE", last_success_at=100.5),
        ]
        previous = build_temporal_cut(
            [instance(101.0)],
            collectors,
            now=101.0,
            interval=2.0,
            generation=1,
        )
        refreshed = build_temporal_cut(
            [instance(106.0)],
            collectors,
            now=106.0,
            interval=2.0,
            generation=2,
            previous=previous,
            fast=True,
        )
        sources = {item.source: item for item in refreshed.sources}

        self.assertEqual(sources["process"].observed_to, 100.0)
        self.assertEqual(sources["process"].sample_generation, 1)
        self.assertEqual(sources["socket"].observed_to, 101.0)
        self.assertEqual(sources["rollout"].observed_to, 106.0)
        self.assertEqual(sources["rollout"].sample_generation, 2)
        self.assertFalse(refreshed.coherent)
        self.assertEqual(refreshed.reason, "source_observation_windows_disjoint")

    def test_full_sample_recovery_restores_coherent_window(self) -> None:
        collectors = [
            CollectorHealth("process", last_success_at=110.0),
            CollectorHealth("socket", last_success_at=110.2),
            CollectorHealth("state_db:INSTANCE", last_success_at=110.1),
        ]
        recovered = build_temporal_cut(
            [instance(110.3)],
            collectors,
            now=110.4,
            interval=2.0,
            generation=3,
        )
        self.assertTrue(recovered.coherent)
        self.assertLess(recovered.actual_source_skew_seconds, 0.5)
        self.assertEqual({item.sample_generation for item in recovered.sources}, {3})

    def test_disjoint_sources_downgrade_all_cross_source_axes(self) -> None:
        value = instance(106.0)
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
            instance_id="INSTANCE",
            session_id="SESSION",
        )
        value.sessions = [SessionHealth("INSTANCE", "SESSION", process)]
        cut = build_temporal_cut(
            [value],
            [CollectorHealth("process", last_success_at=100.0)],
            now=106.0,
            interval=2.0,
            generation=2,
        )

        updated = apply_temporal_completeness([value], cut)

        self.assertFalse(cut.coherent)
        self.assertEqual(
            set(updated[0].sessions[0].completeness.incomplete_axes),
            {"lifecycle", "attention", "failure_recovery", "terminal_ownership", "network", "silence"},
        )
        self.assertTrue(
            all(
                axis.baseline_kind == "temporal_cut"
                for axis in (
                    updated[0].sessions[0].completeness.lifecycle,
                    updated[0].sessions[0].completeness.attention,
                    updated[0].sessions[0].completeness.failure_recovery,
                    updated[0].sessions[0].completeness.terminal_ownership,
                    updated[0].sessions[0].completeness.network,
                    updated[0].sessions[0].completeness.silence,
                )
            )
        )

    def test_terminal_observation_is_independent_and_pid_reuse_identity_is_monotonic(self) -> None:
        value = instance(20.0)
        terminal = TerminalSessionSummary(
            "TERMINAL",
            process_id="PROCESS",
            status="running",
            process_active=True,
            capability=TerminalCapability.POLL_TRANSCRIPT,
            last_output_at=21.0,
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
            instance_id="INSTANCE",
            session_id="SESSION",
        )
        session = SessionHealth("INSTANCE", "SESSION", process)
        session.terminal_sessions = [terminal]
        value.sessions = [session]
        self.assertNotEqual(ProcessIdentity(42, 100), ProcessIdentity(42, 101))
        cut = build_temporal_cut(
            [value],
            [CollectorHealth("process", last_success_at=20.0)],
            now=21.0,
            interval=2.0,
            generation=1,
        )
        self.assertEqual(cut.kind, "composite_interval")
        terminal_source = next(item for item in cut.sources if item.source == "terminal")
        self.assertEqual(terminal_source.observed_to, 21.0)


if __name__ == "__main__":
    unittest.main()
