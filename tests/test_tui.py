from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    AlertOccurrence,
    AlertStatus,
    AlertTransition,
    CodexPaths,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
)
from presentation.tui.controller import detail_scroll_action  # noqa: E402
from presentation.tui.terminal import (  # noqa: E402
    ANSI_SEQUENCE,
    _read_escape_tail,
    emit_frame,
    visible_width,
)
from presentation.tui.views import (  # noqa: E402
    compare_layout,
    detail_layout,
    home_layout,
    main_layout,
)


def make_snapshot(count: int = 1) -> MonitorSnapshot:
    home = Path("/tmp/codex-home")
    paths = CodexPaths(
        home,
        home,
        home / "state.db",
        home / "log.db",
        home / "index",
        home / "sessions",
    )
    sessions: list[SessionHealth] = []
    processes: list[ProcessInfo] = []
    for index in range(count):
        process = ProcessInfo(
            ProcessIdentity(1000 + index, index + 1),
            1,
            "codex",
            index,
            0.0,
            "S",
            "futex",
            "codex",
            "session",
            instance_id="home",
            session_id=f"session-{index}",
            session_title=f"Session {index}",
            model="gpt-test",
        )
        processes.append(process)
        sessions.append(
            SessionHealth(
                "home",
                f"session-{index}",
                process,
                LifecycleState.GENERATING,
                network=NetworkEvidence(NetworkState.ACTIVE, "正在接收数据"),
            )
        )
    instance = InstanceSnapshot(
        "home",
        paths,
        "~/.codex",
        "~/.codex",
        "test",
        processes=processes,
        sessions=sessions,
    )
    return MonitorSnapshot("2026-07-16T10:20:30+08:00", 2.0, [instance])


