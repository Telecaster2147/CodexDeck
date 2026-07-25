from __future__ import annotations

import io
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rich.console import Console

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    AxisCompleteness,
    AttentionRequest,
    AttentionState,
    CodexPaths,
    CompactionSummary,
    CurrentOperationSummary,
    DiagnosisFinding,
    FailureInfo,
    HistoryWindowStats,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
    RecoveryState,
    SessionHealth,
    SessionCompleteness,
    TerminalCapability,
    TerminalChunk,
    TerminalSessionSummary,
    TokenUsageSummary,
    UnparsedPayload,
)
from preferences import CodexDeckPreferences  # noqa: E402
from presentation.tui.textual_app import (  # noqa: E402
    CodexDeckApp,
    NavigationItem,
    SampleCompleted,
    SessionInspector,
    SettingsScreen,
    ShortcutFooter,
    StartupOverlay,
    TerminalPanel,
    _diagnosis_details_renderable,
    _diagnosis_renderable,
    _timeline_line,
    binding_key_label,
    keyboard_reference,
    session_marker,
    session_hidden_label,
    session_status,
    startup_renderable,
    timeline_entries,
)
from presentation.tui.sampling import SamplingCoordinator  # noqa: E402
from textual.css.query import NoMatches  # noqa: E402
from textual.widgets import (  # noqa: E402
    Collapsible,
    ContentSwitcher,
    DataTable,
    Input,
    ListView,
    RichLog,
    Select,
    Static,
    Switch,
    Tabs,
)


def render_plain(renderable: object, width: int = 120) -> str:
    output = io.StringIO()
    Console(width=width, file=output, color_system=None).print(renderable)
    return output.getvalue()


class SamplingCoordinatorTests(unittest.TestCase):
    def test_coordinates_fast_full_and_manual_samples(self) -> None:
        coordinator = SamplingCoordinator.starting_at(2.0, 10.0)

        self.assertFalse(coordinator.begin_due(10.1))
        self.assertIsNone(coordinator.begin_due(12.0))
        coordinator.finish()

        self.assertTrue(coordinator.begin_due(12.0))
        self.assertEqual(coordinator.next_full_at, 14.0)
        coordinator.finish()

        self.assertTrue(coordinator.begin_manual(13.0))
        self.assertEqual(coordinator.next_full_at, 15.0)
        coordinator.finish()

        self.assertTrue(coordinator.begin_initial())
        self.assertFalse(coordinator.begin_initial())

    def test_single_jitter_does_not_degrade_and_consecutive_overdue_does(self) -> None:
        coordinator = SamplingCoordinator.starting_at(2.0, 10.0, wall_now=100.0)
        self.assertFalse(coordinator.begin_due(10.0))
        coordinator.finish(10.15)
        self.assertFalse(coordinator.summary(10.15).degraded)

        self.assertFalse(coordinator.begin_due(10.2))
        self.assertIsNone(coordinator.begin_due(10.31))
        self.assertIsNone(coordinator.begin_due(10.42))
        summary = coordinator.summary(10.42)
        self.assertTrue(summary.degraded)
        self.assertEqual(summary.reason, "consecutive_sample_overdue")
        self.assertEqual(summary.skipped_ticks, 2)
        self.assertGreater(summary.worker_in_flight_age_seconds, 0.2)

    def test_successful_sample_recovers_and_manual_full_resets_schedule(self) -> None:
        coordinator = SamplingCoordinator.starting_at(2.0, 10.0, wall_now=100.0)
        self.assertFalse(coordinator.begin_due(10.0))
        self.assertIsNone(coordinator.begin_due(10.2))
        self.assertIsNone(coordinator.begin_due(10.3))
        coordinator.finish(10.31)

        self.assertTrue(coordinator.begin_manual(10.4))
        coordinator.finish(10.5)
        summary = coordinator.summary(10.5)
        self.assertFalse(summary.degraded)
        self.assertEqual(summary.sample_kind, "manual_full")
        self.assertEqual(coordinator.next_full_at, 12.4)
        self.assertAlmostEqual(summary.last_success_at or 0.0, 100.5)


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
        self.baselines = 0
        self.full_samples = 0
        self.event_samples = 0

    def baseline(self) -> None:
        self.baselines += 1

    def pin_session(self, session: SessionHealth | None) -> None:
        self.pinned = session

    def sample(self) -> MonitorSnapshot:
        self.full_samples += 1
        return self.snapshot

    def prepare_initial_snapshot(self) -> MonitorSnapshot:
        self.baseline()
        return self.sample()

    def refresh_events(self, snapshot: MonitorSnapshot) -> MonitorSnapshot:
        self.event_samples += 1
        return snapshot


class TextualTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_classic_blue_is_the_default_palette_and_cycles_all_themes(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            self.assertEqual(app.theme, "codexdeck-blue")
            variables = app.get_css_variables()
            self.assertEqual(variables["background"], "#0F172A")
            self.assertEqual(variables["surface"], "#111827")
            self.assertEqual(variables["panel"], "#1F2937")
            self.assertEqual(variables["primary"], "#38BDF8")

            await pilot.press("t")
            self.assertEqual(app.theme, "textual-dark")
            await pilot.press("t")
            self.assertEqual(app.theme, "textual-light")
            await pilot.press("t")
            self.assertEqual(app.theme, "codexdeck-blue")

    def test_startup_renderable_has_wide_and_compact_brand_frames(self) -> None:
        wide = render_plain(startup_renderable(0), width=120)
        ready = render_plain(startup_renderable(99), width=120)
        compact = render_plain(startup_renderable(2, compact=True), width=50)

        self.assertIn("██████", wide)
        self.assertIn("██████╗ ███████╗ ██████╗██╗  ██╗", wide)
        self.assertIn("CORE", wide)
        self.assertIn("CONSOLE READY", ready)
        self.assertIn("CODEXDECK", compact)
        self.assertNotIn("██████", compact)
        self.assertLessEqual(max(map(len, compact.splitlines())), 50)

    async def test_startup_overlay_plays_fully_while_initial_sample_prepares(self) -> None:
        snapshot = make_snapshot(1)
        engine = FakeEngine(snapshot)
        empty = MonitorSnapshot("", 2.0, [])
        app = CodexDeckApp(
            engine,
            empty,
            sampling=False,
            startup_animation=True,
            prepare_on_start=True,
        )
        app.STARTUP_FRAME_INTERVAL = 0.01
        app.STARTUP_DURATION = 1.0

        async with app.run_test(size=(120, 30)) as pilot:
            overlay = app.query_one(StartupOverlay)
            self.assertTrue(overlay.display)
            await pilot.pause(0.03)
            self.assertTrue(overlay.display)
            self.assertEqual(engine.baselines, 1)
            self.assertEqual(engine.full_samples, 1)
            self.assertEqual(len(app.snapshot.sessions), 1)
            await pilot.pause(1.05)
            self.assertFalse(overlay.display)
            await pilot.press("3")
            self.assertEqual(app.query_one("#detail-tabs", Tabs).active, "terminal-tab")

    async def test_settings_persists_and_applies_all_preferences(self) -> None:
        snapshot = make_snapshot(1)
        with TemporaryDirectory() as temp:
            preference_file = Path(temp) / "preferences.json"
            app = CodexDeckApp(
                FakeEngine(snapshot),
                snapshot,
                sampling=False,
                startup_animation=True,
                preferences_file=preference_file,
            )
            app.STARTUP_DURATION = 0.01

            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(0.03)
                self.assertFalse(app.query_one(StartupOverlay).display)
                dark_background = app.query_one("#app-header").styles.background
                await pilot.press("s")
                await pilot.pause()
                self.assertIsInstance(app.screen, SettingsScreen)
                switch = app.screen.query_one("#startup-animation-switch", Switch)
                self.assertTrue(switch.value)
                switch.value = False
                app.screen.query_one("#group-sessions-switch", Switch).value = False
                app.screen.query_one("#show-hidden-switch", Switch).value = True
                app.screen.query_one("#follow-output-switch", Switch).value = False
                app.screen.query_one("#notifications-switch", Switch).value = False
                app.screen.query_one("#theme-select", Select).value = "textual-light"
                app.screen.query_one("#default-tab-select", Select).value = "terminal"
                await pilot.press("s")
                await pilot.pause()

                self.assertFalse(app.grouped)
                self.assertTrue(app.show_hidden)
                self.assertFalse(app.follow)
                self.assertFalse(app.notifications_enabled)
                self.assertEqual(app.theme, "textual-light")
                self.assertNotEqual(
                    app.query_one("#app-header").styles.background,
                    dark_background,
                )
                self.assertEqual(app.query_one("#detail-tabs", Tabs).active, "terminal-tab")

            self.assertEqual(
                json.loads(preference_file.read_text()),
                {
                    "startup_animation": False,
                    "group_sessions": False,
                    "show_hidden_sessions": True,
                    "follow_output": False,
                    "notifications": False,
                    "theme": "textual-light",
                    "default_tab": "terminal",
                },
            )

    async def test_settings_scrolls_cleanly_on_narrow_terminal(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(72, 24)) as pilot:
            await pilot.press("s")
            await pilot.pause()

            dialog = app.screen.query_one("#settings-dialog")
            scroll = app.screen.query_one("#settings-scroll")
            self.assertLessEqual(dialog.size.width, 68)
            self.assertGreater(scroll.virtual_size.height, scroll.size.height)
            self.assertEqual(len(app.screen.query(".setting-row")), 7)
            self.assertEqual(len(app.screen.query(Switch)), 5)
            self.assertEqual(len(app.screen.query(Select)), 2)

            scroll.scroll_end(animate=False)
            await pilot.pause()
            self.assertGreater(scroll.scroll_y, 0)
            self.assertTrue(app.screen.query_one("#theme-select", Select).display)

        minimum = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)
        async with minimum.run_test(size=(50, 20)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            dialog = minimum.screen.query_one("#settings-dialog")
            scroll = minimum.screen.query_one("#settings-scroll")
            hint = minimum.screen.query_one("#settings-hint", Static)
            self.assertLessEqual(dialog.size.width, 48)
            self.assertGreater(scroll.size.height, 0)
            self.assertGreater(scroll.virtual_size.height, scroll.size.height)
            self.assertIn("放弃修改", str(hint.render()))
            self.assertIn("保存设置", str(hint.render()))

    async def test_settings_escape_discards_changes_and_flat_override_survives_save(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexDeckApp(
            FakeEngine(snapshot),
            snapshot,
            flat=True,
            sampling=False,
            preferences=CodexDeckPreferences(group_sessions=True),
        )

        async with app.run_test(size=(120, 30)) as pilot:
            self.assertFalse(app.grouped)
            await pilot.press("s")
            app.screen.query_one("#show-hidden-switch", Switch).value = True
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.show_hidden)

            await pilot.press("s")
            await pilot.press("s")
            await pilot.pause()
            self.assertFalse(app.grouped)

    async def test_overview_prioritizes_action_required_and_anomaly_key_selects_it(self) -> None:
        snapshot = make_snapshot(2)
        waiting = snapshot.sessions[1]
        waiting.attention = AttentionState.APPROVAL
        waiting.attention_request = AttentionRequest(
            AttentionState.APPROVAL,
            call_id="call-1",
            summary="等待用户操作",
            detail="Approve command",
            started_at=10.0,
        )
        waiting.current_operation = CurrentOperationSummary(
            "attention", "等待用户操作", "Approve command", 10.0
        )
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            sessions = [item for item in app.query(NavigationItem) if item.kind == "session"]
            self.assertEqual(sessions[0].session_key, waiting.key)
            self.assertIn(
                "ATTENTION · Approve command",
                str(sessions[0].query_one(Static).render()),
            )
            self.assertIn("ATTENTION 1", str(app.query_one("#app-header").render()))
            app.selected_session = snapshot.sessions[0]
            await pilot.press("]")
            self.assertIs(app.selected_session, waiting)
            self.assertEqual(app.query_one("#detail-tabs", Tabs).active, "diagnosis-tab")
            selected = app.selected_session
            await pilot.press("tab")
            self.assertIs(app.selected_session, selected)
            self.assertIsNot(app.focused, app.query_one("#session-list", ListView))

    async def test_attention_transition_notifications_follow_preference(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)
        notices: list[tuple[str, dict[str, object]]] = []

        async with app.run_test(size=(120, 24)) as pilot:
            app.notify = lambda message, **kwargs: notices.append((message, kwargs))  # type: ignore[method-assign]
            refreshed = make_snapshot(1)
            session = refreshed.sessions[0]
            session.attention = AttentionState.USER_INPUT
            session.attention_request = AttentionRequest(
                AttentionState.USER_INPUT,
                summary="等待用户操作",
                detail="Choose an option",
            )
            app._apply_snapshot(refreshed)
            await pilot.pause()
            self.assertEqual(notices[0][0], "Choose an option")
            self.assertIn("ACTION REQUIRED", str(notices[0][1]["title"]))

            app.notifications_enabled = False
            resolved = make_snapshot(1)
            app._apply_snapshot(resolved)
            suppressed = make_snapshot(1)
            suppressed.sessions[0].attention_request = AttentionRequest(
                AttentionState.USER_INPUT,
                summary="等待用户操作",
                detail="Choose another option",
            )
            app._apply_snapshot(suppressed)
            await pilot.pause()
            self.assertEqual(len(notices), 1)

    def test_unparsed_trace_shows_identity_without_raw_object(self) -> None:
        line = _timeline_line(
            NormalizedEvent(
                10.0,
                "UNPARSED_PAYLOAD",
                "未识别协议数据",
                "event_msg:future · 80 chars · abcdef1234",
                unparsed=UnparsedPayload(
                    "event_msg:future",
                    80,
                    "abcdef1234567890",
                    '{"token":"[REDACTED]"}',
                ),
            )
        )

        self.assertIn("UNPARSED", render_plain(line))
        self.assertIn("event_msg:future", render_plain(line))
        self.assertIn("abcdef1234", render_plain(line))
        self.assertNotIn("REDACTED", render_plain(line))

    def test_plan_trace_visualizes_step_state(self) -> None:
        line = _timeline_line(
            NormalizedEvent(
                10.0,
                "PLAN_UPDATED",
                "计划已更新",
                metadata={
                    "plan": [
                        {"step": "解析 rollout 增量", "status": "completed"},
                        {"step": "刷新 Activity", "status": "in_progress"},
                    ]
                },
            )
        )

        self.assertIn("✓ 解析 rollout 增量", render_plain(line))
        self.assertIn("解析 rollout 增量", render_plain(line))
        self.assertIn("→ 刷新 Activity", render_plain(line))
        self.assertNotIn("{'step'", render_plain(line))

    def test_manual_compact_trace_shows_context_evidence(self) -> None:
        line = _timeline_line(
            NormalizedEvent(
                10.0,
                "COMPACTING",
                "正在压缩上下文",
                "检测到手动 compact 任务",
                metadata={
                    "trigger": "manual",
                    "context_tokens": 216_402,
                    "context_window": 353_400,
                    "auto_compact_token_limit": 220_000,
                },
            )
        )

        self.assertIn("COMPACT", render_plain(line))
        self.assertIn("手动 compact", render_plain(line))
        self.assertIn("216,402 / 353,400", render_plain(line))
        self.assertIn("61.2%", render_plain(line))
        self.assertIn("216,402 / 220,000", render_plain(line))
        self.assertIn("98.4%", render_plain(line))
        self.assertIn("剩余 3,598", render_plain(line))

    def test_diagnosis_keeps_conclusion_and_hides_repeated_capacity(self) -> None:
        snapshot = make_snapshot(1)
        instance = snapshot.instances[0]
        instance.auto_compact_token_limit = 220_000
        instance.auto_compact_config_source = "config.toml"
        session = instance.sessions[0]
        session.token_usage = TokenUsageSummary(
            context_tokens=216_402,
            context_window=353_400,
        )
        instance.history_windows = [
            HistoryWindowStats(
                "15m",
                900,
                sample_count=12,
                turn_count=2,
                failure_count=1,
                failure_rate=0.5,
                ttft_samples=2,
                ttft_p50_seconds=1.0,
                ttft_p95_seconds=3.0,
            )
        ]

        output = io.StringIO()
        console = Console(width=120, file=output, color_system=None)
        session.diagnosis = [DiagnosisFinding("info", "模型正在生成", "正常进展")]
        console.print(_diagnosis_renderable(session, instance))
        rendered = output.getvalue()

        self.assertIn("诊断结论", rendered)
        self.assertIn("证据属性  推导", rendered)
        self.assertIn("置信度 中", rendered)
        self.assertIn("完整度 完整", rendered)
        self.assertIn("数据质量", rendered)
        self.assertNotIn("自动 compact 边界", rendered)
        self.assertNotIn("config.toml", rendered)
        self.assertNotIn("历史趋势", rendered)
        self.assertNotIn("15m", rendered)
        self.assertNotIn("p50", rendered)
        detail_count, _ = _diagnosis_details_renderable(session, instance)
        self.assertEqual(detail_count, 0)

    def test_diagnosis_details_show_full_redacted_unknown_payload(self) -> None:
        snapshot = make_snapshot(1)
        session = snapshot.sessions[0]
        full_payload = '{"detail":"' + "x" * 400 + '","token":"[REDACTED]"}'
        session.events = [
            NormalizedEvent(
                10.0,
                "UNPARSED_PAYLOAD",
                "event_msg:future_protocol_event",
                source="rollout",
                source_id="rollout:42",
                complete=False,
                metadata={"diagnostic_payload": full_payload},
                unparsed=UnparsedPayload(
                    "event_msg:future_protocol_event",
                    len(full_payload),
                    "a" * 64,
                    full_payload[:240],
                    True,
                ),
            )
        ]

        count, details = _diagnosis_details_renderable(session, snapshot.instances[0])
        rendered = render_plain(details, width=160)

        self.assertEqual(count, 1)
        self.assertIn("完整脱敏 payload", rendered)
        self.assertIn("x" * 300, "".join(rendered.splitlines()))
        self.assertIn("[REDACTED]", rendered)

    def test_diagnosis_details_show_ingress_backlog_and_gap(self) -> None:
        snapshot = make_snapshot(1)
        instance = snapshot.instances[0]
        session = instance.sessions[0]
        session.process.rollout_path = "/workspace-a/rollout.jsonl"
        instance.rollout_activity = [
            {
                "path": session.process.rollout_path,
                "backlog_bytes": 4096,
                "backlog_records_lower_bound": 2,
                "backlog_age_seconds": 1.5,
                "budget_exceeded": True,
                "gap_count": 1,
                "skipped_bytes": 300000,
                "gap_reason": "oversize_jsonl_record",
            }
        ]

        count, details = _diagnosis_details_renderable(session, instance)
        rendered = render_plain(details)

        self.assertGreater(count, 0)
        self.assertIn("Rollout 入口", rendered)
        self.assertIn("积压记录下界  2", rendered)
        self.assertIn("oversize_jsonl_record", rendered)

    def test_diagnosis_details_label_truncated_unknown_payload(self) -> None:
        snapshot = make_snapshot(1)
        session = snapshot.sessions[0]
        session.events = [
            NormalizedEvent(
                10.0,
                "UNPARSED_PAYLOAD",
                "event_msg:future_protocol_event",
                metadata={
                    "diagnostic_payload": '{"detail":"retained"}',
                    "diagnostic_payload_dropped_chars": 5904,
                },
                unparsed=UnparsedPayload(
                    "event_msg:future_protocol_event", 10_000, "a" * 64, "preview", True
                ),
            )
        ]

        _, details = _diagnosis_details_renderable(session, snapshot.instances[0])
        rendered = render_plain(details, width=160)
        self.assertIn("已截断，省略 5904 chars", rendered)
        self.assertIn("retained", rendered)

    def test_derived_tool_boundaries_are_not_protocol_anomalies(self) -> None:
        snapshot = make_snapshot(1)
        session = snapshot.sessions[0]
        session.events = [
            NormalizedEvent(
                10.0,
                "TOOL_RUNNING",
                "exec_command",
                source="rollout",
                source_id="tool-start",
                derived=True,
                complete=False,
            ),
            NormalizedEvent(
                11.0,
                "TOOL_COMPLETED",
                "exec_command",
                source="rollout",
                source_id="tool-complete",
                derived=True,
                complete=False,
            ),
        ]

        summary = render_plain(_diagnosis_renderable(session, snapshot.instances[0]))
        detail_count, _ = _diagnosis_details_renderable(session, snapshot.instances[0])

        self.assertNotIn("不完整协议事件", summary)
        self.assertEqual(detail_count, 0)

    async def test_diagnosis_details_are_collapsed_and_expandable(self) -> None:
        snapshot = make_snapshot(1)
        snapshot.instances[0].diagnostics = ["完整采集异常详情：source fixture failed"]
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("2")
            await pilot.pause()
            details = app.query_one("#diagnosis-details", Collapsible)
            self.assertTrue(details.collapsed)
            self.assertIn("异常详情 (1)", str(details.title))

            details.collapsed = False
            await pilot.pause()

            self.assertFalse(details.collapsed)
            content = app.query_one("#diagnosis-details-content", Static).content
            rendered = render_plain(content)
            self.assertIn("采集来源报告降级", rendered)
            self.assertIn("fingerprint", rendered)
            self.assertNotIn("source fixture failed", rendered)

    def test_timeline_restores_trimmed_compact_from_summary(self) -> None:
        session = make_snapshot(1).sessions[0]
        session.events = []
        session.compactions = [
            CompactionSummary(
                started_at=10.0,
                completed_at=70.0,
                trigger="manual",
                context_tokens=216_402,
                context_window=353_400,
                turn_id="compact-turn",
            )
        ]

        entries = timeline_entries(session)

        self.assertEqual(
            [entry["kind"] for entry in entries],
            ["COMPACTING", "COMPACT_COMPLETED"],
        )

    def test_active_compact_edges_remain_visible_until_success_terminal(self) -> None:
        session = make_snapshot(1).sessions[0]
        session.events = [
            NormalizedEvent(10, "COMPACT_REQUESTED", "requested", turn_id="turn"),
            NormalizedEvent(11, "COMPACTING", "running", turn_id="turn"),
        ]
        session.compactions = [
            CompactionSummary(
                operation_id="operation",
                status="running",
                requested_at=10,
                started_at=11,
                trigger="manual",
                turn_id="turn",
            )
        ]
        self.assertEqual(
            [entry.kind for entry in timeline_entries(session)],
            ["COMPACT_REQUESTED", "COMPACTING"],
        )

        session.events.append(NormalizedEvent(12, "COMPACT_FAILED", "failed", turn_id="turn"))
        session.compactions[0] = CompactionSummary(
            operation_id="operation",
            status="failed",
            requested_at=10,
            started_at=11,
            failed_at=12,
            trigger="manual",
            turn_id="turn",
        )
        self.assertEqual(
            [entry.kind for entry in timeline_entries(session)],
            ["COMPACT_REQUESTED", "COMPACTING", "COMPACT_FAILED"],
        )

    def test_tool_output_hides_serialized_and_summarizes_background_wait(self) -> None:
        line = _timeline_line(
            NormalizedEvent(
                10.0,
                "TOOL_COMPLETED",
                "工具完成",
                metadata={
                    "arguments": '{"query":"session events","count":2}',
                    "output": '{"status":"completed","result":"two matching sessions"}',
                },
            ),
        )
        rendered = render_plain(line)
        self.assertNotIn("session events", rendered)
        self.assertNotIn("two matching sessions", rendered)
        self.assertNotIn('{"status"', rendered)

        background = _timeline_line(
            NormalizedEvent(
                11.0,
                "TOOL_COMPLETED",
                "工具已返回",
                metadata={
                    "background_running": True,
                    "background_cell_id": "199",
                    "background_wait_seconds": 10.0,
                    "background_output_empty": True,
                    "output": "",
                },
            ),
        )
        background_rendered = render_plain(background)
        self.assertIn("TASK", background_rendered)
        self.assertIn("cell 199 · 已等待 10.0s · 暂无新输出", background_rendered)
        self.assertNotIn("Wall time", background_rendered)
        self.assertNotIn("Output:", background_rendered)

    def test_tool_orchestration_source_is_replaced_by_call_summary(self) -> None:
        script = (
            'const result = await tools.exec_command({"cmd":"uv run tests"});\ntext(result.output);'
        )
        line = _timeline_line(
            NormalizedEvent(
                10.0,
                "TOOL_RUNNING",
                "工具正在运行",
                metadata={
                    "command": "uv run tests",
                    "arguments": script,
                    "nested_tools": ["exec_command"],
                },
            )
        )

        self.assertIn("CALLS", render_plain(line))
        self.assertIn("exec_command", render_plain(line))
        self.assertNotIn("const result", render_plain(line))
        self.assertNotIn("await tools", render_plain(line))

    def test_activity_filters_noise_and_tool_output(self) -> None:
        session = make_snapshot(1).sessions[0]
        session.events = [
            NormalizedEvent(1.0, "REASONING_SUMMARY", "推理摘要"),
            NormalizedEvent(2.0, "MODEL_PROGRESS", "模型进度"),
            NormalizedEvent(3.0, "TOOL_COMPLETED", "工具完成"),
        ]
        self.assertEqual(
            [_kind(item) for item in timeline_entries(session)],
            ["TOOL_COMPLETED"],
        )
        line = _timeline_line(
            NormalizedEvent(
                3.0,
                "TOOL_COMPLETED",
                "工具完成",
                metadata={"output": '{"message":"hidden output"}'},
            ),
        )
        self.assertNotIn("hidden output", render_plain(line))

    def test_activity_folds_completed_tool_boundaries(self) -> None:
        session = make_snapshot(1).sessions[0]
        session.events = [
            NormalizedEvent(
                1.0,
                "TOOL_RUNNING",
                "工具正在运行",
                source_id="start",
                metadata={
                    "call_id": "call-1",
                    "display_name": "Shell 命令",
                    "tool_name": "exec_command",
                    "command": "uv run tests",
                },
            ),
            NormalizedEvent(
                3.0,
                "TOOL_COMPLETED",
                "工具已返回",
                source_id="done",
                metadata={
                    "call_id": "call-1",
                    "display_name": "custom_tool_call_output",
                    "display_name_is_fallback": True,
                },
            ),
        ]

        operational = timeline_entries(session)

        self.assertEqual([_kind(item) for item in operational], ["TOOL_COMPLETED"])
        self.assertEqual(operational[0].metadata["duration_seconds"], 2.0)
        self.assertEqual(operational[0].metadata["display_name"], "Shell 命令")
        self.assertEqual(operational[0].metadata["tool_name"], "exec_command")
        rendered = render_plain(_timeline_line(operational[0]))
        self.assertIn("Shell 命令 调用完成", rendered)
        self.assertIn("TOOL", rendered)
        self.assertIn("exec_command", rendered)
        self.assertNotIn("custom_tool_call_output", rendered)

    def test_execution_trace_renders_command_and_pending_file(self) -> None:
        line = _timeline_line(
            NormalizedEvent(
                10.0,
                "TOOL_RUNNING",
                "工具正在运行",
                "exec",
                metadata={
                    "display_name": "exec",
                    "command": "uv run tests",
                    "cwd": "/workspace/project",
                    "files": ["/workspace/project/src/app.py"],
                },
            )
        )

        self.assertIn("WRITE", render_plain(line))
        self.assertIn("$ uv run tests", render_plain(line))
        self.assertIn("PENDING  /workspace/project/src/app.py", render_plain(line))

    def test_live_poll_uses_fast_event_refresh_between_full_samples(self) -> None:
        snapshot = make_snapshot(1)
        engine = FakeEngine(snapshot)
        app = CodexDeckApp(engine, snapshot, sampling=False)
        samples: list[bool] = []
        app._start_sample = lambda *, full: samples.append(full)  # type: ignore[method-assign]

        app.sampling = True
        app._poll_live_events()

        self.assertEqual(samples, [False])
        self.assertEqual(engine.full_samples, 0)
        self.assertEqual(engine.event_samples, 0)

    async def test_clock_tick_updates_ages_without_rebuilding_widgets_or_logs(self) -> None:
        snapshot = make_snapshot(1)
        snapshot.sessions[0].events = [NormalizedEvent(10, "MODEL_PROGRESS", "progress")]
        engine = FakeEngine(snapshot)
        app = CodexDeckApp(engine, snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            navigation = list(app.query(NavigationItem))
            log = app.query_one("#activity-panel", RichLog)
            line_count = len(log.lines)
            await app._clock_tick()
            await pilot.pause()
            self.assertEqual(
                [id(item) for item in app.query(NavigationItem)], [id(item) for item in navigation]
            )
            self.assertEqual(len(log.lines), line_count)
            self.assertEqual(engine.full_samples, 0)
            self.assertEqual(engine.event_samples, 0)
            inspector = app.query_one(SessionInspector)
            with (
                patch.object(inspector, "show_session", wraps=inspector.show_session) as show,
                patch.object(log, "clear", wraps=log.clear) as clear,
                patch.object(log, "write", wraps=log.write) as write,
            ):
                await app._clock_tick()
                await pilot.pause()

            show.assert_not_called()
            clear.assert_not_called()
            write.assert_not_called()

    async def test_navigation_rebuild_handles_unmounted_and_concurrent_requests(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)):
            with patch.object(app, "query_one", side_effect=NoMatches("screen is unmounted")):
                await app._rebuild_navigation()

            self.assertFalse(app.rebuilding)
            app.rebuilding = True
            await app._rebuild_navigation()
            self.assertTrue(app.navigation_dirty)

    async def test_unchanged_fast_samples_do_not_update_widgets(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(72, 24)) as pilot:
            await pilot.pause()
            inspector = app.query_one(SessionInspector)
            status = app.query_one("#status-line", Static)
            with (
                patch.object(inspector, "show_session", wraps=inspector.show_session) as show,
                patch.object(status, "update", wraps=status.update) as update,
            ):
                for _ in range(100):
                    app._finish_sample(snapshot, "")
                await pilot.pause()

            show.assert_not_called()
            update.assert_not_called()

    async def test_wide_layout_header_and_inspector_refresh(self) -> None:
        snapshot = make_snapshot()
        snapshot.generated_at = "2000-01-01T03:04:05+00:00"
        engine = FakeEngine(snapshot)
        app = CodexDeckApp(engine, snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(len(app.query(NavigationItem)), 4)
            title = str(app.query_one("#session-title", Static).render())
            self.assertIn("Session 0", title)
            self.assertEqual(engine.pinned, snapshot.sessions[0])
            self.assertFalse(app.screen.has_class("compact"))
            header = str(app.query_one("#app-header", Static).render())
            self.assertIn("SESSIONS 3", header)
            self.assertIn("HIDDEN 0", header)
            self.assertIn("ISSUES 0", header)
            self.assertNotIn("03:04:05", header)
            self.assertNotIn("SAMPLE", header)

            refreshed = make_snapshot(1)
            refreshed.sessions[0].process.session_title = "Refreshed session"
            app._apply_snapshot(refreshed)
            await pilot.pause()
            self.assertEqual(len(app.query(NavigationItem)), 2)
            self.assertIn(
                "Refreshed session",
                str(app.query_one("#session-title", Static).render()),
            )

    async def test_inspector_header_exposes_incomplete_state_axes(self) -> None:
        snapshot = make_snapshot(1)
        snapshot.sessions[0].completeness = SessionCompleteness(
            lifecycle=AxisCompleteness("lifecycle", complete=False),
            attention=AxisCompleteness("attention", complete=False),
        )
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            health = str(app.query_one("#health-strip", Static).render())
            self.assertIn("证据不完整", health)
            self.assertIn("不完整 lifecycle,attention", health)

    async def test_navigation_hides_exited_and_confirmed_background_sessions(self) -> None:
        snapshot = make_snapshot(3)
        foreground, background, exited = snapshot.sessions
        foreground.process.process_group_id = 1000
        foreground.process.foreground_process_group_id = 1000
        foreground.process.terminal = "pts/1"
        background.process.process_group_id = 1001
        background.process.foreground_process_group_id = 2001
        background.process.terminal = "pts/2"
        exited.process_exited = True
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            rows = [item for item in app.query(NavigationItem) if item.kind == "session"]
            header = str(app.query_one("#app-header", Static).render())

            self.assertEqual([item.session_key for item in rows], [foreground.key])
            self.assertEqual(app.selected_session.key, foreground.key)
            self.assertIn("SESSIONS 1", header)
            self.assertIn("HIDDEN 2", header)
            self.assertIn("VIEW ACTIVE", header)

            await pilot.press("h")
            await pilot.pause()
            rows = [item for item in app.query(NavigationItem) if item.kind == "session"]
            rendered_rows = [str(item.query_one(Static).render()) for item in rows]
            header = str(app.query_one("#app-header", Static).render())

            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0].session_key, foreground.key)
            self.assertTrue(any("BG" in row for row in rendered_rows))
            self.assertTrue(any("EXITED" in row for row in rendered_rows))
            self.assertIn("VIEW ALL", header)

            hidden_row = next(item for item in rows if item.session_key == background.key)
            app.query_one("#session-list", ListView).index = list(
                app.query_one("#session-list", ListView).children
            ).index(hidden_row)
            app._select_item(hidden_row)
            self.assertEqual(app.selected_session.key, background.key)

            await pilot.press("h")
            await pilot.pause()
            rows = [item for item in app.query(NavigationItem) if item.kind == "session"]
            self.assertEqual([item.session_key for item in rows], [foreground.key])
            self.assertEqual(app.selected_session.key, foreground.key)

    async def test_refresh_preserves_log_scroll_focus_and_navigation_widgets(self) -> None:
        snapshot = make_snapshot(1)
        snapshot.sessions[0].events = [
            NormalizedEvent(float(index), "WARNING", f"Event {index}", "detail")
            for index in range(80)
        ]
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            log = app.query_one("#activity-panel", RichLog)
            log.focus()
            log.scroll_to(y=8, animate=False, immediate=True)
            await pilot.pause()
            self.assertFalse(log.is_vertical_scroll_end)
            previous_scroll_y = log.scroll_y
            previous_navigation = tuple(app.query(NavigationItem))

            refreshed = make_snapshot(1)
            refreshed.sessions[0].events = [
                NormalizedEvent(float(index), "WARNING", f"Event {index}", "detail")
                for index in range(81)
            ]
            app._apply_snapshot(refreshed)
            await pilot.pause()

            self.assertEqual(log.scroll_y, previous_scroll_y)
            self.assertIs(app.focused, log)
            self.assertEqual(tuple(app.query(NavigationItem)), previous_navigation)
            self.assertFalse(log.is_vertical_scroll_end)

            await pilot.resize_terminal(100, 24)
            await pilot.pause(0.1)
            self.assertEqual(log.scroll_x, 0)
            self.assertEqual(log.scroll_y, previous_scroll_y)
            self.assertFalse(log.show_horizontal_scrollbar)
            self.assertFalse(log.is_vertical_scroll_end)

            log.scroll_end(animate=False, immediate=True, x_axis=False)
            await pilot.pause()
            self.assertTrue(log.is_vertical_scroll_end)
            await pilot.resize_terminal(96, 24)
            await pilot.pause(0.1)
            self.assertEqual(log.scroll_x, 0)
            self.assertTrue(log.is_vertical_scroll_end)

    async def test_activity_reflows_after_hidden_update_and_resize(self) -> None:
        snapshot = make_snapshot(1)
        snapshot.sessions[0].events = [
            NormalizedEvent(
                float(index),
                "WARNING",
                f"Visible activity event {index}",
                "detail remains readable after layout changes",
            )
            for index in range(20)
        ]
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            log = app.query_one("#activity-panel", RichLog)

            await pilot.press("2")
            await pilot.pause()
            refreshed = make_snapshot(1)
            refreshed.sessions[0].events = [
                *snapshot.sessions[0].events,
                NormalizedEvent(
                    21.0,
                    "WARNING",
                    "Visible activity event 21",
                    "latest hidden-tab update",
                ),
            ]
            app._apply_snapshot(refreshed)
            await pilot.pause()

            await pilot.press("1")
            await pilot.pause()
            visible = "\n".join(log.render_line(row).text for row in range(log.size.height))
            self.assertIn("Visible activity event 21", visible)

            await pilot.resize_terminal(50, 20)
            await pilot.pause(0.1)
            await pilot.resize_terminal(120, 30)
            await pilot.pause(0.1)
            visible = "\n".join(log.render_line(row).text for row in range(log.size.height))
            self.assertIn("EVENT", visible)
            self.assertIn("Visible activity event 21", visible)
            self.assertNotEqual(
                {line.strip() for line in visible.splitlines() if line.strip()},
                {"..."},
            )

    async def test_session_row_and_health_refresh_in_place(self) -> None:
        snapshot = make_snapshot(1)
        session = snapshot.sessions[0]
        session.lifecycle = LifecycleState.WAITING_RESPONSE
        session.phase = "请求已发送"
        session.alert = "PRE_REQUEST_STALL"
        session.alert_level = "警告"
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            row = next(item for item in app.query(NavigationItem) if item.kind == "session")
            runtime = app.query_one("#health-strip", Static)
            previous_navigation = tuple(app.query(NavigationItem))
            self.assertIn("请求已发送", str(row.query_one(Static).render()))
            self.assertIn("gpt-test", str(runtime.render()))
            self.assertEqual(session_marker(session)[0], "!")

            refreshed = make_snapshot(1)
            refreshed_session = refreshed.sessions[0]
            refreshed_session.lifecycle = LifecycleState.GENERATING
            refreshed_session.phase = "模型正在生成"
            refreshed_session.process.model = "gpt-refreshed"
            app._apply_snapshot(refreshed)
            await pilot.pause()

            self.assertIn("模型正在生成", str(row.query_one(Static).render()))
            self.assertNotIn("等待警告", str(row.query_one(Static).render()))
            self.assertIn("gpt-refreshed", str(runtime.render()))
            self.assertEqual(tuple(app.query(NavigationItem)), previous_navigation)

    async def test_sample_result_and_collector_error_follow_publication_lifecycle(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 24)) as pilot:
            refreshed = make_snapshot(1)
            refreshed.sessions[0].process.model = "gpt-worker-result"
            app.post_message(SampleCompleted(refreshed))
            await pilot.pause()

            self.assertIs(app.snapshot, refreshed)
            self.assertIn(
                "gpt-worker-result",
                str(app.query_one("#health-strip", Static).render()),
            )
            app._show_collector_error("socket probe failed")
            await app._clock_tick()
            self.assertIn(
                "COLLECTOR ERROR  socket probe failed",
                str(app.query_one("#status-line", Static).render()),
            )

            app._finish_sample(snapshot, "")
            await pilot.pause()
            self.assertNotIn(
                "COLLECTOR ERROR",
                str(app.query_one("#status-line", Static).render()),
            )

    def test_session_status_and_markers_preserve_live_phase(self) -> None:
        session = make_snapshot(1).sessions[0]
        session.phase = "模型正在生成"
        session.alert = "PRE_REQUEST_STALL"
        session.alert_level = "警告"

        self.assertEqual(session_status(session), "模型正在生成")
        self.assertEqual(session_marker(session)[0], "!")

        session.alert = None
        session.alert_level = ""
        session.lifecycle = LifecycleState.COMPACTING
        session.phase = "正在压缩上下文"

        self.assertEqual(session_status(session), "正在压缩上下文")
        self.assertEqual(session_marker(session)[0], "C")

        session.lifecycle = LifecycleState.RUNNING_TOOL
        session.phase = "工具正在运行"
        session.current_operation = CurrentOperationSummary(
            "write",
            "写入文件",
            "workspace-a/result.txt",
        )

        self.assertEqual(session_status(session), "工具正在运行 · 写入文件")

        session.protocol_uncertain = True
        session.phase = "协议状态不确定"
        self.assertEqual(session_status(session), "协议状态不确定")
        self.assertEqual(session_marker(session)[0], "?")

        session.protocol_uncertain = False
        session.completeness = SessionCompleteness(
            lifecycle=AxisCompleteness("lifecycle", complete=False)
        )
        self.assertEqual(
            session_status(session),
            "协议状态不确定 · 写入文件 · 证据不完整",
        )
        self.assertEqual(session_marker(session)[0], "?")

    def test_state_symbols_remain_distinct_without_color(self) -> None:
        session = make_snapshot(1).sessions[0]
        session.attention = AttentionState.APPROVAL
        session.attention_request = AttentionRequest(AttentionState.APPROVAL)
        self.assertEqual(session_marker(session)[0], "?")

        session.attention_request = None
        session.current_failure = FailureInfo("failure", "failed")
        self.assertEqual(session_marker(session)[0], "×")

        session.current_failure = None
        session.recovery = RecoveryState.RECONNECTING
        self.assertEqual(session_marker(session)[0], "↻")

        session.recovery = RecoveryState.NONE
        session.network.state = NetworkState.STALLED
        self.assertEqual(session_marker(session)[0], "!")

        session.process_exited = True
        session.events.append(NormalizedEvent(30.0, "SESSION_CLOSED", "会话已由 /new 关闭"))
        self.assertEqual(session_hidden_label(session), "CLOSED")

    async def test_responsive_breakpoints_rows_and_terminal_floor(self) -> None:
        for size, compact in (
            ((120, 30), False),
            ((96, 24), False),
            ((80, 24), True),
            ((60, 20), True),
        ):
            with self.subTest(size=size):
                snapshot = make_snapshot(1)
                snapshot.sessions[0].current_operation = CurrentOperationSummary(
                    "shell",
                    "exec",
                    "pytest tests/test_core.py with a deliberately long suffix",
                    None,
                    tool_count=1,
                    file_count=2,
                )
                if size in {(80, 24), (60, 20)}:
                    terminal = TerminalSessionSummary(
                        "terminal-floor",
                        process_id="700",
                        status="running",
                        process_active=True,
                        capability=TerminalCapability.POLL_TRANSCRIPT,
                        chunks=(TerminalChunk("floor", 1.0, text="visible output\n"),),
                    )
                    snapshot.sessions[0].terminal_sessions = [terminal]
                    if size == (60, 20):
                        snapshot.sessions[0].terminal_sessions.append(
                            replace(terminal, terminal_id="terminal-floor-2")
                        )
                app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    self.assertEqual(app.screen.has_class("compact"), compact)
                    self.assertEqual(app.query_one("#app-header", Static).size.height, 1)
                    self.assertEqual(app.query_one("#status-line", Static).size.height, 1)
                    self.assertEqual(app.query_one(ShortcutFooter).size.height, 1)
                    row = next(item for item in app.query(NavigationItem) if item.kind == "session")
                    self.assertLessEqual(row.size.height, 2)

                    if size in {(80, 24), (60, 20)}:
                        await pilot.press("enter", "3")
                        await pilot.pause()
                        self.assertGreater(
                            app.query_one("#terminal-output", RichLog).size.height,
                            0,
                        )
                        self.assertEqual(
                            app.query_one("#terminal-list", DataTable).display,
                            size == (60, 20),
                        )

        small_snapshot = make_snapshot()
        small_app = CodexDeckApp(FakeEngine(small_snapshot), small_snapshot, sampling=False)
        async with small_app.run_test(size=(40, 10)) as pilot:
            await pilot.pause()
            self.assertTrue(small_app.screen.has_class("too-small"))
            self.assertIn(
                "终端尺寸过小",
                str(small_app.query_one("#too-small", Static).render()),
            )

    async def test_default_groups_sessions_by_workspace_with_home_context(self) -> None:
        snapshot = make_snapshot(2)
        snapshot.sessions[0].process.cwd = "/workspace/project-a"
        snapshot.sessions[1].process.cwd = "/workspace/project-b"
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            groups = [item for item in app.query(NavigationItem) if item.kind == "workspace"]
            self.assertEqual(len(groups), 2)
            labels = "\n".join(str(item.query_one(Static).render()) for item in groups)
            self.assertIn("/workspace/project-a", labels)
            self.assertIn("/workspace/project-b", labels)
            self.assertIn("CODEX_HOME CODEX_HOME", labels)

    async def test_tabs_controls_and_search_are_application_managed(self) -> None:
        snapshot = make_snapshot()
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("2")
            self.assertEqual(
                app.query_one("#detail-content", ContentSwitcher).current,
                "diagnosis-panel",
            )
            await pilot.press("?")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 2)
            controls = "\n".join(str(item.render()) for item in app.screen.query(".control-detail"))
            self.assertIn("Terminal 搜索时跳到下一个匹配", controls)
            self.assertEqual(len(app.screen.query("#controls-runtime-strip")), 0)
            self.assertEqual(len(app.screen.query("#controls-runtime")), 0)
            await pilot.press("escape")
            search = app.query_one("#search", Input)
            search.value = "Session 2"
            await pilot.pause()
            self.assertEqual(len(app.query(NavigationItem)), 2)

        narrow_snapshot = make_snapshot(1)
        narrow = CodexDeckApp(FakeEngine(narrow_snapshot), narrow_snapshot, sampling=False)
        async with narrow.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            await pilot.press("?")
            await pilot.pause()
            dialog = narrow.screen.query_one("#controls-dialog")
            scroll = narrow.screen.query_one("#controls-scroll")
            self.assertLessEqual(dialog.size.width, 56)
            self.assertGreater(scroll.size.height, 0)
            self.assertGreater(scroll.virtual_size.height, scroll.size.height)
            await pilot.press("escape")
            self.assertEqual(len(narrow.screen_stack), 1)

    async def test_zooming_navigation_keeps_the_session_list_visible(self) -> None:
        snapshot = make_snapshot(2)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            self.assertFalse(app.ENABLE_COMMAND_PALETTE)
            await pilot.press("ctrl+p")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)

            initial_theme = app.theme
            await pilot.press("t")
            await pilot.pause()
            self.assertNotEqual(app.theme, initial_theme)
            self.assertIn("THEME", str(app.query_one("#status-line", Static).render()))

            session_list = app.query_one("#session-list", ListView)
            session_list.focus()
            await pilot.press("z")
            await pilot.pause()

            self.assertEqual(app.zoom_mode, "navigation")
            self.assertTrue(app.screen.has_class("zoom-navigation"))
            self.assertGreater(app.query_one("#navigation").size.width, 80)
            self.assertGreater(session_list.size.height, 0)
            self.assertFalse(app.query_one("#inspector").display)

            await pilot.press("z")
            await pilot.pause()
            self.assertEqual(app.zoom_mode, "")
            self.assertTrue(app.query_one("#inspector").display)

            await pilot.press("2")
            app.query_one("#diagnosis-panel").focus()
            await pilot.press("z")
            await pilot.pause()
            self.assertEqual(app.zoom_mode, "inspector")
            self.assertTrue(app.screen.has_class("zoom-inspector"))
            self.assertGreater(app.query_one("#inspector").size.width, 0)
            self.assertIn("ZOOM", str(app.query_one("#status-line", Static).render()))
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.zoom_mode, "")
            self.assertFalse(app.screen.has_class("zoom-inspector"))

            session_list.focus()
            await pilot.press("z")
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertTrue(app.compact)
            self.assertEqual(app.zoom_mode, "")
            self.assertFalse(app.screen.has_class("zoom-navigation"))

    async def test_footer_changes_with_focus_and_active_page(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            footer = app.query_one(ShortcutFooter)
            rendered = " ".join(str(item.render()) for item in footer.query(".shortcut-key"))
            self.assertIn("Enter 打开", rendered)
            self.assertIn("SESSIONS", str(footer.query_one(".shortcut-mode").render()))
            quit_key = footer.query_one(".shortcut-quit")
            self.assertEqual(str(quit_key.render()), "q 退出")
            self.assertEqual(quit_key.region.right, footer.region.right)

            await pilot.press("3")
            await pilot.pause()
            rendered = " ".join(str(item.render()) for item in footer.query(".shortcut-key"))
            self.assertIn("n/N 匹配", rendered)
            self.assertNotIn("palette", rendered.lower())
            self.assertEqual(len(footer.query(".shortcut-quit")), 1)

            help_key = next(
                item
                for item in footer.query(".shortcut-key")
                if getattr(item, "trigger", "") == "question_mark"
            )
            await pilot.click(help_key)
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press("escape")

    async def test_terminal_tab_shows_read_only_transcript_and_preserves_scroll(self) -> None:
        snapshot = make_snapshot(1)
        chunks = tuple(
            TerminalChunk(
                f"source-{index}",
                float(index + 1),
                stream="stderr" if index == 20 else "stdout",
                text=f"line {index}\n",
                sequence=index,
            )
            for index in range(40)
        )
        snapshot.sessions[0].terminal_sessions = [
            TerminalSessionSummary(
                "terminal-1",
                process_id="777",
                command="server --watch",
                cwd="/work/repository",
                status="running",
                process_active=True,
                capability=TerminalCapability.POLL_TRANSCRIPT,
                association_status="ambiguous",
                correlation_source="call_id",
                retained_bytes=320,
                chunks=chunks,
            )
        ]
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("3")
            await pilot.pause()

            self.assertEqual(
                app.query_one("#detail-content", ContentSwitcher).current,
                "terminal-panel",
            )
            self.assertEqual(app.query_one("#terminal-list", DataTable).row_count, 1)
            header = str(app.query_one("#terminal-header", Static).render())
            self.assertIn("READ ONLY", header)
            self.assertIn("POLL", header)
            self.assertIn("AMBIGUOUS/call_id", header)
            self.assertNotIn("server --watch", header)
            log = app.query_one("#terminal-output", RichLog)
            rendered = "\n".join(line.text for line in log.lines)
            self.assertIn("/work/repository $ server --watch", rendered)
            self.assertIn("OUT │ line 0", rendered)
            self.assertIn("ERR │ line 20", rendered)
            self.assertIn("line 20", rendered)
            self.assertNotRegex(rendered, r"\d{2}:\d{2}:\d{2}")

            await pilot.press("enter")
            log.scroll_to(y=5, animate=False, immediate=True)
            await pilot.pause()
            previous_scroll = log.scroll_y
            selected = app.selected_session
            await pilot.press("j")
            self.assertGreaterEqual(log.scroll_y, previous_scroll)
            self.assertIs(app.selected_session, selected)
            await pilot.press("k")
            self.assertIs(app.selected_session, selected)
            app.follow = False
            refreshed = make_snapshot(1)
            refreshed.sessions[0].terminal_sessions = [
                replace(
                    snapshot.sessions[0].terminal_sessions[0],
                    chunks=chunks
                    + (TerminalChunk("source-new", 99.0, text="new line\n", sequence=99),),
                )
            ]
            app._apply_snapshot(refreshed)
            await pilot.pause()

            self.assertEqual(log.scroll_y, previous_scroll)
            self.assertIs(app.focused, log)
            await pilot.press("/")
            self.assertTrue(app.query_one("#terminal-search", Input).has_focus)
            await pilot.press("l", "i", "n", "e", "enter")
            self.assertEqual(app.query_one("#terminal-search", Input).value, "line")
            self.assertIn("MATCH 1/", str(app.query_one("#terminal-header", Static).render()))
            first_match_scroll = log.scroll_y
            await pilot.press("n")
            self.assertGreaterEqual(log.scroll_y, first_match_scroll)

    async def test_terminal_tab_only_shows_current_background_processes(self) -> None:
        self.assertEqual(
            TerminalPanel.CAPABILITY_LABELS[TerminalCapability.STREAMING],
            "RESERVED",
        )
        snapshot = make_snapshot(1)
        running = TerminalSessionSummary(
            "running-terminal",
            process_id="777",
            command="npm run dev",
            cwd="/workspace-a",
            status="running",
            process_active=True,
            capability=TerminalCapability.POLL_TRANSCRIPT,
            chunks=(TerminalChunk("running", 1.0, text="ready\n"),),
        )
        completed = TerminalSessionSummary(
            "completed-terminal",
            process_id="778",
            command="pytest",
            status="completed",
            capability=TerminalCapability.FINAL_TRANSCRIPT,
            chunks=(TerminalChunk("completed", 1.0, text="passed\n"),),
        )
        stale = replace(running, terminal_id="stale-terminal", process_id="779", stale=True)
        unconfirmed = replace(
            running,
            terminal_id="unconfirmed-terminal",
            process_id="780",
            process_active=False,
        )
        snapshot.sessions[0].terminal_sessions = [completed, running, stale, unconfirmed]
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("3")
            await pilot.pause()

            table = app.query_one("#terminal-list", DataTable)
            self.assertEqual(table.row_count, 1)
            header = str(app.query_one("#terminal-header", Static).render())
            self.assertIn("PID 777", header)
            self.assertNotIn("npm run dev", header)
            self.assertNotIn("pytest", header)
            rendered = "\n".join(
                line.text for line in app.query_one("#terminal-output", RichLog).lines
            )
            self.assertIn("/workspace-a $ npm run dev", rendered)
            self.assertIn("TTY │ ready", rendered)
            self.assertNotIn("passed", rendered)

            refreshed = make_snapshot(1)
            refreshed.sessions[0].terminal_sessions = [
                replace(running, status="completed", exit_code=0)
            ]
            app._apply_snapshot(refreshed)
            await pilot.pause()

            self.assertEqual(table.row_count, 0)
            self.assertIn(
                "当前没有运行中的后台进程",
                str(app.query_one("#terminal-header", Static).render()),
            )
            self.assertIn(
                "当前没有运行中的后台进程",
                "\n".join(line.text for line in app.query_one("#terminal-output", RichLog).lines),
            )

    async def test_terminal_prompt_keeps_complete_long_command_on_narrow_screen(self) -> None:
        snapshot = make_snapshot(1)
        command = (
            "python -m worker --config /workspace-a/config/production.toml "
            "--queue background-jobs --concurrency 12 --log-level debug"
        )
        snapshot.sessions[0].terminal_sessions = [
            TerminalSessionSummary(
                "terminal-long-command",
                process_id="777",
                command=command,
                cwd="/workspace-a",
                status="running",
                process_active=True,
                capability=TerminalCapability.POLL_TRANSCRIPT,
                chunks=(TerminalChunk("running", 1.0, text="ready\n"),),
            )
        ]
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(72, 24)) as pilot:
            await pilot.press("3")
            await pilot.pause()

            log = app.query_one("#terminal-output", RichLog)
            self.assertTrue(log.wrap)
            rendered = "\n".join(line.text for line in log.lines)
            visible = "\n".join(
                log.render_line(row).text.rstrip() for row in range(log.size.height)
            )
            self.assertIn(f"/workspace-a $ {command}", rendered.replace("\n", ""))
            self.assertNotIn("…", rendered)
            self.assertIn("/workspace-a $ python -m worker --config", visible)
            self.assertIn("/workspace-a/config/production.toml --queue background-jobs", visible)
            self.assertIn("--concurrency 12 --log-level debug", visible)
            self.assertIn("TTY │ ready", visible)
            self.assertLessEqual(log.virtual_size.height, log.size.height)

    async def test_terminal_reflows_after_hidden_tab_and_resize(self) -> None:
        snapshot = make_snapshot(1)
        command = "python -m worker --config /workspace-a/config.toml"
        snapshot.sessions[0].terminal_sessions = [
            TerminalSessionSummary(
                "terminal-reflow",
                root_call_id="call-reflow",
                process_id="17",
                command=command,
                cwd="/workspace-a",
                status="running",
                process_active=True,
                capability=TerminalCapability.POLL_TRANSCRIPT,
                chunks=(TerminalChunk("chunk-1", 10.0, "stdout", "ready\n", 1),),
            )
        ]
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause(0.1)
            log = app.query_one("#terminal-output", RichLog)
            visible = "\n".join(log.render_line(row).text for row in range(log.size.height))
            self.assertIn("python -m worker --config", visible)
            self.assertGreaterEqual(log.virtual_size.width, 24)

            await pilot.resize_terminal(50, 20)
            await pilot.pause(0.1)
            await pilot.resize_terminal(120, 30)
            await pilot.pause(0.1)
            visible = "\n".join(log.render_line(row).text for row in range(log.size.height))
            self.assertIn("python -m worker --config", visible)
            self.assertIn("OUT │ ready", visible)
            self.assertGreaterEqual(log.virtual_size.width, 24)

    async def test_removed_display_shortcuts_have_no_actions(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexDeckApp(FakeEngine(snapshot), snapshot, sampling=False)

        keys = {binding.key for binding in app.BINDINGS}
        self.assertNotIn("comma", keys)
        self.assertNotIn("a", keys)
        self.assertNotIn("o", keys)
        self.assertNotIn("x", keys)
        self.assertNotIn("ctrl+p", keys)

        async with app.run_test(size=(120, 36)) as pilot:
            grouped = app.grouped
            await pilot.press(",", "a")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.grouped, grouped)

    def test_help_and_readme_follow_application_bindings(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        reference = keyboard_reference()
        for binding in CodexDeckApp.BINDINGS:
            label = binding_key_label(binding.key)
            self.assertIn(label, reference)
            self.assertIn(f"`{label}`", readme)
        for removed in ("Operational", "Diagnostic", "显示设置", "辅助进程"):
            self.assertNotIn(removed, reference)

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
        self.assertEqual([_kind(item) for item in entries], ["TURN_FAILED"])

        session.current_failure = None
        session.events = [NormalizedEvent(20.0, "KEEPALIVE", "收到 keepalive")]
        self.assertEqual([_kind(item) for item in timeline_entries(session)], [])


def _kind(item: object) -> str:
    return str(item.get("kind", "")) if isinstance(item, dict) else str(item.kind)


if __name__ == "__main__":
    unittest.main()
