from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from app import AppOptions, _run_application, _select_export_session  # noqa: E402
from cli import build_parser  # noqa: E402
from models import (  # noqa: E402
    CodexPaths,
    InstanceSnapshot,
    MonitorSnapshot,
    NetworkEvidence,
    NormalizedEvent,
    ProcessIdentity,
    ProcessInfo,
)
from state_machine import SessionStateMachine  # noqa: E402


def fixture() -> tuple[MonitorSnapshot, SessionStateMachine]:
    machine = SessionStateMachine(900)
    process = ProcessInfo(
        ProcessIdentity(42, 100),
        1,
        "codex",
        10,
        0.0,
        "S",
        "futex",
        "codex",
        "session",
        instance_id="home-1",
        session_id="session-1",
    )
    machine.ingest(
        "home-1:session-1",
        [NormalizedEvent(100, "TURN_STARTED", "started", source_id="start")],
    )
    session = machine.derive("home-1:session-1", process, NetworkEvidence(), 161)
    home = Path("/tmp/home-1")
    instance = InstanceSnapshot(
        "home-1",
        CodexPaths(
            home,
            home,
            home / "state_5.sqlite",
            home / "logs_2.sqlite",
            home / "session_index.jsonl",
            home / "sessions",
        ),
        str(home),
        str(home),
        "environment",
        sessions=[session],
    )
    return MonitorSnapshot("2026-07-16T12:00:00+08:00", 2.0, [instance]), machine


class FakeEngine:
    interval = 2.0

    def __init__(self, snapshot: MonitorSnapshot, machine: SessionStateMachine) -> None:
        self.snapshot = snapshot
        self.machine = machine
        self.samples = 0
        self.baselines = 0

    def sample(self) -> MonitorSnapshot:
        self.samples += 1
        return self.snapshot

    def baseline(self) -> None:
        self.baselines += 1


def options(command: str, **values: object) -> AppOptions:
    defaults = {
        "interval": 2.0,
        "idle_threshold": 30.0,
        "event_lookback": 900,
        "selected_pids": None,
        "selected_homes": None,
        "once": False,
        "json": False,
        "no_color": True,
        "show_auxiliary": False,
        "flat": False,
        "command": command,
    }
    defaults.update(values)
    return AppOptions(**defaults)


class CliFeatureTests(unittest.TestCase):
    def test_checkout_launcher_uses_project_environment(self) -> None:
        system_python = Path("/usr/bin/python3")
        if not system_python.exists():
            self.skipTest("system Python is unavailable")
        result = subprocess.run(
            [str(system_python), str(PROJECT_ROOT / "codex-net-health"), "--version"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^codexnet 0\.\d+\.\d+\s*$")

    def test_parser_exposes_export_metrics_and_history(self) -> None:
        args = build_parser().parse_args(
            ["export", "--session", "abc", "--history", "/tmp/history.sqlite"]
        )
        self.assertEqual(args.command, "export")
        self.assertEqual(args.session, "abc")
        self.assertEqual(args.history, Path("/tmp/history.sqlite"))
        self.assertEqual(build_parser().parse_args(["metrics"]).command, "metrics")
        hook = build_parser().parse_args(
            ["hook-event", "--hook-events", "/tmp/compact-hooks.jsonl"]
        )
        self.assertEqual(hook.command, "hook-event")
        self.assertEqual(hook.hook_events, Path("/tmp/compact-hooks.jsonl"))

    def test_metrics_samples_immediately_and_emits_prometheus(self) -> None:
        snapshot, machine = fixture()
        engine = FakeEngine(snapshot, machine)
        output = io.StringIO()
        with redirect_stdout(output):
            code = _run_application(engine, options("metrics"))
        self.assertEqual(code, 0)
        self.assertEqual(engine.samples, 1)
        self.assertEqual(engine.baselines, 0)
        self.assertIn("# TYPE codexnet_instances gauge", output.getvalue())

    def test_current_incidents_export_is_immediate_json(self) -> None:
        snapshot, machine = fixture()
        engine = FakeEngine(snapshot, machine)
        output = io.StringIO()
        with redirect_stdout(output):
            code = _run_application(
                engine,
                options("export", current_incidents=True),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["export_type"], "current_incidents")
        self.assertEqual(payload["incident_count"], 1)
        self.assertEqual(engine.baselines, 0)

    def test_session_export_uses_machine_retention(self) -> None:
        snapshot, machine = fixture()
        session = snapshot.sessions[0]
        engine = FakeEngine(snapshot, machine)
        output = io.StringIO()
        with redirect_stdout(output):
            _run_application(engine, options("export", export_session=session.key))
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["export_type"], "session_review")
        self.assertEqual(payload["retention"]["event_count"], 1)

    def test_export_selector_reports_missing_and_ambiguous_ids(self) -> None:
        snapshot, _ = fixture()
        with self.assertRaisesRegex(RuntimeError, "未找到指定会话"):
            _select_export_session(snapshot, "missing")
        duplicate = snapshot.sessions[0]
        snapshot.instances[0].sessions.append(duplicate)
        with self.assertRaisesRegex(RuntimeError, "多个 Codex Home"):
            _select_export_session(snapshot, duplicate.session_id)


if __name__ == "__main__":
    unittest.main()