class TuiViewportTests(unittest.TestCase):
    def test_compare_layout_summarizes_multiple_homes_responsively(self) -> None:
        first = make_snapshot(2)
        second = make_snapshot(1).instances[0]
        second.instance_id = "home-two"
        second.display_codex_home = "~/work/codex-two"
        first.instances.append(second)
        for width in (39, 80, 120):
            layout = compare_layout(first, width, 8, False)
            rendered = "\n".join(layout.lines)
            self.assertEqual(len(layout.lines), 8)
            self.assertIn("~/.codex", rendered)
            self.assertIn("~/work/codex-two", rendered)
            self.assertTrue(all(visible_width(line) <= width for line in layout.lines))

    def test_selected_session_stays_visible_across_one_hundred_rows(self) -> None:
        snap = make_snapshot(100)
        key = f"session:{snap.sessions[-1].key}"
        layout = main_layout(snap, 80, 12, key, set(), True, False, False)
        self.assertIn(key, [ref.key for ref in layout.refs])
        self.assertEqual(len(layout.all_refs), 101)
        self.assertGreater(layout.top, 0)
        self.assertLessEqual(len(layout.refs), layout.body_height)

    def test_screen_refs_never_include_offscreen_rows(self) -> None:
        layout = main_layout(make_snapshot(30), 80, 10, "", set(), True, False, False)
        self.assertLess(len(layout.refs), len(layout.all_refs))
        self.assertLessEqual(len(layout.refs), layout.body_height)

    def test_detail_follow_up_moves_only_one_visual_row(self) -> None:
        top, follow = detail_scroll_action("\x1b[A", 40, 40, 8, "timeline", True)
        self.assertEqual(top, 39)
        self.assertFalse(follow)
        top, follow = detail_scroll_action("\x1b[B", top, 40, 8, "timeline", follow)
        self.assertEqual(top, 40)
        top, _ = detail_scroll_action("\x1b[B", top, 40, 8, "timeline", follow)
        self.assertEqual(top, 40)

    def test_detail_page_home_end_contract(self) -> None:
        self.assertEqual(
            detail_scroll_action("\x1b[5~", 20, 40, 8, "evidence", False)[0],
            12,
        )
        self.assertEqual(
            detail_scroll_action("\x1b[6~", 20, 40, 8, "evidence", False)[0],
            28,
        )
        self.assertEqual(
            detail_scroll_action("\x1b[H", 20, 40, 8, "timeline", True),
            (0, False),
        )
        self.assertEqual(
            detail_scroll_action("G", 3, 40, 8, "timeline", False),
            (40, True),
        )

    def test_long_events_scroll_by_wrapped_visual_rows(self) -> None:
        session = make_snapshot().sessions[0]
        session.events = [NormalizedEvent(1, "WARNING", "long", "x" * 180)]
        narrow = detail_layout(session, 24, 8, False, "timeline", False)
        wide = detail_layout(session, 120, 8, False, "timeline", False)
        self.assertGreater(narrow.max_top, wide.max_top)

    def test_modes_have_independent_content_and_fixed_network_row(self) -> None:
        session = make_snapshot().sessions[0]
        session.events = [NormalizedEvent(1, "TURN_STARTED", "started")]
        rows = {}
        for mode in ("timeline", "turns", "evidence"):
            layout = detail_layout(session, 80, 12, False, mode, False, 999)
            rows[mode] = layout.lines
            self.assertIn("网络", layout.lines[1])
        self.assertIn("Timeline", "\n".join(rows["timeline"]))
        self.assertIn("Turns", "\n".join(rows["turns"]))
        self.assertIn("Evidence", "\n".join(rows["evidence"]))

    def test_alert_lifecycle_is_visible_in_timeline_and_evidence(self) -> None:
        session = make_snapshot().sessions[0]
        session.alerts = [
            AlertOccurrence(
                "alert-1",
                "stage_stall",
                "warning",
                AlertStatus.ACKNOWLEDGED,
                "等待模型响应",
                1,
                3,
                acknowledged_at=3,
                transitions=[
                    AlertTransition(AlertStatus.OPENED, 1, "等待模型响应"),
                    AlertTransition(AlertStatus.ACKNOWLEDGED, 3, "用户已确认"),
                ],
            )
        ]
        timeline = "\n".join(
            detail_layout(session, 80, 14, False, "timeline", False).lines
        )
        evidence = "\n".join(
            detail_layout(session, 80, 14, False, "evidence", False).lines
        )
        self.assertIn("告警已打开", timeline)
        self.assertIn("告警已确认", timeline)
        self.assertIn("[ACKNOWLEDGED]", evidence)

    def test_home_detail_is_a_session_viewport(self) -> None:
        snap = make_snapshot(30)
        instance = snap.instances[0]
        key = f"session:{instance.sessions[-1].key}"
        layout = home_layout(instance, 60, 10, key, False)
        self.assertIn("HOME", layout.lines[0])
        self.assertIn(key, [ref.key for ref in layout.refs])
        self.assertEqual(len(layout.all_refs), 30)

    def test_responsive_matrix_has_exact_dimensions(self) -> None:
        snap = make_snapshot(4)
        session = snap.sessions[0]
        session.events = [NormalizedEvent(1, "TURN_FAILED", "failed", "detail" * 20)]
        for width in (24, 39, 40, 79, 80, 120):
            for height in (3, 5, 8, 24):
                overview = main_layout(snap, width, height, "", set(), True, False, True)
                detail = detail_layout(session, width, height, True)
                for lines in (overview.lines, detail.lines):
                    self.assertEqual(len(lines), height, (width, height))
                    self.assertTrue(
                        all(visible_width(line) <= width for line in lines),
                        (width, height),
                    )
                self.assertIn("网络", detail.lines[1])

    def test_color_and_plain_layout_text_match(self) -> None:
        session = make_snapshot().sessions[0]
        session.events = [NormalizedEvent(1, "TURN_FAILED", "failed")]
        colored = detail_layout(session, 80, 12, True).lines
        plain = detail_layout(session, 80, 12, False).lines
        self.assertEqual([ANSI_SEQUENCE.sub("", line) for line in colored], plain)
        self.assertIn("ERR", "\n".join(plain))


class TerminalProtocolTests(unittest.TestCase):
    def test_reads_complete_variable_length_csi(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"[1;5A")
            self.assertEqual(_read_escape_tail(read_fd), "[1;5A")
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_only_first_frame_clears_screen(self) -> None:
        output = io.StringIO()
        with patch("sys.stdout", output):
            emit_frame(["first"], 20, 3, clear=True)
            split = len(output.getvalue())
            emit_frame(["next"], 20, 3, clear=False)
        rendered = output.getvalue()
        self.assertIn("\x1b[2J", rendered[:split])
        self.assertNotIn("\x1b[2J", rendered[split:])


if __name__ == "__main__":
    unittest.main()
