from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    AgentNode,
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
    RateLimitSummary,
    RateLimitWindow,
    SessionHealth,
    TokenUsageSummary,
    ToolExecutionSummary,
    TurnSummary,
)
from presentation.json_output import render_json  # noqa: E402
from presentation.text import render_text  # noqa: E402
from presentation.tui.terminal import emit_frame, visible_width  # noqa: E402
from presentation.tui.views import (  # noqa: E402
    detail_scroll_limit,
    detail_view,
    help_view,
    main_view,
)


def snapshot() -> MonitorSnapshot:
    home = Path("/home/test/.codex")
    paths = CodexPaths(
        home,
        home,
        home / "state_5.sqlite",
        home / "logs_2.sqlite",
        home / "session_index.jsonl",
        home / "sessions",
    )
    session_process = ProcessInfo(
        ProcessIdentity(42, 10), 1, "codex", 1, 0.0, "S", "futex", "codex", "session",
        instance_id="i1", session_id="s1", session_title="Active session", model="gpt-test",
    )
    auxiliary = ProcessInfo(
        ProcessIdentity(43, 11), 1, "node", 1, 0.0, "S", "futex", "node codex", "launcher",
        instance_id="i1",
    )
    health = SessionHealth(
        "i1",
        "s1",
        session_process,
        LifecycleState.GENERATING,
        network=NetworkEvidence(NetworkState.ACTIVE),
    )
    instance = InstanceSnapshot(
        "i1",
        paths,
        "~/.codex",
        "~/.codex",
        "environment",
        processes=[session_process, auxiliary],
        sessions=[health],
    )
    return MonitorSnapshot("2026-07-15T00:00:00+08:00", 2.0, [instance])


def snapshot_with_metrics() -> MonitorSnapshot:
    result = snapshot()
    session = result.sessions[0]
    usage = TokenUsageSummary(
        input_tokens=400,
        cached_input_tokens=100,
        output_tokens=80,
        reasoning_output_tokens=20,
        total_tokens=480,
        context_tokens=1200,
        context_window=4000,
    )
    tool = ToolExecutionSummary(
        "call-1",
        turn_id="turn-1",
        display_name="shell",
        duration_seconds=1.5,
        status="completed",
    )
    session.turns = [
        TurnSummary(
            "turn-1",
            duration_seconds=4.5,
            time_to_first_token_seconds=0.75,
            status="completed",
            token_usage=usage,
            tool_count=1,
            tool_duration_seconds=1.5,
            tools=(tool,),
        )
    ]
    session.tool_executions = [tool]
    session.token_usage = usage
    session.cumulative_token_usage = TokenUsageSummary(total_tokens=1500)
    session.rate_limits = RateLimitSummary(
        primary=RateLimitWindow(used_percent=42.5, reset_at=123.0, window_minutes=300),
        credits=7.5,
        reached=False,
    )
    session.agents = [
        AgentNode(
            "agent-1",
            status="running",
            children=[AgentNode("agent-2", parent_thread_id="agent-1", status="completed")],
        )
    ]
    return result


