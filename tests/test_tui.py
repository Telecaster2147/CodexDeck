from __future__ import annotations

import io
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from rich.console import Console

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
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
    TerminalCapability,
    TerminalChunk,
    TerminalSessionSummary,
    TokenUsageSummary,
    UnparsedPayload,
)
from presentation.tui.textual_app import (  # noqa: E402
    CodexNetApp,
    NavigationItem,
    SampleCompleted,
    TerminalPanel,
    _diagnosis_renderable,
    _timeline_line,
    binding_key_label,
    keyboard_reference,
    session_marker,
    session_status,
    timeline_entries,
)
from textual.widgets import (  # noqa: E402
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    ListView,
    RichLog,
    Static,
)


def render_plain(renderable: object, width: int = 120) -> str:
    output = io.StringIO()
    Console(width=width, file=output, color_system=None).print(renderable)
    return output.getvalue()


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
        self.full_samples = 0
        self.event_samples = 0

    def pin_session(self, session: SessionHealth | None) -> None:
        self.pinned = session

    def sample(self) -> MonitorSnapshot:
        self.full_samples += 1
        return self.snapshot

    def refresh_events(self, snapshot: MonitorSnapshot) -> MonitorSnapshot:
        self.event_samples += 1
        return snapshot


class TextualTuiTests(unittest.IsolatedAsyncioTestCase):
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
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            sessions = [item for item in app.query(NavigationItem) if item.kind == "session"]
            self.assertEqual(sessions[0].session_key, waiting.key)
            self.assertIn(
                "ATTENTION · Approve command",
                str(sessions[0].query_one(Static).render()),
            )
            app.selected_session = snapshot.sessions[0]
            await pilot.press("]")
            self.assertIs(app.selected_session, waiting)
            selected = app.selected_session
            await pilot.press("tab")
            self.assertIs(app.selected_session, selected)
            self.assertIsNot(app.focused, app.query_one("#session-list", ListView))

    async def test_new_attention_emits_cross_session_notification(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)
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
        self.assertIn("数据质量", rendered)
        self.assertNotIn("自动 compact 边界", rendered)
        self.assertNotIn("config.toml", rendered)
        self.assertNotIn("历史趋势", rendered)
        self.assertNotIn("15m", rendered)
        self.assertNotIn("p50", rendered)

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
            ["COMPACT_COMPLETED"],
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

        session.events.append(
            NormalizedEvent(12, "COMPACT_FAILED", "failed", turn_id="turn")
        )
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
            'const result = await tools.exec_command({"cmd":"uv run tests"});\n'
            'text(result.output);'
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
        app = CodexNetApp(engine, snapshot, sampling=False)
        samples: list[bool] = []
        app._start_sample = lambda *, full: samples.append(full)  # type: ignore[method-assign]

        app.sampling = True
        app._poll_live_events()

        self.assertEqual(samples, [False])
        self.assertEqual(engine.full_samples, 0)
        self.assertEqual(engine.event_samples, 0)

    async def test_clock_tick_only_updates_existing_widgets_and_timeline_age(self) -> None:
        snapshot = make_snapshot(1)
        snapshot.sessions[0].events = [NormalizedEvent(10, "MODEL_PROGRESS", "progress")]
        engine = FakeEngine(snapshot)
        app = CodexNetApp(engine, snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            navigation = list(app.query(NavigationItem))
            log = app.query_one("#activity-panel", RichLog)
            line_count = len(log.lines)
            await app._clock_tick()
            await pilot.pause()
            self.assertEqual([id(item) for item in app.query(NavigationItem)], [
                id(item) for item in navigation
            ])
            self.assertEqual(len(log.lines), line_count)
            self.assertEqual(engine.full_samples, 0)
            self.assertEqual(engine.event_samples, 0)

    async def test_wide_layout_header_and_inspector_refresh(self) -> None:
        snapshot = make_snapshot()
        snapshot.generated_at = "2000-01-01T03:04:05+00:00"
        engine = FakeEngine(snapshot)
        app = CodexNetApp(engine, snapshot, sampling=False)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(len(app.query(NavigationItem)), 4)
            title = str(app.query_one("#session-title", Static).render())
            self.assertIn("Session 0", title)
            self.assertEqual(engine.pinned, snapshot.sessions[0])
            self.assertFalse(app.screen.has_class("compact"))
            header = str(app.query_one("#app-header", Static).render())
            self.assertIn("SESSIONS 3", header)
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

    async def test_refresh_preserves_log_scroll_focus_and_navigation_widgets(self) -> None:
        snapshot = make_snapshot(1)
        snapshot.sessions[0].events = [
            NormalizedEvent(float(index), "WARNING", f"Event {index}", "detail")
            for index in range(80)
        ]
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

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

    async def test_session_row_and_health_refresh_in_place(self) -> None:
        snapshot = make_snapshot(1)
        session = snapshot.sessions[0]
        session.lifecycle = LifecycleState.WAITING_RESPONSE
        session.phase = "请求已发送"
        session.alert = "PRE_REQUEST_STALL"
        session.alert_level = "警告"
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

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

    async def test_sample_completed_message_applies_worker_result(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

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

    async def test_collector_error_survives_clock_tick_until_successful_sample(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 24)) as pilot:
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

    async def test_navigation_rebuild_coalesces_concurrent_requests(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(120, 24)):
            app.rebuilding = True
            await app._rebuild_navigation()
            self.assertTrue(app.navigation_dirty)

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

    async def test_compact_layout_drilldown_and_command_palette(self) -> None:
        snapshot = make_snapshot()
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertTrue(app.screen.has_class("compact"))
            self.assertIn("SESSIONS 3", str(app.query_one("#app-header", Static).render()))
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(app.screen.has_class("detail-open"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.screen.has_class("detail-open"))
            commands = list(app.get_system_commands(app.screen))
            self.assertNotIn("Screenshot", {command.title for command in commands})

    async def test_responsive_breakpoints_rows_and_terminal_floor(self) -> None:
        for size, compact in (((120, 30), False), ((96, 24), False), ((80, 24), True), ((60, 20), True)):
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
                        capability=TerminalCapability.POLL_TRANSCRIPT,
                        chunks=(TerminalChunk("floor", 1.0, text="visible output\n"),),
                    )
                    snapshot.sessions[0].terminal_sessions = [terminal]
                    if size == (60, 20):
                        snapshot.sessions[0].terminal_sessions.append(
                            replace(terminal, terminal_id="terminal-floor-2")
                        )
                app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    self.assertEqual(app.screen.has_class("compact"), compact)
                    self.assertEqual(app.query_one("#app-header", Static).size.height, 1)
                    self.assertEqual(app.query_one("#status-line", Static).size.height, 1)
                    self.assertEqual(app.query_one(Footer).size.height, 1)
                    row = next(
                        item for item in app.query(NavigationItem) if item.kind == "session"
                    )
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
        small_app = CodexNetApp(FakeEngine(small_snapshot), small_snapshot, sampling=False)
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
                "diagnosis-panel",
            )
            await pilot.press("?")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press("escape")
            search = app.query_one("#search", Input)
            search.value = "Session 2"
            await pilot.pause()
            self.assertEqual(len(app.query(NavigationItem)), 2)

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
                capability=TerminalCapability.POLL_TRANSCRIPT,
                retained_bytes=320,
                chunks=chunks,
            )
        ]
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

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
        snapshot.sessions[0].terminal_sessions = [completed, running, stale]
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

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
                "\n".join(
                    line.text for line in app.query_one("#terminal-output", RichLog).lines
                ),
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
                capability=TerminalCapability.POLL_TRANSCRIPT,
                chunks=(TerminalChunk("running", 1.0, text="ready\n"),),
            )
        ]
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        async with app.run_test(size=(72, 24)) as pilot:
            content = app.query_one("#detail-content", ContentSwitcher)
            await pilot.press("o")
            await pilot.pause()
            self.assertEqual(content.current, "activity-panel")

            await pilot.press("3")
            await pilot.pause()

            log = app.query_one("#terminal-output", RichLog)
            self.assertTrue(log.wrap)
            rendered = "\n".join(line.text for line in log.lines)
            self.assertIn(f"/workspace-a $ {command}", rendered.replace("\n", ""))
            self.assertNotIn("…", rendered)

    async def test_removed_display_shortcuts_have_no_actions(self) -> None:
        snapshot = make_snapshot(1)
        app = CodexNetApp(FakeEngine(snapshot), snapshot, sampling=False)

        keys = {binding.key for binding in app.BINDINGS}
        self.assertNotIn("comma", keys)
        self.assertNotIn("a", keys)

        async with app.run_test(size=(120, 36)) as pilot:
            grouped = app.grouped
            await pilot.press(",", "a")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.grouped, grouped)

    def test_help_and_readme_follow_application_bindings(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        reference = keyboard_reference()
        for binding in CodexNetApp.BINDINGS:
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
