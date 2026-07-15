#!/usr/bin/env python3
from __future__ import annotations

import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_net_health import activity, collectors, config, models, ui  # noqa: E402


def process(session_id: str = "session-test") -> models.ProcessInfo:
    return models.ProcessInfo(
        pid=4242,
        ppid=1,
        command="codex",
        elapsed_seconds=100,
        cpu_percent=0.0,
        process_state="S",
        wait_channel="futex_wait_queue",
        args="codex",
        role="session",
        session_id=session_id,
    )


class ActivityTrackerTests(unittest.TestCase):
    def derive(self, events: list[tuple[int, str, str]]) -> models.ConversationActivity:
        tracker = activity.ActivityTracker(900)
        now = time.time()
        tracker.events["session-test"] = [
            models.ActivityEvent(
                now - age,
                kind,
                config.ACTIVITY_LABELS.get(kind, kind),
                detail,
            )
            for age, kind, detail in events
        ]
        tracker.events["session-test"].sort(key=lambda item: item.timestamp)
        return tracker._derive(process())

    def test_pre_request_stall(self) -> None:
        state = self.derive([(61, "TASK_START", "")])
        self.assertEqual(state.alert, config.ALERT_PRE_REQUEST)

    def test_http_response_stall(self) -> None:
        state = self.derive(
            [(80, "TASK_START", ""), (31, "HTTP_POST", "")]
        )
        self.assertEqual(state.alert, config.ALERT_HTTP_RESPONSE)

    def test_post_tool_stall(self) -> None:
        state = self.derive(
            [
                (110, "TOOL_DONE", ""),
                (105, "HTTP_POST", ""),
                (95, "RESPONSE_STARTED", ""),
            ]
        )
        self.assertEqual(state.alert, config.ALERT_POST_TOOL)

    def test_keepalive_only(self) -> None:
        state = self.derive(
            [
                (140, "HTTP_POST", ""),
                (135, "RESPONSE_STARTED", ""),
                (125, "REASONING", ""),
                (5, "KEEPALIVE", ""),
            ]
        )
        self.assertEqual(state.alert, config.ALERT_KEEPALIVE_ONLY)

    def test_compacting_state(self) -> None:
        state = self.derive(
            [
                (12, "TOKEN_USAGE", "207000/220000"),
                (10, "COMPACT_START", "自动压缩 MidTurn"),
            ]
        )
        self.assertTrue(state.compacting)
        self.assertEqual(state.compact_mode, "自动")
        self.assertEqual(state.compact_phase, "mid-turn")
        self.assertEqual(state.token_used, 207000)

    def test_alert_recovery_is_recorded(self) -> None:
        tracker = activity.ActivityTracker(900)
        now = time.time()
        tracker.events["session-test"] = [
            models.ActivityEvent(now - 40, "HTTP_POST", "post")
        ]
        first = tracker._derive(process())
        self.assertEqual(first.alert, config.ALERT_HTTP_RESPONSE)
        tracker.events["session-test"].extend(
            [
                models.ActivityEvent(now, "RESPONSE_STARTED", "started"),
                models.ActivityEvent(now + 0.01, "REASONING", "reasoning"),
            ]
        )
        second = tracker._derive(process())
        self.assertFalse(second.alert)
        self.assertTrue(any(event.kind == "RECOVERED" for event in second.events))

    def test_sensitive_values_are_redacted(self) -> None:
        github_token = "ghp_" + ("a" * 26)
        text = activity.redact_sensitive(
            f"TOKEN={github_token} password=hunter2 Authorization: Bearer abc.def"
        )
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc.def", text)
        self.assertNotIn(github_token, text)
        self.assertGreaterEqual(text.count("[REDACTED]"), 3)

    def test_frame_clears_each_line_before_writing_text(self) -> None:
        output = io.StringIO()
        with mock.patch.object(ui.sys, "stdout", output):
            ui.emit_frame([("visible TUI", "")], width=80, height=1, use_color=False)

        rendered = output.getvalue()
        self.assertLess(rendered.index(config.ERASE_LINE), rendered.index("visible TUI"))
        self.assertIn("visible TUI\r\n", rendered)


class CollectorAndRenderingTests(unittest.TestCase):
    def test_process_parser_discovers_codex_session(self) -> None:
        rows = "4242 1 codex 100 0.0 S futex_wait_queue /usr/bin/codex --model test\n"

        processes = collectors.parse_ps_output(rows)

        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].pid, 4242)
        self.assertEqual(processes[0].role, "session")

    def test_text_renderer_handles_connection_details(self) -> None:
        item = models.ProcessAssessment(
            process=process(),
            health=config.STATE_ACTIVE,
            network_hang="否",
            reason="采样期间存在流量进展",
            connections=[
                models.ConnectionAssessment(
                    key="127.0.0.1:50000->203.0.113.1:443",
                    state="ESTAB",
                    local="127.0.0.1:50000",
                    peer="203.0.113.1:443",
                    route="external",
                    recv_q=0,
                    send_q=0,
                    sent_delta=128,
                    received_delta=256,
                    acked_delta=128,
                    retrans_delta=0,
                    idle_seconds=0.1,
                    health=config.STATE_ACTIVE,
                    reason="采样期间存在流量进展",
                )
            ],
        )
        output = io.StringIO()

        with mock.patch.object(ui.sys, "stdout", output):
            ui.render_text(
                [item],
                models.SseHealth(True, 900),
                interval=1.0,
                use_color=False,
                show_auxiliary=False,
            )

        rendered = output.getvalue()
        self.assertIn("203.0.113.1:443", rendered)
        self.assertIn("活跃传输", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
