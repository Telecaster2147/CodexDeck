from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_net_health.models import (  # noqa: E402
    CodexPaths,
    FailureInfo,
    InstanceSnapshot,
    LifecycleState,
    MonitorSnapshot,
    NetworkEvidence,
    NetworkState,
    ProcessIdentity,
    ProcessInfo,
    SessionHealth,
)
from codex_net_health.presentation.json_output import render_json  # noqa: E402
from codex_net_health.presentation.text import render_text  # noqa: E402
from codex_net_health.presentation.tui.terminal import visible_width  # noqa: E402
from codex_net_health.presentation.tui.views import (  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
