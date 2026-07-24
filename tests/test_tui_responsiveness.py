from __future__ import annotations

from dataclasses import replace
import time
import unittest

from textual.widgets import ListView, RichLog

from models import NormalizedEvent, TerminalCapability, TerminalChunk, TerminalSessionSummary
from presentation.tui.responsiveness import ResponsivenessReport, ResponsivenessSample
from presentation.tui.textual_app import CodexDeckApp
from tests.test_tui import FakeEngine, make_snapshot


class ResponsivenessEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_slow_callback_is_degraded_even_when_correctness_passes(self) -> None:
        report = ResponsivenessReport(
            cadence_seconds=0.1,
            correctness_passed=True,
            samples=(
                ResponsivenessSample(
                    "rollout_burst",
                    callback_latency_seconds=0.2,
                    event_loop_lag_seconds=0.01,
                    screen_update_seconds=0.02,
                ),
            ),
        )
        payload = report.as_dict()
        self.assertEqual(payload["correctness"], "PASS")
        self.assertEqual(payload["responsiveness"], "DEGRADED")
        self.assertIn("rollout_burst:callback_latency", payload["degraded_reasons"])

    async def test_pilot_collects_high_load_responsiveness_evidence(self) -> None:
        snapshot = make_snapshot(20)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)
        samples: list[ResponsivenessSample] = []

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            navigation = app.query_one("#session-list", ListView)

            refreshed = make_snapshot(20)
            refreshed.sessions[0].events = [
                NormalizedEvent(float(index), "MODEL_PROGRESS", f"burst {index}")
                for index in range(100)
            ]
            focused = app.focused
            scroll_y = navigation.scroll_y
            started = time.perf_counter()
            app._apply_snapshot(refreshed)
            await pilot.pause()
            elapsed = time.perf_counter() - started
            samples.append(
                ResponsivenessSample(
                    "rollout_burst",
                    elapsed,
                    0.0,
                    elapsed,
                    focus_stable=app.focused is focused,
                    scroll_stable=navigation.scroll_y == scroll_y,
                    follow_stable=app.follow,
                )
            )

            before_key = app.selected_key
            started = time.perf_counter()
            await pilot.press("down")
            await pilot.pause()
            key_elapsed = time.perf_counter() - started
            samples.append(
                ResponsivenessSample(
                    "navigation_update",
                    key_elapsed,
                    0.0,
                    key_elapsed,
                    key_to_visible_seconds=key_elapsed,
                    visible_update=app.selected_key != before_key,
                )
            )

            terminal = TerminalSessionSummary(
                "terminal-1",
                process_id="123",
                command="benchmark command",
                status="running",
                process_active=True,
                capability=TerminalCapability.POLL_TRANSCRIPT,
                chunks=(TerminalChunk("chunk-1", 1.0, text="ready\n"),),
            )
            terminal_snapshot = make_snapshot(20)
            terminal_snapshot.sessions[0].terminal_sessions = [terminal]
            started = time.perf_counter()
            app._apply_snapshot(terminal_snapshot)
            await pilot.press("3")
            await pilot.pause()
            terminal_snapshot_2 = make_snapshot(20)
            terminal_snapshot_2.sessions[0].terminal_sessions = [
                replace(
                    terminal,
                    chunks=terminal.chunks
                    + (TerminalChunk("chunk-2", 2.0, text="appended\n", sequence=2),),
                )
            ]
            app._apply_snapshot(terminal_snapshot_2)
            await pilot.pause()
            terminal_elapsed = time.perf_counter() - started
            output = app.query_one("#terminal-output", RichLog)
            samples.append(
                ResponsivenessSample(
                    "terminal_append",
                    terminal_elapsed,
                    0.0,
                    terminal_elapsed,
                    visible_update=any("appended" in line.text for line in output.lines),
                )
            )

            focused = app.focused
            started = time.perf_counter()
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            await pilot.resize_terminal(120, 30)
            await pilot.pause()
            resize_elapsed = time.perf_counter() - started
            samples.append(
                ResponsivenessSample(
                    "narrow_resize",
                    resize_elapsed,
                    0.0,
                    resize_elapsed,
                    focus_stable=app.focused is focused,
                    visible_update=app.size.width == 120,
                )
            )

        report = ResponsivenessReport(0.1, correctness_passed=True, samples=tuple(samples))
        payload = report.as_dict()
        self.assertEqual(payload["correctness"], "PASS")
        self.assertEqual(
            {sample["scenario"] for sample in payload["samples"]},
            {"rollout_burst", "navigation_update", "terminal_append", "narrow_resize"},
        )
        for sample in payload["samples"]:
            self.assertGreaterEqual(sample["callback_latency_seconds"], 0.0)
        self.assertIn(payload["responsiveness"], {"PASS", "DEGRADED"})


if __name__ == "__main__":
    unittest.main()
