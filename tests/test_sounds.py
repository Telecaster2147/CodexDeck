from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    AttentionRequest,
    AttentionState,
    CodexPaths,
    InstanceSnapshot,
    MonitorSnapshot,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
    TurnSummary,
)
from presentation.tui.sounds import SoundScheduler  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def set(self, value: float) -> None:
        self.now = value


def snapshot(
    *,
    attention: bool = False,
    turns: list[TurnSummary] | None = None,
    exited: bool = False,
) -> MonitorSnapshot:
    home = Path("/tmp/home")
    process = ProcessInfo(
        ProcessIdentity(10, 20),
        1,
        "codex",
        1,
        0.0,
        "S",
        "wait",
        "codex",
        "session",
        instance_id="instance",
        session_id="session",
    )
    session = SessionHealth(
        "instance",
        "session",
        process,
        process_exited=exited,
        turns=turns or [],
    )
    if attention:
        session.attention_request = AttentionRequest(
            AttentionState.USER_INPUT,
            request_id="request",
        )
    instance = InstanceSnapshot(
        "instance",
        CodexPaths(
            home,
            home,
            home / "state.sqlite",
            home / "logs.sqlite",
            home / "index.jsonl",
            home / "sessions",
        ),
        str(home),
        str(home),
        "environment",
        sessions=[session],
    )
    return MonitorSnapshot("now", 2.0, [instance])


class SoundSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.bells: list[float] = []
        self.scheduler = SoundScheduler(
            lambda: self.bells.append(self.clock()),
            clock=self.clock,
            enabled=True,
            attention_enabled=True,
            completion_enabled=True,
        )

    def tick_at(self, value: float) -> None:
        self.clock.set(value)
        self.scheduler.tick()

    def test_initial_attention_delays_repeats_and_cancels(self) -> None:
        self.scheduler.observe(snapshot(attention=True))
        self.tick_at(4.9)
        self.assertEqual(self.bells, [])
        self.tick_at(5.0)
        self.tick_at(7.0)
        self.tick_at(7.25)
        self.tick_at(7.50)
        self.assertEqual(self.bells, [7.0, 7.25, 7.5])

        self.tick_at(65.0)
        self.tick_at(67.0)
        self.assertEqual(self.bells[-1], 67.0)

        self.scheduler.observe(snapshot())
        self.tick_at(127.0)
        self.assertEqual(len(self.bells), 4)

    def test_attention_resolution_cancels_pending_first_sound(self) -> None:
        self.scheduler.observe(snapshot(attention=True))
        self.tick_at(5.0)
        self.clock.set(6.0)
        self.scheduler.observe(snapshot())
        self.tick_at(7.0)
        self.assertEqual(self.bells, [])

    def test_completion_is_filtered_deduplicated_and_not_replayed_at_startup(self) -> None:
        completed = TurnSummary("old", completed_at=1.0, duration_seconds=30.0, status="completed")
        self.scheduler.observe(snapshot(turns=[completed]))
        self.tick_at(3.0)
        self.assertEqual(self.bells, [])

        short = TurnSummary("short", completed_at=2.0, duration_seconds=5.0, status="completed")
        long = TurnSummary("long", completed_at=2.0, duration_seconds=12.0, status="completed")
        self.clock.set(4.0)
        self.scheduler.observe(snapshot(turns=[completed, short, long]))
        self.tick_at(6.0)
        self.tick_at(6.15)
        self.assertEqual(self.bells, [6.0, 6.15])

        self.scheduler.observe(snapshot(turns=[completed, short, long]))
        self.tick_at(8.0)
        self.assertEqual(len(self.bells), 2)

    def test_attention_wins_a_shared_merge_window(self) -> None:
        self.scheduler.observe(snapshot(attention=True))
        completed = TurnSummary("long", completed_at=4.0, duration_seconds=12.0, status="completed")
        self.clock.set(4.0)
        self.scheduler.observe(snapshot(attention=True, turns=[completed]))
        self.tick_at(5.0)
        self.tick_at(6.0)
        self.tick_at(6.25)
        self.tick_at(6.50)
        self.assertEqual(self.bells, [6.0, 6.25, 6.5])

    def test_master_disable_clears_pending_pulses(self) -> None:
        self.scheduler.observe(snapshot(attention=True))
        self.tick_at(5.0)
        self.scheduler.configure(
            enabled=False,
            attention_enabled=True,
            completion_enabled=True,
        )
        self.tick_at(7.0)
        self.assertEqual(self.bells, [])


if __name__ == "__main__":
    unittest.main()
