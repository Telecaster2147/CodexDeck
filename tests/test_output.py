from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models import (  # noqa: E402
    AgentNode,
    CodexPaths,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
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

    def test_text_all_includes_auxiliary_process(self) -> None:
        self.assertIn("辅助进程", render_text(snapshot(), show_auxiliary=True))


if __name__ == "__main__":
    unittest.main()
