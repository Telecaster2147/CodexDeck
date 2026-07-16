from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    CodexPaths,
    FailureInfo,
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
from presentation.tui.textual_app import (  # noqa: E402
    CodexNetApp,
    NavigationItem,
    timeline_entries,
)
from textual.widgets import ContentSwitcher, Input, RichLog, Static  # noqa: E402


def make_snapshot(count: int = 3) -> MonitorSnapshot:
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
            cwd="/work/repository",
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
        "CODEX_HOME",
        "CODEX_HOME",
        "test",
        processes=processes,
        sessions=sessions,
    )
    return MonitorSnapshot("2026-07-16T10:20:30+08:00", 2.0, [instance])


class FakeMachine:
    lookback_seconds = 900

    def acknowledge_alert(self, *_: object) -> bool:
        return True


class FakeEngine:
    interval = 999.0
    machine = FakeMachine()

    def __init__(self, snapshot: MonitorSnapshot) -> None:
        self.snapshot = snapshot
        self.pinned: SessionHealth | None = None

    def pin_session(self, session: SessionHealth | None) -> None:
        self.pinned = session

    def sample(self) -> MonitorSnapshot:
        return self.snapshot


class TextualTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_wide_layout_mounts_navigation_and_persistent_inspector(self) -> None:
        snapshot = make_snapshot()
        engine = FakeEngine(snapshot)
        app = CodexNetApp(engine, snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(len(app.query(NavigationItem)), 4)
            title = str(app.query_one("#session-title", Static).render())
            self.assertIn("Session 0", title)
            self.assertEqual(engine.pinned, snapshot.sessions[0])
            self.assertFalse(app.screen.has_class("compact"))

            refreshed = make_snapshot(1)
            refreshed.sessions[0].process.session_title = "Refreshed session"
            app._apply_snapshot(refreshed)
            await pilot.pause()
            self.assertEqual(len(app.query(NavigationItem)), 2)
            self.assertIn(
                "Refreshed session",
                str(app.query_one("#session-title", Static).render()),
            )

    async def test_refresh_preserves_log_scroll_focus_and_navigation_widgets(self) -> None:
        snapshot = make_snapshot(1)
        snapshot.sessions[0].events = [
            NormalizedEvent(float(index), "KEEPALIVE", f"Event {index}", "detail")
            for index in range(80)
        ]
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            log = app.query_one("#timeline-panel", RichLog)
            log.focus()
            log.scroll_to(y=8, animate=False, immediate=True)
            await pilot.pause()
            self.assertFalse(log.is_vertical_scroll_end)
            previous_scroll_y = log.scroll_y
            previous_navigation = tuple(app.query(NavigationItem))

            refreshed = make_snapshot(1)
            refreshed.sessions[0].events = [
                NormalizedEvent(float(index), "KEEPALIVE", f"Event {index}", "detail")
                for index in range(81)
            ]
            app._apply_snapshot(refreshed)
            await pilot.pause()

            self.assertEqual(log.scroll_y, previous_scroll_y)
            self.assertIs(app.focused, log)
            self.assertEqual(tuple(app.query(NavigationItem)), previous_navigation)
            self.assertFalse(log.is_vertical_scroll_end)

    async def test_compact_layout_drills_into_detail_and_returns(self) -> None:
        snapshot = make_snapshot()
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertTrue(app.screen.has_class("compact"))
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(app.screen.has_class("detail-open"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.screen.has_class("detail-open"))

    async def test_default_groups_sessions_by_workspace_with_home_context(self) -> None:
        snapshot = make_snapshot(2)
        snapshot.sessions[0].process.cwd = "/workspace/project-a"
        snapshot.sessions[1].process.cwd = "/workspace/project-b"
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            groups = [item for item in app.query(NavigationItem) if item.kind == "workspace"]
            self.assertEqual(len(groups), 2)
            labels = "\n".join(
                str(item.query_one(Static).render()) for item in groups
            )
            self.assertIn("/workspace/project-a", labels)
            self.assertIn("/workspace/project-b", labels)
            self.assertIn("CODEX_HOME CODEX_HOME", labels)

    async def test_tabs_help_and_search_are_framework_managed(self) -> None:
        snapshot = make_snapshot()
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("2")
            self.assertEqual(
                app.query_one("#detail-content", ContentSwitcher).current,
                "turns-panel",
            )
            await pilot.press("?")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press("escape")
            search = app.query_one("#search", Input)
            search.value = "Session 2"
            await pilot.pause()
            self.assertEqual(len(app.query(NavigationItem)), 2)

    async def test_terminal_floor_shows_small_terminal_state(self) -> None:
        snapshot = make_snapshot()
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(40, 10)) as pilot:
            await pilot.pause()
            self.assertTrue(app.screen.has_class("too-small"))
            self.assertIn(
                "终端尺寸过小",
                str(app.query_one("#too-small", Static).render()),
            )

    def test_timeline_failure_is_not_duplicated_or_reordered(self) -> None:
        session = make_snapshot(1).sessions[0]
        failure = FailureInfo("request_error", "earlier failure", "", "turn-1", 10.0)
        session.events = [
            NormalizedEvent(
                10.0,
                "TURN_FAILED",
                "模型调用失败",
                "earlier failure",
                failure=failure,
            ),
            NormalizedEvent(20.0, "KEEPALIVE", "收到 keepalive", "keepalive"),
        ]
        session.current_failure = failure
        session.latest_failure = failure

        entries = timeline_entries(session)
        self.assertEqual(
            sum(_kind(item) == "TURN_FAILED" for item in entries),
            1,
        )
        self.assertEqual([_kind(item) for item in entries], ["TURN_FAILED", "KEEPALIVE"])

        session.current_failure = None
        session.events = [NormalizedEvent(20.0, "KEEPALIVE", "收到 keepalive")]
        self.assertEqual([_kind(item) for item in timeline_entries(session)], ["KEEPALIVE"])


def _kind(item: object) -> str:
    return str(item.get("kind", "")) if isinstance(item, dict) else str(item.kind)


if __name__ == "__main__":
    unittest.main()