class OutputTests(unittest.TestCase):
    def test_json_has_versioned_instance_shape(self) -> None:
        payload = json.loads(render_json(snapshot(), pretty=True))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["instances"][0]["instance_id"], "i1")
        self.assertEqual(payload["instances"][0]["sessions"][0]["network"]["state"], "ACTIVE")
        self.assertEqual(len(payload["instances"][0]["processes"]), 1)
        self.assertIsNone(payload["instances"][0]["sessions"][0]["alert"])
        self.assertIsNone(payload["instances"][0]["sessions"][0]["process"]["current_task"])

    def test_json_all_includes_auxiliary_processes(self) -> None:
        payload = json.loads(render_json(snapshot(), pretty=False, show_auxiliary=True))
        self.assertEqual(len(payload["instances"][0]["processes"]), 2)

    def test_json_additive_metrics_keep_schema_one_and_nullable_strings(self) -> None:
        payload = json.loads(render_json(snapshot_with_metrics(), pretty=False))
        self.assertEqual(payload["schema_version"], 1)
        session = payload["instances"][0]["sessions"][0]
        self.assertEqual(session["turns"][0]["time_to_first_token_seconds"], 0.75)
        self.assertEqual(session["turns"][0]["tools"][0]["display_name"], "shell")
        self.assertEqual(session["token_usage"]["context_tokens"], 1200)
        self.assertEqual(session["rate_limits"]["primary"]["used_percent"], 42.5)
        self.assertEqual(session["agents"][0]["children"][0]["thread_id"], "agent-2")
        self.assertIsNone(session["turns"][0]["collaboration_mode"])
        self.assertIsNone(session["turns"][0]["trace_id"])
        self.assertIsNone(session["agents"][0]["parent_thread_id"])
        self.assertIsNone(session["agents"][0]["provenance"]["source"])

    def test_text_summarizes_turn_tools_tokens_limits_and_subagents(self) -> None:
        output = render_text(snapshot_with_metrics())
        self.assertIn("Turn turn-1 | completed | 耗时 4s | TTFT 0.75s | 工具 1 / 1s", output)
        self.assertIn("Token：本 Turn in 400 / cached 100 / out 80", output)
        self.assertIn("累计 total 1500；上下文 1200/4000 (30.0%)", output)
        self.assertIn("Rate limit：primary 42.5% | credits 7.5 | 未触限", output)
        self.assertIn("Subagent：2 个 | completed 1 / running 1", output)

    def test_compact_json_is_one_line(self) -> None:
        output = render_json(snapshot(), pretty=False)
        self.assertNotIn("\n", output)
        json.loads(output)

    def test_text_and_tui_all_include_auxiliary_process(self) -> None:
        self.assertIn("辅助进程", render_text(snapshot(), show_auxiliary=True))
        lines, _ = main_view(snapshot(), 100, 30, "", set(), True, True, False)
        self.assertTrue(any("launcher" in line for line in lines))

    def test_tui_narrow_rows_keep_status(self) -> None:
        lines, _ = main_view(snapshot(), 48, 12, "", set(), True, False, False)
        self.assertTrue(any("模型正在生成" in line for line in lines))
        self.assertTrue(all(visible_width(line) <= 48 for line in lines))

    def test_tui_tiny_terminal_keeps_header_inside_real_dimensions(self) -> None:
        lines, _ = main_view(snapshot(), 24, 3, "", set(), True, False, True)
        self.assertEqual(len(lines), 3)
        self.assertIn("\033[7m", lines[0])
        self.assertTrue(all(visible_width(line) <= 24 for line in lines))

    def test_tui_selected_session_has_text_label_and_single_row_highlight(self) -> None:
        selected_key = f"session:{snapshot().sessions[0].key}"
        lines, _ = main_view(
            snapshot(), 80, 12, selected_key, set(), True, False, True
        )
        selected = next(line for line in lines if "已选中" in line)
        self.assertIn("已选中 · 模型正在生成", selected)
        self.assertIn("\033[7m", selected)

    def test_tui_search_filters_sessions(self) -> None:
        lines, refs = main_view(
            snapshot(),
            80,
            16,
            "",
            set(),
            True,
            False,
            False,
            "missing",
            True,
        )
        self.assertFalse(any(ref.kind == "session" for ref in refs))
        self.assertTrue(any("搜索" in line for line in lines))

    def test_tui_help_respects_terminal_width(self) -> None:
        lines = help_view(48, 12, False)
        self.assertTrue(any("快捷键" in line for line in lines))
        self.assertTrue(all(visible_width(line) <= 48 for line in lines))

    def test_tui_paused_detail_can_reach_complete_failure_message(self) -> None:
        session = snapshot().sessions[0]
        session.current_failure = FailureInfo(
            "test_error",
            "BEGIN-" + "x" * 300 + "-END",
        )
        rendered = "\n".join(
            line
            for offset in range(40)
            for line in detail_view(session, 48, 12, False, False, offset)
        )
        self.assertIn("BEGIN-", rendered)
        self.assertIn("-END", rendered)

    def test_tui_detail_keeps_network_status_fixed_while_scrolling(self) -> None:
        session = snapshot().sessions[0]
        session.network.reason = "正在接收响应数据"
        first = detail_view(session, 60, 12, False, False, 0)
        scrolled = detail_view(session, 60, 12, False, False, 999)
        self.assertIn("网络  活跃传输 · 正在接收响应数据", first[1])
        self.assertEqual(first[1], scrolled[1])

    def test_tui_detail_default_follow_mode_renders_latest_events(self) -> None:
        session = snapshot().sessions[0]
        session.events = [
            NormalizedEvent(index, "MODEL_PROGRESS", f"事件 {index}")
            for index in range(20)
        ]
        rendered = "\n".join(detail_view(session, 60, 12, False))
        self.assertIn("事件 19", rendered)

    def test_tui_detail_scroll_is_clamped_at_exact_last_page(self) -> None:
        session = snapshot().sessions[0]
        session.events = [
            NormalizedEvent(index, "MODEL_PROGRESS", f"事件 {index}")
            for index in range(20)
        ]
        maximum = detail_scroll_limit(session, 60, 12)
        self.assertGreater(maximum, 0)
        rendered = detail_view(session, 60, 12, False, False, maximum + 1000)
        self.assertIn(f"偏移 {maximum}", rendered[-1])
        previous = detail_view(session, 60, 12, False, False, maximum - 1)
        self.assertIn(f"偏移 {maximum - 1}", previous[-1])

    def test_tui_detail_colors_event_kinds_semantically(self) -> None:
        session = snapshot().sessions[0]
        session.events = [
            NormalizedEvent(1, "TURN_FAILED", "模型调用失败"),
            NormalizedEvent(2, "RECOVERED", "连接已恢复"),
            NormalizedEvent(3, "TOOL_RUNNING", "工具正在运行"),
        ]
        rendered = "\n".join(detail_view(session, 80, 20, True, False, 0))
        self.assertIn("\033[31m模型调用失败", rendered)
        self.assertIn("\033[32m连接已恢复", rendered)
        self.assertIn("\033[35m工具正在运行", rendered)

    def test_emit_frame_addresses_rows_without_newline_scroll(self) -> None:
        output = io.StringIO()
        with patch("sys.stdout", output):
            emit_frame(["header"], 10, 3)
        rendered = output.getvalue()
        self.assertIn("\033[1;1H", rendered)
        self.assertIn("\033[3;1H", rendered)
        self.assertNotIn("\r\n", rendered)
        self.assertFalse(rendered.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
