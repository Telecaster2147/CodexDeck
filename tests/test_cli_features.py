from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from app import AppOptions, _run_application, _select_export_session  # noqa: E402
from cli import _normalize_args, build_parser, required_commands_available  # noqa: E402
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
        "idle_threshold": 30.0,
        "event_lookback": 900,
        "selected_pids": None,
        "selected_homes": None,
        "once": False,
        "watch": False,
        "output_format": "text",
        "no_color": True,
        "show_auxiliary": False,
        "flat": False,
        "command": command,
    }
    defaults.update(values)
    return AppOptions(**defaults)


class CliFeatureTests(unittest.TestCase):
    def test_only_ps_is_a_hard_command_dependency(self) -> None:
        with patch("cli.shutil.which", side_effect=lambda command: None if command == "ss" else "/bin/ps"):
            required_commands_available()
        with patch("cli.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "ps"):
                required_commands_available()

    def test_checkout_launcher_uses_project_environment(self) -> None:
        system_python = Path("/usr/bin/python3")
        if not system_python.exists():
            self.skipTest("system Python is unavailable")
        result = subprocess.run(
            [str(system_python), str(PROJECT_ROOT / "codexdeck"), "--version"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^codexdeck 0\.\d+\.\d+\s*$")

    def test_parser_exposes_export(self) -> None:
        args = build_parser().parse_args(["export", "--session", "abc"])
        self.assertEqual(args.command, "export")
        self.assertEqual(args.session, "abc")
        self.assertTrue(
            build_parser().parse_args(["monitor", "--strict-observation"]).strict_observation
        )

        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                build_parser().parse_args(["--pid", "42", "export", "--session", "abc"])

    def test_monitor_output_modes_are_explicit_and_validated(self) -> None:
        parser = build_parser()
        watch = _normalize_args(
            parser,
            parser.parse_args(["monitor", "--watch", "--format", "ndjson"]),
        )
        self.assertTrue(watch.watch)
        self.assertEqual(watch.output_format, "ndjson")

        alias = _normalize_args(parser, parser.parse_args(["monitor", "--json"]))
        self.assertEqual(alias.output_format, "json")

        for arguments in (
            ["monitor", "--watch", "--format", "json"],
            ["monitor", "--format", "ndjson"],
        ):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parsed = parser.parse_args(arguments)
                    _normalize_args(parser, parsed)

    def test_subcommands_reject_arguments_from_other_domains(self) -> None:
        invalid = (
            ["doctor", "--once"],
            ["doctor", "--all"],
            ["export", "--strict-observation", "--session", "abc"],
            ["monitor", "--session", "abc"],
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(arguments)

    def test_bare_invocation_normalizes_to_monitor_defaults(self) -> None:
        parser = build_parser()
        args = _normalize_args(parser, parser.parse_args([]))
        self.assertEqual(args.command, "monitor")
        self.assertFalse(args.once)
        self.assertFalse(args.watch)
        self.assertEqual(args.output_format, "text")

        legacy_json = _normalize_args(parser, parser.parse_args(["--json"]))
        self.assertEqual(legacy_json.command, "monitor")
        self.assertEqual(legacy_json.output_format, "json")

    def test_subcommand_help_describes_constraints_and_output(self) -> None:
        expectations = {
            "doctor": ("不等待普通监控的基线窗口", "doctor_schema_version 2"),
            "export": ("必须指定 --session", "terminal transcript 正文不会进入导出"),
        }

        for command, phrases in expectations.items():
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    build_parser().parse_args([command, "--help"])
                self.assertEqual(raised.exception.code, 0)
                rendered = output.getvalue()
                self.assertIn(f"usage: codexdeck {command}", rendered)
                for phrase in phrases:
                    self.assertIn(phrase, rendered)

    def test_session_export_uses_machine_retention(self) -> None:
        snapshot, machine = fixture()
        session = snapshot.sessions[0]
        engine = FakeEngine(snapshot, machine)
        output = io.StringIO()
        with redirect_stdout(output):
            _run_application(engine, options("export", export_session=session.key))
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["export_type"], "bounded_session_report")
        self.assertEqual(payload["retention"]["event_count"], 1)

    def test_noninteractive_monitor_defaults_to_one_shot(self) -> None:
        snapshot, machine = fixture()
        engine = FakeEngine(snapshot, machine)
        output = io.StringIO()
        with (
            patch("app.sys.stdin.isatty", return_value=False),
            patch("app.sys.stdout.isatty", return_value=False),
            patch("app.time.sleep") as sleep,
            redirect_stdout(output),
        ):
            result = _run_application(engine, options("monitor"))
        self.assertEqual(result, 0)
        self.assertEqual(engine.baselines, 1)
        self.assertEqual(engine.samples, 1)
        sleep.assert_called_once_with(2.0)
        self.assertIn("CodexDeck", output.getvalue())

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
